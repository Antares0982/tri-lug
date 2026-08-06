"""Animated-image detection and conversion, done locally on this machine.

Two independent jobs, both needed to keep animation alive across the bridge:

* `is_animated_image` — decides, from magic bytes alone, whether a payload is a
  *moving* image. The Telegram renderer needs this because `sendPhoto`
  re-encodes whatever it gets into a static JPEG; only `sendAnimation` keeps a
  GIF moving. A `mime` of ``image/gif`` is not enough on its own, since a
  single-frame GIF sent via `sendAnimation` becomes an unplayable "animation".
* `tgs_to_gif` / `video_to_gif` — normalize Telegram's two animated sticker
  formats (gzipped Lottie JSON, and WebM) plus its GIFs (silent MP4) into one
  animated GIF, which is the only moving format both QQ and Matrix render
  natively. Conversion happens here rather than in the QQ relay so the relay's
  wire contract stays untouched (see CLAUDE.md).

Both converters shell out (`lottieconverter`, `ffmpeg`, provided by the
devShell) and return `None` on *any* failure — missing binary, non-zero exit,
timeout. Callers are expected to degrade to a static thumbnail rather than drop
the message.

Conversion runs on the inbound path, but it costs the message *nothing* in
staleness terms: `BridgeMessage.ts` is stamped when the message is built, which
is after `_extract_attachments` returns (`tg_adapter._build_message`), so the
Router's 60s check at the fan never sees this latency. Nor does it block the
Router — `Router.submit` is a synchronous `put_nowait` downstream of here.

What conversion *does* cost is inbound ordering: python-telegram-bot dispatches
handlers concurrently, so a slow sticker can be overtaken by a plain text
message posted after it. That race already exists for any slow media download;
conversion widens the window, which is why `_CONVERT_TIMEOUT` is kept short and
why `_MAX_CONCURRENT` bounds a sticker flood.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import os
import shutil
import tempfile

from antares_bot.bot_logging import get_logger

_LOGGER = get_logger(__name__)

# Output geometry for every converted animation. 256px matches mautrix-telegram's
# default and keeps a typical sticker GIF in the hundreds-of-KB range, which
# matters because the bytes are inlined as base64 into the QQ transport payload.
GIF_SIZE = 256
GIF_FPS = 25

# Wall-clock cap on one conversion, and a ceiling on how many can run at once.
# See the module docstring: both exist to bound the inbound reordering window,
# not to protect a staleness budget.
_CONVERT_TIMEOUT = 20.0
_MAX_CONCURRENT = 2
_slots = asyncio.Semaphore(_MAX_CONCURRENT)


# --------------------------------------------------------------- detection
def is_animated_image(data: bytes | None) -> bool:
    """True when the payload is a multi-frame image (animated GIF, APNG, or
    animated WebP). Parses the container's block structure rather than
    pattern-matching, so a still image whose pixel data happens to contain a
    frame marker isn't misread as animated."""
    if not data:
        return False
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_is_animated(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_is_animated(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_is_animated(data)
    return False


def _gif_is_animated(data: bytes) -> bool:
    return _gif_frames(data, stop_at=2) > 1


def _gif_frames(data: bytes, stop_at: int | None = None) -> int:
    """Count the image descriptors in a GIF's block stream. Layout: 6-byte
    header, 7-byte logical screen descriptor, optional global colour table, then
    a stream of extension (0x21) / image (0x2C) blocks terminated by 0x3B.

    `stop_at` bounds the walk: the animated-or-not question only ever needs to
    see two, and stopping there keeps the check O(first two frames)."""
    n = len(data)
    if n < 13:
        return 0
    packed = data[10]
    pos = 13
    if packed & 0x80:  # global colour table present
        pos += 3 * (1 << ((packed & 0x07) + 1))
    frames = 0
    while pos < n:
        marker = data[pos]
        if marker == 0x3B:  # trailer
            break
        if marker == 0x21:  # extension: marker + label, then sub-blocks
            pos += 2
            pos = _skip_gif_sub_blocks(data, pos)
        elif marker == 0x2C:  # image descriptor: marker + 8 bytes + packed
            frames += 1
            if stop_at is not None and frames >= stop_at:
                return frames
            pos += 10
            local = data[pos - 1]
            if local & 0x80:  # local colour table present
                pos += 3 * (1 << ((local & 0x07) + 1))
            pos += 1  # LZW minimum code size
            pos = _skip_gif_sub_blocks(data, pos)
        else:  # malformed; don't guess further
            break
    return frames


def describe_gif(data: bytes | None) -> str:
    """`WxH, N frames` for a GIF payload, empty string for anything else.

    Diagnostic only, and only used on a rare warning path: when Telegram accepts
    a GIF but declines to treat it as an animation, the geometry and the frame
    count are the two things that make the pattern identifiable (Telegram
    converts GIF -> MP4 itself and refuses some inputs — very small ones in
    particular)."""
    if not data or data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        return ""
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return f"{width}x{height}, {_gif_frames(data)} frames"


def _skip_gif_sub_blocks(data: bytes, pos: int) -> int:
    """Advance past a GIF sub-block chain (length-prefixed runs, 0 terminator)."""
    n = len(data)
    while pos < n and data[pos]:
        pos += data[pos] + 1
    return pos + 1


def _png_is_animated(data: bytes) -> bool:
    """APNG marks itself with an `acTL` chunk, which must appear before the
    first `IDAT`."""
    pos = 8  # past the signature
    n = len(data)
    while pos + 8 <= n:
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        if ctype == b"acTL":
            return True
        if ctype == b"IDAT":
            return False
        pos += 12 + length  # length + type + payload + crc
    return False


def _webp_is_animated(data: bytes) -> bool:
    """An animated WebP is an extended (`VP8X`) file with the ANIMATION flag
    set, and carries `ANMF` frame chunks."""
    pos = 12  # past "RIFF" + size + "WEBP"
    n = len(data)
    while pos + 8 <= n:
        fourcc = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if fourcc == b"ANMF":
            return True
        if fourcc == b"VP8X" and size >= 1 and pos + 8 < n:
            if data[pos + 8] & 0x02:  # ANIMATION flag
                return True
        pos += 8 + size + (size & 1)  # chunks are padded to an even size
    return False


# -------------------------------------------------------------- conversion
@functools.cache
def _have(binary: str) -> bool:
    """Cached, so `shutil.which` isn't hit per sticker. Tests that patch PATH
    must call `_have.cache_clear()`."""
    found = shutil.which(binary) is not None
    if not found:
        # Once per process, not once per sticker: a busy group would otherwise
        # log this line forever.
        _LOGGER.warning(
            "[media] %s not on PATH; animated media will degrade to a static "
            "thumbnail (see shell.nix)",
            binary,
        )
    return found


async def tgs_to_gif(data: bytes) -> bytes | None:
    """Telegram animated sticker (gzipped Lottie JSON) -> animated GIF.

    The gzip layer is peeled here with the stdlib rather than left to
    lottieconverter, so the subprocess only ever sees plain JSON. Both ends are
    piped: neither Lottie JSON nor GIF output needs a seekable stream."""
    if not _have("lottieconverter"):
        return None
    try:
        lottie = gzip.decompress(data)
    except (OSError, EOFError):
        _LOGGER.warning("[media] .tgs payload is not gzipped", exc_info=True)
        return None
    return await _run(
        [
            "lottieconverter",
            "-",
            "-",
            "gif",
            f"{GIF_SIZE}x{GIF_SIZE}",
            str(GIF_FPS),
        ],
        stdin_data=lottie,
    )


async def video_to_gif(data: bytes, suffix: str) -> bytes | None:
    """WebM video sticker or MP4 (Telegram GIF) -> animated GIF.

    The input goes through a temp file, not a pipe: an MP4's moov atom can sit
    at the end of the file and Matroska demuxing may seek, both of which fail on
    a non-seekable stdin. GIF output streams fine, so that side stays a pipe.
    The palettegen/paletteuse filter pair is what keeps the output from looking
    like 1998; it is the same chain mautrix-telegram uses."""
    if not _have("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory(prefix="tri-lug-anim-") as tmp:
        src = os.path.join(tmp, f"in.{suffix}")
        with open(src, "wb") as fh:
            fh.write(data)
        # Forcing the VP9 decoder for WebM mirrors what mautrix-telegram does for
        # video stickers; MP4 is left to auto-detection.
        decoder = ["-c:v", "libvpx-vp9"] if suffix == "webm" else []
        return await _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                *decoder,
                "-i",
                src,
                "-vf",
                f"fps={GIF_FPS},scale={GIF_SIZE}:-1:flags=lanczos,"
                "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                "-loop",
                "0",
                "-f",
                "gif",
                "pipe:1",
            ]
        )


async def _run(argv: list[str], *, stdin_data: bytes | None = None) -> bytes | None:
    """Run a converter and return its stdout, or None on any failure.

    Never raises: the caller's contract is to fall back to a static image, so a
    broken converter must not take the message down with it."""
    async with _slots:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE
                if stdin_data is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=_CONVERT_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "[media] %s timed out after %.0fs", argv[0], _CONVERT_TIMEOUT
            )
            return None
        except Exception:
            _LOGGER.warning("[media] %s failed to run", argv[0], exc_info=True)
            return None
        finally:
            # wait_for cancels communicate(); it does NOT reap the child. Without
            # this, a run of timeouts leaks processes and their pipe fds.
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
        assert proc is not None  # any failure to spawn returned above
        if proc.returncode != 0 or not out:
            _LOGGER.warning(
                "[media] %s exited %s: %s",
                argv[0],
                proc.returncode,
                err.decode("utf-8", "replace").strip()[:400],
            )
            return None
        return out
