"""Network-free tests for the Telegram adapter.

No python-telegram-bot Application / network: a `_FakeBot` records the kwargs of
every send_* call and hands back a synthetic message id, and inbound media is
duck-typed (`_FakeSticker` / `_FakeMessage`) rather than built from real PTB
objects, whose constructors are large and bound to a live `Bot`.

The animated-media converters are monkeypatched throughout, so the suite needs
neither ffmpeg nor lottieconverter installed.

Covers:
  1. the `[label] name:` header is bolded via an explicit entity (UTF-16 offsets)
  2. text / photo / album / audio all carry that entity, on the right leg
  3. a bridged QQ voice note goes out via send_audio, not send_voice
  4. outbound: animated images go via send_animation (send_photo would freeze
     them), still images keep their album batching, and source order survives
  5. inbound: animated/video stickers and GIFs are converted once, cached by
     file_unique_id, and degrade to a static thumbnail on any failure
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from telegram import MessageEntity

from modules.tri_lug_utils import media
from modules.tri_lug_utils.bridge_message import (
    QQ,
    Attachment,
    BridgeMessage,
    BridgeUser,
)
from modules.tri_lug_utils.tg_adapter import TelegramAdapter

from conftest import ROOM, gif_bytes, png_bytes, webp_bytes

CHAT_ID = 4242
# A non-BMP character in the name makes the entity length differ from the plain
# character count, so the UTF-16 measurement is actually exercised.
SENDER = "小明🎉"
HEADER = f"[QQ] {SENDER}:"
HEADER_U16_LEN = 10  # "[QQ] " 5 + 小明 2 + 🎉 2 (surrogate pair) + ":" 1


@dataclass
class _Sent:
    message_id: int


@dataclass
class _FakeBot:
    id: int = 1
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _next_id: int = 100

    def _record(self, name: str, kwargs: dict, count: int = 1):
        self.calls.append((name, kwargs))
        sent = []
        for _ in range(count):
            self._next_id += 1
            sent.append(_Sent(self._next_id))
        return sent

    async def send_message(self, **kw):
        return self._record("send_message", kw)[0]

    async def send_photo(self, **kw):
        return self._record("send_photo", kw)[0]

    async def send_audio(self, **kw):
        return self._record("send_audio", kw)[0]

    async def send_media_group(self, **kw):
        return self._record("send_media_group", kw, count=len(kw["media"]))

    async def send_animation(self, **kw):
        return self._record("send_animation", kw)[0]


@dataclass
class _FakeApp:
    bot: _FakeBot = field(default_factory=_FakeBot)


def _adapter() -> tuple[TelegramAdapter, _FakeBot]:
    adapter = TelegramAdapter(ROOM, CHAT_ID)
    app = _FakeApp()
    adapter.attach_app(app)  # type: ignore[arg-type]
    return adapter, app.bot


def _message(text: str = "", attachments: list[Attachment] | None = None):
    return BridgeMessage(
        platform=QQ,
        room_key=ROOM,
        msg_id="1",
        sender=BridgeUser(QQ, "10086", SENDER),
        text=text,
        attachments=attachments or [],
    )


def _assert_header_bold(entities) -> None:
    assert len(entities) == 1, entities
    entity = entities[0]
    assert entity.type == MessageEntity.BOLD, entity.type
    assert entity.offset == 0, entity.offset
    assert entity.length == HEADER_U16_LEN, entity.length


async def test_text_header_bold():
    adapter, bot = _adapter()
    ids = await adapter.send(_message("hello"), reply_to_native_id="7")
    assert ids == ["101"], ids
    name, kw = bot.calls[0]
    assert name == "send_message"
    assert kw["text"] == f"{HEADER}\nhello", kw["text"]
    assert kw["reply_to_message_id"] == 7
    _assert_header_bold(kw["entities"])


async def test_photo_caption_entities():
    adapter, bot = _adapter()
    msg = _message("look", [Attachment("image", data=b"\x89PNG", mime="image/png")])
    await adapter.send(msg, reply_to_native_id=None)
    name, kw = bot.calls[0]
    assert name == "send_photo"
    assert kw["caption"] == f"{HEADER}\nlook"
    _assert_header_bold(kw["caption_entities"])


async def test_album_caption_only_on_first_item():
    adapter, bot = _adapter()
    msg = _message("three", [Attachment("image", data=b"i%d" % i) for i in range(3)])
    ids = await adapter.send(msg, reply_to_native_id=None)
    assert len(ids) == 3, ids
    name, kw = bot.calls[0]
    assert name == "send_media_group"
    media = kw["media"]
    assert media[0].caption == f"{HEADER}\nthree"
    _assert_header_bold(media[0].caption_entities)
    # The rest of the album carries neither caption nor entities.
    assert all(m.caption is None and not m.caption_entities for m in media[1:])


async def test_voice_note_uses_send_audio():
    """A bridged QQ voice note: send_audio (send_voice would reject mp3), with
    the bold header as its caption and the reply anchored on it."""
    adapter, bot = _adapter()
    msg = _message(
        "",
        [Attachment("audio", data=b"ID3mp3", mime="audio/mpeg", filename="voice.mp3")],
    )
    ids = await adapter.send(msg, reply_to_native_id="55")
    assert ids == ["101"], ids
    assert [c[0] for c in bot.calls] == ["send_audio"], bot.calls
    kw = bot.calls[0][1]
    assert kw["audio"] == b"ID3mp3"
    assert kw["filename"] == "voice.mp3"
    assert kw["caption"] == HEADER  # no body text -> header alone
    assert kw["reply_to_message_id"] == 55
    _assert_header_bold(kw["caption_entities"])


async def test_image_and_audio_anchor_reply_once():
    """With both kinds present the image leg goes first and owns the caption +
    reply; the audio leg follows bare, and every id is returned for linking."""
    adapter, bot = _adapter()
    msg = _message(
        "mixed",
        [
            Attachment("image", data=b"\x89PNG", mime="image/png"),
            Attachment("audio", data=b"ID3mp3", mime="audio/mpeg"),
        ],
    )
    ids = await adapter.send(msg, reply_to_native_id="9")
    assert ids == ["101", "102"], ids
    assert [c[0] for c in bot.calls] == ["send_photo", "send_audio"]
    audio_kw = bot.calls[1][1]
    assert audio_kw["caption"] is None
    assert audio_kw["caption_entities"] is None
    assert audio_kw["reply_to_message_id"] is None
    assert audio_kw["filename"] == "voice.mp3"  # no filename -> default


# ------------------------------------------------------- outbound: animation
ANIMATED = gif_bytes(frames=3)
STILL = gif_bytes(frames=1)


async def test_animated_gif_uses_send_animation():
    """send_photo re-encodes to a static JPEG, which is exactly the bug: an
    animated GIF has to go out through send_animation to keep moving."""
    adapter, bot = _adapter()
    msg = _message("wiggle", [Attachment("image", data=ANIMATED, mime="image/gif")])
    ids = await adapter.send(msg, reply_to_native_id="7")
    assert ids == ["101"], ids
    assert [c[0] for c in bot.calls] == ["send_animation"], bot.calls
    kw = bot.calls[0][1]
    assert kw["animation"] == ANIMATED
    assert kw["caption"] == f"{HEADER}\nwiggle"
    assert kw["reply_to_message_id"] == 7
    _assert_header_bold(kw["caption_entities"])


async def test_still_gif_still_uses_send_photo():
    """The decision is made on the bytes, not the mime: a single-frame GIF put
    through send_animation becomes an animation bubble that cannot play."""
    adapter, bot = _adapter()
    msg = _message("", [Attachment("image", data=STILL, mime="image/gif")])
    await adapter.send(msg, reply_to_native_id=None)
    assert [c[0] for c in bot.calls] == ["send_photo"], bot.calls


async def test_animated_apng_falls_back_to_send_photo():
    """Telegram's sendAnimation takes GIF or silent MP4 only. Routing an APNG
    (or animated WebP — a QQ mface can be either) there gets it rejected, which
    would *lose* a message that send_photo delivers, if statically."""
    adapter, bot = _adapter()
    for data in (png_bytes(animated=True), webp_bytes(animated=True)):
        bot.calls.clear()
        msg = _message("", [Attachment("image", data=data)])
        await adapter.send(msg, reply_to_native_id=None)
        assert [c[0] for c in bot.calls] == ["send_photo"], bot.calls


async def test_url_only_gif_trusts_mime():
    """With no bytes to inspect (a QQ image that degraded to a url), the mime
    is all there is to go on."""
    adapter, bot = _adapter()
    msg = _message("", [Attachment("image", url="http://x/a.gif", mime="image/gif")])
    await adapter.send(msg, reply_to_native_id=None)
    assert [c[0] for c in bot.calls] == ["send_animation"], bot.calls


async def test_mixed_media_preserves_source_order():
    """Stills and animations interleaved: consecutive stills batch into an
    album, animations go out alone (sendMediaGroup rejects them), and the
    sender's ordering is not rearranged."""
    adapter, bot = _adapter()
    msg = _message(
        "mixed",
        [
            Attachment("image", data=STILL, mime="image/gif"),
            Attachment("image", data=STILL, mime="image/gif"),
            Attachment("image", data=ANIMATED, mime="image/gif"),
            Attachment("image", data=STILL, mime="image/gif"),
        ],
    )
    ids = await adapter.send(msg, reply_to_native_id="9")
    assert [c[0] for c in bot.calls] == [
        "send_media_group",
        "send_animation",
        "send_photo",
    ], bot.calls
    assert len(ids) == 4, ids  # album contributes 2


