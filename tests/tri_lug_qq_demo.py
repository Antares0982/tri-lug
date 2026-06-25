"""Network-free tests for the QQ adapter and OneBot translation.

Covers, with no RabbitMQ / NapCat:
  1. parse_group_event: reply + at + text + image + mface + face -> BridgeMessage
  2. parse rejects non-message / empty events
  3. build_send_segments: reply prepend, identity header, base64 image
  4. QQAdapter.handle_event: group filter + self-message loop prevention + emit
  5. QQAdapter.send via a fake transport: returns native id, builds right action

Run:  python -m tests.tri_lug_qq_demo
"""

from __future__ import annotations

import asyncio

from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import (
    QQ,
    TG,
    Attachment,
    BridgeMessage,
    BridgeUser,
)
from modules.tri_lug_utils.idmap import IdMap
from modules.tri_lug_utils.onebot import build_send_segments, parse_group_event
from modules.tri_lug_utils.qq_adapter import QQAdapter, QQTransport
from modules.tri_lug_utils.router import Router

ROOM = "default"
GROUP_ID = 222
SELF_UIN = 10000


class FakeQQTransport(QQTransport):
    """Records action calls and hands back a synthetic message_id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._mid = 1000

    async def call_action(self, action: str, params: dict) -> dict | None:
        self.calls.append((action, params))
        self._mid += 1
        return {"message_id": self._mid}


def _group_event(segments, user_id=456, message_id=789):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": user_id,
        "message_id": message_id,
        "sender": {"user_id": user_id, "nickname": "Alice", "card": "GroupAlice"},
        "message": segments,
    }


def test_parse_rich_event():
    print("\n--- 1: parse rich event ---")
    ev = _group_event(
        [
            {"type": "reply", "data": {"id": "111"}},
            {"type": "at", "data": {"qq": "456", "name": "Bob"}},
            {"type": "text", "data": {"text": "hi there"}},
            {"type": "image", "data": {"url": "https://x/a.jpg", "file": "a.jpg"}},
            {"type": "mface", "data": {"url": "https://x/s.png", "summary": "[doge]"}},
            {"type": "face", "data": {"id": "178"}},
        ]
    )
    bm = parse_group_event(ev, ROOM)
    assert bm is not None
    assert bm.platform == QQ and bm.msg_id == "789"
    assert bm.reply_to_msg_id == "111"
    assert bm.text == "@Bob hi there", repr(bm.text)
    assert bm.mentions == ["456"], bm.mentions
    assert len(bm.attachments) == 2 and all(a.kind == "image" for a in bm.attachments)
    assert bm.sender.display_name == "GroupAlice"
    print("parse rich event OK:", repr(bm.text), [a.url for a in bm.attachments])


def test_parse_rejects():
    print("\n--- 2: parse rejects junk ---")
    assert parse_group_event({"post_type": "notice"}, ROOM) is None
    assert parse_group_event(_group_event([]), ROOM) is None  # empty content
    assert (
        parse_group_event(_group_event([{"type": "face", "data": {"id": "1"}}]), ROOM)
        is None
    )
    print("parse rejection OK")


def test_build_segments():
    print("\n--- 3: build send segments ---")
    bm = BridgeMessage(
        platform=TG,
        room_key=ROOM,
        msg_id="1",
        sender=BridgeUser(TG, "u1", "Carol"),
        text="hello qq",
        attachments=[Attachment("image", data=b"PNGDATA")],
    )
    segs = build_send_segments(bm, reply_to_native_id="999")
    assert segs[0] == {"type": "reply", "data": {"id": "999"}}, segs[0]
    assert segs[1]["type"] == "text"
    assert segs[1]["data"]["text"] == "[TG] Carol:\nhello qq", segs[1]
    assert segs[2]["type"] == "image"
    assert segs[2]["data"]["file"].startswith("base64://"), segs[2]
    print("build segments OK:", segs[1]["data"]["text"], "| img=base64")


async def test_adapter_inbound():
    print("\n--- 4: adapter inbound (filter + loop prevention + emit) ---")
    idmap = IdMap(":memory:")
    await idmap.open()

    async def _no_sleep(_):
        return

    router = Router(idmap, sleep=_no_sleep)
    tg = MockAdapter(TG, ROOM)
    qq = QQAdapter(ROOM, GROUP_ID, SELF_UIN, FakeQQTransport())
    router.register(tg)
    router.register(qq)
    await router.start()

    # wrong group -> dropped
    await qq.handle_event(
        _group_event([{"type": "text", "data": {"text": "x"}}], message_id=1)
        | {"group_id": 999}
    )
    await router.join()
    assert len(tg.sent) == 0, "wrong-group message leaked"

    # self message -> dropped (loop prevention)
    await qq.handle_event(
        _group_event(
            [{"type": "text", "data": {"text": "echo"}}], user_id=SELF_UIN, message_id=2
        )
    )
    await router.join()
    assert len(tg.sent) == 0, "self message leaked"

    # real message -> emitted to TG
    await qq.handle_event(
        _group_event([{"type": "text", "data": {"text": "real"}}], message_id=3)
    )
    await router.join()
    assert len(tg.sent) == 1, tg.sent
    emitted = tg.sent[0][1]
    assert emitted.platform == QQ and emitted.text == "real"
    await router.stop()
    await idmap.close()
    print("adapter inbound OK")


async def test_adapter_send():
    print("\n--- 5: adapter send via transport ---")
    transport = FakeQQTransport()
    qq = QQAdapter(ROOM, GROUP_ID, SELF_UIN, transport)
    msg = BridgeMessage(
        platform=TG,
        room_key=ROOM,
        msg_id="7",
        sender=BridgeUser(TG, "u", "Dave"),
        text="from tg",
    )
    native_ids = await qq.send(msg, reply_to_native_id=None)
    assert native_ids == ["1001"], native_ids
    action, params = transport.calls[0]
    assert action == "send_group_msg" and params["group_id"] == GROUP_ID
    assert params["message"][0]["data"]["text"] == "[TG] Dave:\nfrom tg"
    print("adapter send OK: native_ids=", native_ids)


async def main():
    test_parse_rich_event()
    test_parse_rejects()
    test_build_segments()
    await test_adapter_inbound()
    await test_adapter_send()
    print("\nALL QQ CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
