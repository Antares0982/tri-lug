"""Broker-free tests of the alice-side RabbitMQ QQTransport.

The actual AMQP publish (`_send`) is stubbed (via the `make_qq_transport`
fixture) so the echo<->future RPC correlation and inbound event routing can be
exercised without a running RabbitMQ.
"""

from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import QQ, TG, BridgeMessage, BridgeUser
from modules.tri_lug_utils.qq_adapter import QQAdapter
from modules.tri_lug_utils.qq_rabbitmq import RK_ACTION
from modules.tri_lug_utils.router import Router

from conftest import GROUP_ID, ROOM, SELF_UIN, make_text_event


async def test_call_action_rpc_correlation(make_qq_transport):
    """call_action publishes a qq.action and resolves on the matching echo."""
    transport = make_qq_transport()
    data = await transport.call_action("get_login_info", {})
    assert data == {"message_id": 555}, data
    assert transport.published[0][0] == RK_ACTION
    assert transport.published[0][1]["echo"].startswith("alice-")


async def test_inbound_event_routed(make_qq_transport, idmap):
    """A qq.event dispatched by the transport reaches handle_event and fans out."""
    transport = make_qq_transport()

    async def _no_sleep(_):
        return

    router = Router(idmap, sleep=_no_sleep)
    tg = MockAdapter(TG, ROOM)
    qq = QQAdapter(ROOM, GROUP_ID, SELF_UIN, transport)
    transport.bind_adapter(qq)
    router.register(tg)
    router.register(qq)
    await router.start()

    await transport._dispatch(make_text_event("hello from qq"))
    await router.join()
    assert len(tg.sent) == 1, tg.sent
    assert tg.sent[0][1].platform == QQ and tg.sent[0][1].text == "hello from qq"
    await router.stop()


async def test_adapter_send_over_transport(make_qq_transport):
    """QQAdapter.send end-to-end over the stubbed transport returns the id."""
    transport = make_qq_transport()
    qq = QQAdapter(ROOM, GROUP_ID, SELF_UIN, transport)
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