async def test_only_first_leg_carries_caption_and_reply():
    """Whatever goes out first owns the caption and the reply anchor; every
    later leg is bare, so a reply isn't duplicated across the fan-out."""
    adapter, bot = _adapter()
    msg = _message(
        "lead",
        [
            Attachment("image", data=ANIMATED, mime="image/gif"),
            Attachment("image", data=STILL, mime="image/gif"),
        ],
    )
    await adapter.send(msg, reply_to_native_id="42")
    first_kw = bot.calls[0][1]
    assert bot.calls[0][0] == "send_animation"
    assert first_kw["caption"] == f"{HEADER}\nlead"
    assert first_kw["reply_to_message_id"] == 42
    second_kw = bot.calls[1][1]
    assert bot.calls[1][0] == "send_photo"
    assert second_kw["caption"] is None
    assert second_kw["caption_entities"] is None
    assert second_kw["reply_to_message_id"] is None


async def test_animation_leading_album_leaves_album_uncaptioned():
    """Regression guard on the album path once it is no longer always first."""
    adapter, bot = _adapter()
    msg = _message(
        "x",
        [
            Attachment("image", data=ANIMATED, mime="image/gif"),
            Attachment("image", data=STILL, mime="image/gif"),
            Attachment("image", data=STILL, mime="image/gif"),
        ],
    )
    await adapter.send(msg, reply_to_native_id="5")
    assert [c[0] for c in bot.calls] == ["send_animation", "send_media_group"]
    group_kw = bot.calls[1][1]
    assert all(m.caption is None for m in group_kw["media"])
    assert group_kw["reply_to_message_id"] is None


