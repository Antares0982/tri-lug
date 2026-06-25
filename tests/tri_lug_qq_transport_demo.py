"""Broker-free test of the alice-side RabbitMQ QQTransport.

Stubs the actual AMQP publish (`_send`) so the echo<->future RPC correlation and
the inbound event routing can be exercised without a running RabbitMQ:

  1. call_action publishes a `qq.action` and resolves when the matching
     `qq.action_resp` (same echo) arrives -> returns the response data.
  2. a `qq.event` dispatched by the transport reaches QQAdapter.handle_event and
     fans out through the Router.
  3. QQAdapter.send end-to-end over the stubbed transport returns the message_id.

Run:  python -m tests.tri_lug_qq_transport_demo
"""

import asyncio

from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import QQ, TG, BridgeMessage, BridgeUser
from modules.tri_lug_utils.idmap import IdMap
from modules.tri_lug_utils.qq_adapter import QQAdapter
from modules.tri_lug_utils.qq_rabbitmq import (
    RK_ACTION,
    RK_AVATAR_REQ,
    RabbitMQQQTransport,
)
from modules.tri_lug_utils.router import Router

ROOM = "default"
GROUP_ID = 222
SELF_UIN = 10000


def _make_transport():
    """A transport wired for tests: no real connection; `_send` immediately
    fakes a NapCat action response so call_action resolves."""
    t = RabbitMQQQTransport(
        host="x",
        port=0,
        user="",
        password="",
        vhost="/",
        cafile="",
        certfile="",
        keyfile="",
    )
    t._exchange = object()  # sentinel so call_action doesn't early-return
    t.published = []

    async def fake_send(routing_key, payload):
        t.published.append((routing_key, payload))
        if routing_key == RK_ACTION:
            await t._dispatch(
                {
                    "status": "ok",
                    "retcode": 0,
                    "echo": payload["echo"],
                    "data": {"message_id": 555},
                }
            )
        elif routing_key == RK_AVATAR_REQ:
            # No avatar bytes in this stub; resolve the RPC promptly so the
            # background fetch doesn't linger.
            await t._dispatch({"echo": payload["echo"]})

    t._send = fake_send
    return t


def _group_event(text):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": 456,
        "message_id": 789,
        "sender": {"user_id": 456, "nickname": "Alice"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def main():
    transport = _make_transport()
    idmap = IdMap(":memory:")
    await idmap.open()

    async def _no_sleep(_):
        return

    router = Router(idmap, sleep=_no_sleep)
    tg = MockAdapter(TG, ROOM)
    qq = QQAdapter(ROOM, GROUP_ID, SELF_UIN, transport)
    transport.bind_adapter(qq)
    router.register(tg)
    router.register(qq)
    await router.start()

    print("\n--- 1: call_action RPC correlation ---")
    data = await transport.call_action("get_login_info", {})
    assert data == {"message_id": 555}, data
    assert transport.published[0][0] == RK_ACTION
    assert transport.published[0][1]["echo"].startswith("alice-")
    print("call_action resolved by matching echo OK")

    print("\n--- 2: inbound event -> handle_event -> router ---")
    await transport._dispatch(_group_event("hello from qq"))
    await router.join()
    assert len(tg.sent) == 1, tg.sent
    assert tg.sent[0][1].platform == QQ and tg.sent[0][1].text == "hello from qq"
    print("event routed to TG OK")

    print("\n--- 3: QQAdapter.send over transport ---")
    msg = BridgeMessage(
        platform=TG,
        room_key=ROOM,
        msg_id="1",
        sender=BridgeUser(TG, "u", "Dave"),
        text="from tg",
    )
    native_ids = await qq.send(msg, reply_to_native_id=None)
    assert native_ids == ["555"], native_ids
    action_payload = transport.published[-1][1]
    assert action_payload["action"] == "send_group_msg"
    assert action_payload["params"]["group_id"] == GROUP_ID
    print("QQAdapter.send returned native ids", native_ids)

    await router.stop()
    await idmap.close()
    print("\nALL QQ-TRANSPORT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
