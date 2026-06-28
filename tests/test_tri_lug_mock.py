"""Network-free tests for the tri_lug bridge spine.

Exercises Router (msg-in -> fan -> msg-out) + IdMap + MockAdapters with no live
platform and no real waiting (sleep is stubbed via the `make_bridge` fixture, so
the 3s pacing is observed via requested sleep durations rather than by blocking).
"""

import time

from modules.tri_lug_utils.bridge_message import MATRIX, QQ, TG, Attachment


async def test_fanout_and_reply_resolution(make_bridge):
    """A QQ message fans out to TG + Matrix, the ids link, and a Matrix reply
    resolves to the right native id on each target."""
    router, idmap, _sleeps, adp = await make_bridge()
    tg, qq, matrix = adp[TG], adp[QQ], adp[MATRIX]

    qq_id = await qq.simulate_incoming("Alice", "hello from QQ")
    await router.join()
    assert len(tg.sent) == 1, tg.sent
    assert len(matrix.sent) == 1, matrix.sent
    tg_native = tg.sent[0][0]
    matrix_native = matrix.sent[0][0]

    assert await idmap.native_id_for(QQ, qq_id, TG) == tg_native
    assert await idmap.native_id_for(QQ, qq_id, MATRIX) == matrix_native
    assert await idmap.native_id_for(TG, tg_native, MATRIX) == matrix_native

    # Matrix user replies to that message -> reply re-points to each target's id.
    await matrix.simulate_incoming(
        "Bob", "replying from Matrix", reply_to_native_id=matrix_native
    )
    await router.join()
    qq_reply = qq.sent[-1]
    tg_reply = tg.sent[-1]
    assert qq_reply[2] == qq_id, qq_reply
    assert tg_reply[2] == tg_native, tg_reply


async def test_single_image_fanout(make_bridge):
    """A single image (sticker normalized to image) fans out carrying it."""
    router, _idmap, _sleeps, adp = await make_bridge()
    await adp[QQ].simulate_incoming(
        "Alice",
        text="",
        attachments=[Attachment("image", data=b"\x89PNG fake", mime="image/png")],
    )
    await router.join()
    assert len(adp[TG].sent[-1][1].attachments) == 1
    assert len(adp[MATRIX].sent[-1][1].attachments) == 1


async def test_multi_image_fanout(make_bridge):
    """A single message with multiple images carries them all."""
    router, _idmap, _sleeps, adp = await make_bridge()
    await adp[QQ].simulate_incoming(
        "Alice",
        text="three pics",
        attachments=[
            Attachment("image", data=b"a", mime="image/png"),
            Attachment("image", data=b"b", mime="image/png"),
            Attachment("image", data=b"c", mime="image/png"),
        ],
    )
    await router.join()
    assert len(adp[TG].sent[-1][1].attachments) == 3, adp[TG].sent[-1][1].attachments
    assert len(adp[MATRIX].sent[-1][1].attachments) == 3


async def test_stale_message_dropped(make_bridge):
    """A message with an old ts is dropped at the fan."""
    router, _idmap, _sleeps, adp = await make_bridge()
    before = len(adp[TG].sent)
    await adp[QQ].simulate_incoming("Alice", "ancient", ts=time.time() - 120)
    await router.join()
    assert len(adp[TG].sent) == before, "stale message should not have fanned out"


async def test_pacing_gap(make_bridge):
    """Per-target pacing requests a >=3s gap between back-to-back sends (sleep is
    stubbed, so the wait is asserted via the recorded durations)."""
    router, _idmap, sleeps, adp = await make_bridge()
    await adp[QQ].simulate_incoming("Alice", "pace-1")
    await adp[QQ].simulate_incoming("Alice", "pace-2")
    await router.join()
    assert any(d >= 2.9 for d in sleeps), f"expected a ~3s pacing wait, got {sleeps}"


async def test_dry_run_delivers_nothing(make_bridge):
    """In dry-run mode nothing is delivered to the adapters."""
    router, _idmap, _sleeps, adp = await make_bridge(dry_run=True)
    await adp[QQ].simulate_incoming("Alice", "should not be delivered")
    await router.join()
    assert adp[TG].sent == [], adp[TG].sent
    assert adp[MATRIX].sent == [], adp[MATRIX].sent