# ---------------------------------------------------------- inbound: stickers
@dataclass
class _FakeFile:
    data: bytes

    async def download_as_bytearray(self):
        return bytearray(self.data)


@dataclass
class _FakeMedia:
    """Duck-typed stand-in for telegram.Sticker / telegram.Animation."""

    file_unique_id: str = "uniq"
    data: bytes = b"raw"
    is_animated: bool = False
    is_video: bool = False
    thumbnail: "_FakeMedia | None" = None
    duration: float | None = None
    file_size: int | None = None

    async def get_file(self):
        return _FakeFile(self.data)


@dataclass
class _FakeMessage:
    photo: list = field(default_factory=list)
    sticker: object | None = None
    animation: object | None = None


def _thumbed(**kw) -> _FakeMedia:
    return _FakeMedia(thumbnail=_FakeMedia(data=b"\xff\xd8\xffthumb"), **kw)


async def _extract(adapter: TelegramAdapter, msg) -> list[Attachment]:
    """Drive the inbound extractor with a duck-typed message (the real
    telegram.Message is not constructible without a live Bot)."""
    return await adapter._extract_attachments(msg)  # type: ignore[arg-type]


@pytest.fixture
def converters(monkeypatch):
    """Replace both converters with recorders. Returns the call log; set
    `.result` to None to simulate a conversion failure."""

    class _Rec:
        calls: list[tuple[str, bytes]] = []
        result: bytes | None = ANIMATED

    rec = _Rec()

    async def fake_tgs(data):
        rec.calls.append(("tgs", data))
        return rec.result

    async def fake_video(data, suffix):
        rec.calls.append((suffix, data))
        return rec.result

    monkeypatch.setattr(media, "tgs_to_gif", fake_tgs)
    monkeypatch.setattr(media, "video_to_gif", fake_video)
    return rec


async def test_lottie_sticker_is_converted_to_gif(converters):
    adapter, _ = _adapter()
    sticker = _thumbed(is_animated=True)
    out = await _extract(adapter, _FakeMessage(sticker=sticker))
    assert [c[0] for c in converters.calls] == ["tgs"]
    assert len(out) == 1
    assert out[0].mime == "image/gif"
    assert out[0].data == ANIMATED


