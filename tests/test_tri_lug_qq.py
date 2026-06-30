"""Network-free tests for the QQ adapter and OneBot translation.

Covers, with no RabbitMQ / NapCat:
  1. parse_group_event: reply + at + text + image + mface + face -> BridgeMessage
  2. parse rejects non-message / empty events
  3. build_send_segments: reply prepend, identity header, base64 image
  4. QQAdapter.handle_event: group filter + self-message loop prevention + emit
  5. QQAdapter.send via a fake transport: returns native id, builds right action
"""

from __future__ import annotations

import json

from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import (
    QQ,
    TG,
    Attachment,
    BridgeMessage,
    BridgeUser,
)
from modules.tri_lug_utils.onebot import (
    build_send_segments,
    parse_card_json,
    parse_group_event,
)
from modules.tri_lug_utils.qq_adapter import QQAdapter
from modules.tri_lug_utils.router import Router

from conftest import (
    GROUP_ID,
    ROOM,
    SELF_UIN,
    FakeQQTransport,
    make_group_event,
)


def _card_segment(payload: dict) -> dict:
    """Wrap a card payload as a OneBot ``json`` segment (NapCat array form)."""
    return {"type": "json", "data": {"data": json.dumps(payload, ensure_ascii=False)}}


_BILIBILI_CARD = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "title": "哔哩哔哩",
            "desc": "2026国产末世科幻剧《我们生活在南京》首曝预告！",
            "qqdocurl": "https://b23.tv/JrNSje4?share_medium=android&share_source=qq",
        }
    },
}
_ZHIHU_CARD = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "title": "知乎",
            "desc": "OpenCode Go多个账号使用GLM-5.2",
            "qqdocurl": "https://zhuanlan.zhihu.com/p/2052119367018717420?share_code=x&utm_psn=y",
        }
    },
}
_WEIXIN_CARD = {
    "app": "com.tencent.tuwen.lua",
    "meta": {
        "news": {
            "title": "粽叶飘香迎端午，三室一厅聚温情 | 智能软件与工程学院…",
            "jumpUrl": "https://mp.weixin.qq.com/s/FPhdErv5guPqn2OG7ZsPBA",
        }
    },
}


def test_parse_rich_event():
    ev = make_group_event(
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


def test_parse_rejects():
    assert parse_group_event({"post_type": "notice"}, ROOM) is None
    assert parse_group_event(make_group_event([]), ROOM) is None  # empty content
    assert (
        parse_group_event(
            make_group_event([{"type": "face", "data": {"id": "1"}}]), ROOM
        )
        is None
    )


def test_parse_card_bilibili():
    card = parse_card_json(json.dumps(_BILIBILI_CARD))
    assert card is not None
    assert card.kind == "bilibili" and card.needs_resolve
    assert card.title == "2026国产末世科幻剧《我们生活在南京》首曝预告！"
    # tracking query stripped, but still the b23.tv short link (resolved later).
    assert card.url == "https://b23.tv/JrNSje4", card.url


def test_parse_card_zhihu():
    card = parse_card_json(json.dumps(_ZHIHU_CARD))
    assert card is not None
    assert card.kind == "zhihu" and not card.needs_resolve
    assert card.title == "OpenCode Go多个账号使用GLM-5.2"
    assert card.url == "https://zhuanlan.zhihu.com/p/2052119367018717420", card.url


def test_parse_card_weixin():
    card = parse_card_json(json.dumps(_WEIXIN_CARD))
    assert card is not None
    assert card.kind == "weixin" and not card.needs_resolve
    assert card.url == "https://mp.weixin.qq.com/s/FPhdErv5guPqn2OG7ZsPBA", card.url


def test_parse_card_cq_escaped():
    """NapCat may leave CQ HTML entities in the payload; _load_card unescapes."""
    raw = json.dumps(_WEIXIN_CARD).replace(",", "&#44;")
    card = parse_card_json(raw)
    assert card is not None and card.kind == "weixin"


def test_parse_card_unknown():
    unknown = {
        "app": "com.x",
        "meta": {"detail_1": {"qqdocurl": "https://example.com/a"}},
    }
    assert parse_card_json(json.dumps(unknown)) is None
    assert parse_card_json("not json") is None
    assert parse_card_json(None) is None


def test_card_in_group_event():
    """A card segment becomes a '{title}\\n{url}' BridgeMessage."""
    bm = parse_group_event(make_group_event([_card_segment(_WEIXIN_CARD)]), ROOM)
    assert bm is not None
    assert bm.text == (
        "粽叶飘香迎端午，三室一厅聚温情 | 智能软件与工程学院…\n"
        "https://mp.weixin.qq.com/s/FPhdErv5guPqn2OG7ZsPBA"
    ), repr(bm.text)


async def test_adapter_resolves_b23(idmap):
    """A bilibili card's b23.tv short link is expanded before forwarding."""

    async def _no_sleep(_):
        return

    async def fake_resolver(short):
        assert short == "https://b23.tv/JrNSje4"
        return "https://www.bilibili.com/video/BV1xx"

    router = Router(idmap, sleep=_no_sleep)
    tg = MockAdapter(TG, ROOM)
    qq = QQAdapter(
        ROOM, GROUP_ID, SELF_UIN, FakeQQTransport(), link_resolver=fake_resolver
    )
    router.register(tg)
    router.register(qq)
    await router.start()

    await qq.handle_event(make_group_event([_card_segment(_BILIBILI_CARD)]))
    await router.join()

    assert len(tg.sent) == 1, tg.sent
    assert tg.sent[0][1].text == (
        "2026国产末世科幻剧《我们生活在南京》首曝预告！\n"
        "https://www.bilibili.com/video/BV1xx"
    ), repr(tg.sent[0][1].text)
    await router.stop()


def test_build_segments():
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


async def test_adapter_inbound(idmap):
    """group filter + self-message loop prevention + emit."""

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
        make_group_event([{"type": "text", "data": {"text": "x"}}], message_id=1)
        | {"group_id": 999}
    )
    await router.join()
    assert len(tg.sent) == 0, "wrong-group message leaked"

    # self message -> dropped (loop prevention)
    await qq.handle_event(
        make_group_event(
            [{"type": "text", "data": {"text": "echo"}}], user_id=SELF_UIN, message_id=2
        )
    )
    await router.join()
    assert len(tg.sent) == 0, "self message leaked"

    # real message -> emitted to TG
    await qq.handle_event(
        make_group_event([{"type": "text", "data": {"text": "real"}}], message_id=3)
    )
    await router.join()
    assert len(tg.sent) == 1, tg.sent
    emitted = tg.sent[0][1]
    assert emitted.platform == QQ and emitted.text == "real"
    await router.stop()


async def test_adapter_send():
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
