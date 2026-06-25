"""Adapter base class and the mock adapters that stand in for QQ / Matrix.

An adapter owns one platform's side of one logical room. It:
  * translates inbound platform events into BridgeMessage and calls `_emit`,
  * renders an outbound BridgeMessage via `send`, returning the new native id.

Loop prevention lives in each adapter: it must drop messages authored by its
own bridge identity *before* emitting, otherwise the message we just posted
would echo back and fan out forever.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from antares_bot.bot_logging import get_logger

from modules.tri_lug_utils.bridge_message import Attachment, BridgeMessage, BridgeUser

if TYPE_CHECKING:
    from modules.tri_lug_utils.router import Router

_LOGGER = get_logger(__name__)


class BaseAdapter(abc.ABC):
    platform: str = "base"

    def __init__(self, room_key: str) -> None:
        self.room_key = room_key
        self.router: "Router" | None = None

    def bind(self, router: "Router") -> None:
        self.router = router

    async def start(self) -> None:
        """Open the platform connection / start background consumers."""

    async def stop(self) -> None:
        """Tear down the platform connection."""

    @abc.abstractmethod
    async def send(
        self, msg: BridgeMessage, reply_to_native_id: str | None
    ) -> str | None:
        """Render `msg` into this platform. `reply_to_native_id` is this
        platform's native id to reply to (already resolved by the Router), or
        None. Returns the native id of the sent message, or None on failure."""

    async def set_pin(self, native_id: str, pinned: bool) -> None:
        """Pin (or unpin) `native_id` — this platform's native id, already
        resolved by the Router. Platforms that don't model pinning leave this a
        no-op (the default)."""

    async def _emit(self, msg: BridgeMessage) -> None:
        # Enqueue and return: the Router's msg-in queue decouples receipt (which
        # must ack promptly) from the paced outbound delivery.
        if self.router is not None:
            self.router.submit(msg)


class MockAdapter(BaseAdapter):
    """Logging-only stand-in for a not-yet-wired transport (QQ / Matrix).

    `send` records the render and returns a synthetic incrementing native id so
    the IdMap / reply-resolution flow can be exercised end to end. Tests drive
    inbound traffic via `simulate_incoming`.
    """

    def __init__(self, platform: str, room_key: str) -> None:
        super().__init__(room_key)
        self.platform = platform
        self._counter = 0
        # (native_id, msg, reply_to_native_id) of everything we were asked to send.
        self.sent: list[tuple[str, BridgeMessage, str | None]] = []

    async def send(
        self, msg: BridgeMessage, reply_to_native_id: str | None
    ) -> str | None:
        self._counter += 1
        native_id = f"{self.platform}-out-{self._counter}"
        reply_note = f" (reply→{reply_to_native_id})" if reply_to_native_id else ""
        attach_note = (
            f" +[{','.join(a.kind for a in msg.attachments)}]"
            if msg.attachments
            else ""
        )
        _LOGGER.info(
            "[%s.send] room=%s id=%s <- [%s] %s: %r%s%s",
            self.platform,
            self.room_key,
            native_id,
            msg.platform,
            msg.sender.display_name,
            msg.text,
            reply_note,
            attach_note,
        )
        self.sent.append((native_id, msg, reply_to_native_id))
        return native_id

    async def simulate_incoming(
        self,
        sender_name: str,
        text: str = "",
        msg_id: str | None = None,
        reply_to_native_id: str | None = None,
        attachments: list[Attachment] | None = None,
        ts: float | None = None,
    ) -> str:
        """Test hook: pretend a user on this platform posted a message. Returns
        the native id assigned to the simulated message. `ts` overrides the
        receipt timestamp so staleness handling can be exercised."""
        self._counter += 1
        mid = msg_id or f"{self.platform}-in-{self._counter}"
        bm = BridgeMessage(
            platform=self.platform,
            room_key=self.room_key,
            msg_id=mid,
            sender=BridgeUser(
                self.platform, f"{self.platform}:{sender_name}", sender_name
            ),
            text=text,
            reply_to_msg_id=reply_to_native_id,
            attachments=attachments or [],
        )
        if ts is not None:
            bm.ts = ts
        await self._emit(bm)
        return mid