async def test_video_sticker_is_converted_to_gif(converters):
    adapter, _ = _adapter()
    out = await _extract(adapter, _FakeMessage(sticker=_thumbed(is_video=True)))
    assert [c[0] for c in converters.calls] == ["webm"]
    assert out[0].mime == "image/gif"


async def test_static_sticker_is_untouched(converters):
    adapter, _ = _adapter()
    out = await _extract(adapter, _FakeMessage(sticker=_FakeMedia()))
    assert converters.calls == []
    assert out[0].mime == "image/webp"


async def test_conversion_failure_falls_back_to_thumbnail(converters):
    """The load-bearing degradation: no ffmpeg/lottieconverter, or a broken
    sticker, must never cost the message — it drops to today's behaviour."""
    converters.result = None
    adapter, _ = _adapter()
    out = await _extract(adapter, _FakeMessage(sticker=_thumbed(is_animated=True)))
    assert len(out) == 1
    assert out[0].mime == "image/jpeg"
    assert out[0].data == b"\xff\xd8\xffthumb"


async def test_conversion_is_cached_by_file_unique_id(converters):
    adapter, _ = _adapter()
    for _ in range(3):
        await _extract(adapter, _FakeMessage(sticker=_thumbed(is_animated=True)))
    assert len(converters.calls) == 1, converters.calls


async def test_conversion_failure_is_cached_too(converters):
    """Retrying a sticker that already failed costs a subprocess on the
    latency-sensitive inbound path, so negative results are cached."""
    converters.result = None
    adapter, _ = _adapter()
    for _ in range(3):
        await _extract(adapter, _FakeMessage(sticker=_thumbed(is_animated=True)))
    assert len(converters.calls) == 1, converters.calls


async def test_gif_animation_is_converted(converters):
    adapter, _ = _adapter()
    out = await _extract(
        adapter, _FakeMessage(animation=_thumbed(duration=3, file_size=1000))
    )
    assert [c[0] for c in converters.calls] == ["mp4"]
    assert out[0].mime == "image/gif"


async def test_overlong_animation_skips_conversion(converters):
    """A Telegram 'GIF' is an arbitrary silent MP4; a long one would make a
    huge GIF, so it never reaches the converter at all."""
    adapter, _ = _adapter()
    out = await _extract(
        adapter, _FakeMessage(animation=_thumbed(duration=600, file_size=1000))
    )
    assert converters.calls == []
    assert out[0].mime == "image/jpeg"


async def test_oversized_animation_skips_conversion(converters):
    adapter, _ = _adapter()
    out = await _extract(
        adapter,
        _FakeMessage(animation=_thumbed(duration=1, file_size=50 * 1024 * 1024)),
    )
    assert converters.calls == []
    assert out[0].mime == "image/jpeg"


async def test_conversion_failure_without_thumbnail_yields_nothing(converters):
    converters.result = None
    adapter, _ = _adapter()
    out = await _extract(adapter, _FakeMessage(sticker=_FakeMedia(is_animated=True)))
    assert out == []


async def test_single_frame_conversion_output_is_rejected(converters):
    """A converter that succeeds but produces a still is worse than the
    thumbnail: same lack of motion, more bytes."""
    converters.result = STILL
    adapter, _ = _adapter()
    out = await _extract(adapter, _FakeMessage(sticker=_thumbed(is_animated=True)))
    assert out[0].mime == "image/jpeg"


async def test_oversized_conversion_output_is_rejected(converters):
    converters.result = gif_bytes(frames=2) + b"\x00" * (9 * 1024 * 1024)
    adapter, _ = _adapter()
    out = await _extract(adapter, _FakeMessage(sticker=_thumbed(is_animated=True)))
    assert out[0].mime == "image/jpeg"


async def test_webp_thumbnail_keeps_its_mime(converters):
    """Telegram sticker thumbnails are WEBP *or* JPEG. Labelling a WEBP as JPEG
    makes Matrix clients refuse to render it."""
    converters.result = None
    adapter, _ = _adapter()
    sticker = _FakeMedia(is_animated=True, thumbnail=_FakeMedia(data=webp_bytes()))
    out = await _extract(adapter, _FakeMessage(sticker=sticker))
    assert out[0].mime == "image/webp"
    assert out[0].filename == "sticker.webp"
