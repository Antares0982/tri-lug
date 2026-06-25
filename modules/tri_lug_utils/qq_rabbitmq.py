"""RabbitMQ implementation of QQTransport (alice side).

Pairs with the standalone `tri-lug-qq-relay` running on the QQ machine. Both
sides dial out to the broker (machine B can't accept inbound). Exchange
`tri_lug` (topic):

  * consume `qq.event`       -> QQAdapter.handle_event
  * consume `qq.action_resp` -> resolve the pending call_action future (by echo)
  * publish `qq.action`      <- call_action

`call_action` is a request/response RPC: it publishes an action tagged with a
unique `echo` and awaits the matching response. Reuses the aio_pika/mTLS pattern
from modules/hermes.py.

Single-instance assumption: events are consumed via an exclusive queue, so QQ
messages that arrive while alice is down are dropped (acceptable for v1).
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
from typing import TYPE_CHECKING

import aio_pika
from aio_pika import ExchangeType

from antares_bot.bot_logging import get_logger

from modules.tri_lug_utils.qq_adapter import QQTransport

if TYPE_CHECKING:
    from modules.tri_lug_utils.qq_adapter import QQAdapter

_LOGGER = get_logger(__name__)

RK_EVENT = "qq.event"
RK_ACTION = "qq.action"
RK_ACTION_RESP = "qq.action_resp"
# Avatar fetch RPC: alice asks the relay (machine B) for an avatar's raw bytes
# so it never has to reach the QQ avatar CDN itself. Separate from the OneBot
# action path because NapCat has no "give me avatar bytes" action.
RK_AVATAR_REQ = "qq.avatar_req"
RK_AVATAR_RESP = "qq.avatar_resp"


class RabbitMQQQTransport(QQTransport):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        vhost: str,
        cafile: str,
        certfile: str,
        keyfile: str,
        exchange: str = "tri_lug",
        action_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._vhost = vhost
        self._cafile = cafile
        self._certfile = certfile
        self._keyfile = keyfile
        self._exchange_name = exchange
        self._action_timeout = action_timeout

        self._adapter: "QQAdapter" | None = None
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo_seq = 0

    def bind_adapter(self, adapter: "QQAdapter") -> None:
        self._adapter = adapter

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(**self._connect_kwargs())
        self._channel = await self._connection.channel()
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, ExchangeType.TOPIC, durable=True
        )
        queue = await self._channel.declare_queue("", exclusive=True)
        await queue.bind(self._exchange, RK_EVENT)
        await queue.bind(self._exchange, RK_ACTION_RESP)
        await queue.bind(self._exchange, RK_AVATAR_RESP)
        await queue.consume(self._on_message)
        _LOGGER.info(
            "[qq.rmq] connected (%s:%s exchange=%s)",
            self._host,
            self._port,
            self._exchange_name,
        )

    async def stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                pass
            self._connection = None

    # ------------------------------------------------------------------- action
    async def call_action(self, action: str, params: dict) -> dict | None:
        if self._exchange is None:
            _LOGGER.warning("[qq.rmq] not connected, dropping action %s", action)
            return None
        result = await self._rpc(
            RK_ACTION, {"action": action, "params": params}, label=action
        )
        if isinstance(result, dict):
            return result.get("data")
        return None

    async def fetch_avatar(self, kind: str, target_id: str) -> bytes | None:
        """Ask the relay for an avatar's raw bytes (``kind`` = ``qq``,
        ``target_id`` = the uin). Returns the decoded bytes, or None on
        timeout / not-found. The relay reaches the avatar CDN on machine B."""
        if self._exchange is None:
            _LOGGER.warning("[qq.rmq] not connected, dropping avatar request")
            return None
        result = await self._rpc(
            RK_AVATAR_REQ, {"kind": kind, "id": str(target_id)}, label="avatar"
        )
        if not isinstance(result, dict):
            return None
        b64 = result.get("base64")
        if not b64:
            return None
        try:
            return base64.b64decode(b64)
        except ValueError, TypeError:
            return None

    async def _rpc(self, routing_key: str, payload: dict, *, label: str) -> dict | None:
        """Echo-tagged request/response: publish `payload` (with a fresh echo)
        and await the matching response dict, or None on timeout."""
        echo = self._next_echo()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._send(routing_key, {**payload, "echo": echo})
        try:
            return await asyncio.wait_for(fut, timeout=self._action_timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning("[qq.rmq] %s timed out (echo=%s)", label, echo)
            return None
        finally:
            self._pending.pop(echo, None)

    # ------------------------------------------------------------------ inbound
    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                data = json.loads(message.body)
            except ValueError, TypeError:
                _LOGGER.warning("[qq.rmq] non-JSON message dropped")
                return
            await self._dispatch(data)

    async def _dispatch(self, data: dict) -> None:
        """Route one decoded broker message. Split out from the aio_pika wrapper
        so the event/response correlation is unit-testable without a broker."""
        if data.get("post_type"):  # OneBot event
            if self._adapter is not None:
                await self._adapter.handle_event(data)
        elif "echo" in data:  # action / avatar RPC response
            fut = self._pending.get(str(data["echo"]))
            if fut is not None and not fut.done():
                fut.set_result(data)

    # ------------------------------------------------------------------ helpers
    async def _send(self, routing_key: str, payload: dict) -> None:
        assert self._exchange is not None
        await self._exchange.publish(
            aio_pika.Message(body=json.dumps(payload).encode()),
            routing_key=routing_key,
        )

    def _next_echo(self) -> str:
        self._echo_seq += 1
        return f"alice-{self._echo_seq}"

    def _connect_kwargs(self) -> dict:
        kw: dict = {"host": self._host, "port": self._port}
        if self._vhost and self._vhost != "/":
            kw["virtualhost"] = self._vhost
        if self._user:
            kw["login"] = self._user
            kw["password"] = self._password
        if self._cafile and self._certfile and self._keyfile:
            ctx = ssl.create_default_context(cafile=self._cafile)
            ctx.load_cert_chain(certfile=self._certfile, keyfile=self._keyfile)
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            kw["ssl"] = True
            kw["ssl_context"] = ctx
        return kw
