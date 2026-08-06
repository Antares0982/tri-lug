"""Byte-level tests for animated-image detection.

`is_animated_image` decides whether an outbound image has to go to Telegram via
`send_animation` instead of `send_photo`, so a wrong answer either freezes a
moving image or produces an unplayable animation bubble. It parses container
structure rather than sniffing a prefix; these fixtures exercise that.

No ffmpeg / lottieconverter needed — the converters themselves are stubbed
wherever they are used (see tests/test_tri_lug_tg.py).
"""

from __future__ import annotations

from modules.tri_lug_utils.media import describe_gif, is_animated_image

from conftest import gif_bytes, png_bytes, webp_bytes


def test_multi_frame_gif_is_animated():
    assert is_animated_image(gif_bytes(frames=3))


def test_single_frame_gif_is_not_animated():
    """The case that makes the mime type alone insufficient: a still image that
    is nonetheless `image/gif`."""
    assert not is_animated_image(gif_bytes(frames=1))


def test_apng_is_animated():
    assert is_animated_image(png_bytes(animated=True))


def test_plain_png_is_not_animated():
    assert not is_animated_image(png_bytes())


def test_animated_webp_is_animated():
    assert is_animated_image(webp_bytes(animated=True))


def test_still_webp_is_not_animated():
    assert not is_animated_image(webp_bytes())


def test_jpeg_and_junk_are_not_animated():
    assert not is_animated_image(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    assert not is_animated_image(b"not an image at all")


def test_empty_input_is_not_animated():
    assert not is_animated_image(b"")
    assert not is_animated_image(None)


def test_truncated_gif_does_not_hang_or_raise():
    """A partially-downloaded GIF must fail closed, not loop or explode."""
    full = gif_bytes(frames=3)
    for cut in (5, 13, 20, len(full) - 1):
        assert is_animated_image(full[:cut]) in (True, False)


def test_describe_gif_reports_geometry_and_frames():
    """Diagnostic for the one case nothing else explains: Telegram accepting a
    GIF but filing it as a document. Geometry and frame count are what make
    those refusals recognizable as a pattern."""
    assert describe_gif(gif_bytes(frames=3)) == "1x1, 3 frames"


def test_describe_gif_ignores_non_gifs():
    assert describe_gif(png_bytes(animated=True)) == ""
    assert describe_gif(b"") == ""
    assert describe_gif(None) == ""
