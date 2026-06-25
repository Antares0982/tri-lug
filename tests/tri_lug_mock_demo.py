"""Network-free smoke test for the tri_lug bridge spine.

Exercises Router (msg-in -> fan -> msg-out) + IdMap + MockAdapters with no live
platform and no real waiting (sleep is stubbed, so the 3s pacing is observed via
the requested sleep durations rather than by blocking):

  1. a QQ message fans out to TG + Matrix and the three ids get linked;
  2. a Matrix reply resolves to the right native id on each target;
  3. an image (sticker normalized to image) fans out carrying the attachment;
  4. a single message with multiple images carries them all;
  5. a stale message (old ts) is dropped at the fan;
  6. per-target pacing requests a >=3s gap between back-to-back sends;
  7. dry-run delivers nothing to the adapters.

Run:  python -m tests.tri_lug_mock_demo   (from the repo root)
"""

import asyncio
import time

from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import MATRIX, QQ, TG, Attachment
from modules.tri_lug_utils.idmap import IdMap
from modules.tri_lug_utils.router import Router

ROOM = "default"


async def _make_bridge(dry_run=False):
    """A fresh idmap + router (sleep stubbed) wired to three mock adapters.
    Returns (idmap, router, sleeps, {platform: adapter})."""
    idmap = IdMap(":memory:")
    await idmap.open()
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)  # record but don't actually wait

    router = Router(idmap, dry_run=dry_run, send_gap_seconds=3.0, sleep=fake_sleep)
    adapters = {p: MockAdapter(p, ROOM) for p in (TG, QQ, MATRIX)}
    for a in adapters.values():
        router.register(a)
    await router.start()
    return idmap, router, sleeps, adapters


async def main() -> None:
    idmap, router, sleeps, adp = await _make_bridge()
    tg, qq, matrix = adp[TG], adp[QQ], adp[MATRIX]

    print("\n--- step 1: QQ user posts a message ---")
    qq_id = await qq.simulate_incoming("Alice", "hello from QQ")
    await router.join()
    assert len(tg.sent) == 1, tg.sent
    assert len(matrix.sent) == 1, matrix.sent
    tg_native = tg.sent[0][0]
    matrix_native = matrix.sent[0][0]
    print(f"qq_id={qq_id}  tg_native={tg_native}  matrix_native={matrix_native}")

    assert await idmap.native_id_for(QQ, qq_id, TG) == tg_native
    assert await idmap.native_id_for(QQ, qq_id, MATRIX) == matrix_native
    assert await idmap.native_id_for(TG, tg_native, MATRIX) == matrix_native
    print("id-map linkage OK")

    print("\n--- step 2: Matrix user replies to that message ---")
    await matrix.simulate_incoming(
        "Bob", "replying from Matrix", reply_to_native_id=matrix_native
    )
    await router.join()
    qq_reply = qq.sent[-1]
    tg_reply = tg.sent[-1]
    print(f"qq target reply_to = {qq_reply[2]} (expect {qq_id})")
    print(f"tg target reply_to = {tg_reply[2]} (expect {tg_native})")
    assert qq_reply[2] == qq_id, qq_reply
    assert tg_reply[2] == tg_native, tg_reply
    print("cross-platform reply resolution OK")

    print("\n--- step 3: QQ user posts one image ---")
    await qq.simulate_incoming(
        "Alice",
        text="",
        attachments=[Attachment("image", data=b"\x89PNG fake", mime="image/png")],
    )
    await router.join()
    assert len(tg.sent[-1][1].attachments) == 1
    assert len(matrix.sent[-1][1].attachments) == 1
    print("single-image fan-out OK")

    print("\n--- step 4: QQ user posts multiple images in one message ---")
    await qq.simulate_incoming(
        "Alice",
        text="three pics",
        attachments=[
            Attachment("image", data=b"a", mime="image/png"),
            Attachment("image", data=b"b", mime="image/png"),
            Attachment("image", data=b"c", mime="image/png"),
        ],
    )
    await router.join()
    assert len(tg.sent[-1][1].attachments) == 3, tg.sent[-1][1].attachments
    assert len(matrix.sent[-1][1].attachments) == 3
    print("multi-image fan-out OK (all 3 carried)")

    print("\n--- step 5: a stale message is dropped at the fan ---")
    before = len(tg.sent)
    await qq.simulate_incoming("Alice", "ancient", ts=time.time() - 120)
    await router.join()
    assert len(tg.sent) == before, "stale message should not have fanned out"
    print("stale-drop OK")

    print("\n--- step 6: per-target pacing requests >=3s between sends ---")
    # Two TG-bound messages back to back -> the second send should ask to sleep
    # ~3s before going out (sleep is stubbed so nothing actually blocks).
    sleeps.clear()
    await qq.simulate_incoming("Alice", "pace-1")
    await qq.simulate_incoming("Alice", "pace-2")
    await router.join()
    assert any(d >= 2.9 for d in sleeps), f"expected a ~3s pacing wait, got {sleeps}"
    print(f"pacing OK (requested waits: {[round(d, 2) for d in sleeps]})")

    await router.stop()
    await idmap.close()

    print("\n--- step 7: dry-run delivers nothing ---")
    d_idmap, d_router, _, d_adp = await _make_bridge(dry_run=True)
    await d_adp[QQ].simulate_incoming("Alice", "should not be delivered")
    await d_router.join()
    assert d_adp[TG].sent == [], d_adp[TG].sent
    assert d_adp[MATRIX].sent == [], d_adp[MATRIX].sent
    print("dry-run OK (no adapter.send happened)")
    await d_router.stop()
    await d_idmap.close()

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
