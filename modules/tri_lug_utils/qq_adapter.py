"""QQ adapter (OneBot 11 via NapCat).

Splits cleanly into:
  * translation — `onebot.py`, pure and unit-tested;
  * transport   — `QQTransport`, an abstract carrier of OneBot action requests
    to NapCat (the RabbitMQ implementation relays to the machine-B napcat
    bridge; see design doc §2). Inbound OneBot events are pushed in via
    `handle_event`.

Loop prevention: events authored by our own bot uin are dropped before emit.
"""

from __future__ import annotations

import abc
import asyncio
import time

from antares_bot.bot_logging import get_logger

from modules.tri_lug_utils.adapters import BaseAdapter
from modules.tri_lug_utils.bridge_message import QQ, BridgeMessage, BridgeUser
from modules.tri_lug_utils.onebot import (
    build_send_segments,
    describe_event,
    is_noise_event,
    parse_group_event,
)
from modules.tri_lug_utils.router import STOP_COMMAND, parse_control_command

_LOGGER = get_logger(__name__)

# How long a resolved group nickname / avatar is reused before refetching, so a
# member's renamed card or changed avatar eventually propagates. The negative
# TTL caps how often a missing avatar is retried.
_MEMBER_NAME_TTL = 300.0
_AVATAR_TTL = 6 * 3600.0
_AVATAR_NEG_TTL = 600.0


class QQTransport(abc.ABC):
    """Carries OneBot action requests to NapCat and returns the response `data`.

    Concrete implementations also deliver inbound OneBot events by calling the
    bound adapter's `handle_event`.
    """

    @abc.abstractmethod
    async def call_action(self, action: str, params: dict) -> dict | None:
        """Invoke a OneBot action, returning its response `data` (or None)."""

    async def fetch_avatar(self, kind: str, target_id: str) -> bytes | None:
        """Return raw avatar bytes for ``(kind, target_id)``, or None if the
        transport can't supply them (the default)."""
        return None

    async def start(self) -> None:  # pragma: no cover - trivial
        ...

    async def stop(self) -> None:  # pragma: no cover - trivial
        ...


class QQAdapter(BaseAdapter):
    platform = QQ

    def __init__(
        self,
        room_key: str,
        group_id: int,
        self_uin: int,
        transport: "QQTransport",
    ) -> None:
        super().__init__(room_key)
        self.group_id = group_id
        self.self_uin = self_uin
        self.transport = transport
        # uin -> (fetched_at, value) caches, so repeated messages from one member
        # don't re-hit NapCat (names) / the relay over RabbitMQ (avatars).
        self._member_names: dict[str, tuple[float, str]] = {}
        # uin -> (fetched_at, bytes|None); None records a recent miss so it
        # isn't retried on every message (see _AVATAR_NEG_TTL).
        self._avatars: dict[str, tuple[float, bytes | None]] = {}
        self._avatar_inflight: set[str] = set()

    async def start(self) -> None:
        await self.transport.start()

    async def stop(self) -> None:
        await self.transport.stop()

    async def handle_event(self, event: dict) -> None:
        """Entry point for inbound OneBot events (called by the transport)."""
        if str(event.get("group_id")) != str(self.group_id):
            return
        # loop prevention: never re-bridge the bridge bot's own messages
        if str(event.get("user_id")) == str(self.self_uin):
            return
        name_map = await self._resolve_at_names(event)
        bm = parse_group_event(event, self.room_key, self.self_uin, name_map)
        if bm is None:
            # In-scope group event with nothing we bridge (video/file/card/poke/
            # recall/...). Drop it silently when it's a known noise type,
            # otherwise record it clearly marked.
            if not is_noise_event(event):
                _LOGGER.warning(
                    "[QQ][log-only · not forwarded] %s", describe_event(event)
                )
            return
        # A bare /stop_bridge or /start_bridge toggles the runtime pause instead
        # of being bridged. Checked before delivery so /start_bridge works while
        # the bridge is paused.
        if not bm.attachments:
            cmd = parse_control_command(bm.text)
            if cmd is not None:
                if self.router is not None:
                    self.router.set_paused(cmd == STOP_COMMAND)
                return
        await self._attach_avatar(bm.sender)
        await self._emit(bm)

    # --------------------------------------------------------- name / avatar
    async def _resolve_at_names(self, event: dict) -> dict[str, str]:
        """Look up the group nickname of every `at` target in the event, since
        NapCat leaves the segment's `name` empty (bug: mentions otherwise render
        as `@QQ号`). Self-ats are skipped — they are dropped downstream."""
        segments = event.get("message")
        if not isinstance(segments, list):
            return {}
        names: dict[str, str] = {}
        for seg in segments:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            qq = str((seg.get("data") or {}).get("qq", ""))
            if not qq or qq == "all" or qq == str(self.self_uin) or qq in names:
                continue
            name = await self._member_name(qq)
            if name:
                names[qq] = name
        return names

    async def _member_name(self, uin: str) -> str | None:
        cached = self._member_names.get(uin)
        if cached is not None and time.monotonic() - cached[0] < _MEMBER_NAME_TTL:
            return cached[1]
        resp = await self.transport.call_action(
            "get_group_member_info",
            {"group_id": self.group_id, "user_id": int(uin), "no_cache": True},
        )
        if not resp:
            return cached[1] if cached else None
        name = resp.get("card") or resp.get("nickname")
        if name:
            self._member_names[uin] = (time.monotonic(), str(name))
            return str(name)
        return None

    async def _attach_avatar(self, sender: BridgeUser) -> None:
        """Tag the sender with a stable avatar key and, if already cached, the
        bytes, so a target adapter (Matrix ghost) can mirror the avatar. The
        actual fetch happens in the background — message delivery never waits on
        the avatar RPC / CDN; the bytes ride the next message once cached."""
        uin = sender.user_id
        if not uin:
            return
        sender.avatar_key = f"qq:{uin}"
        cached = self._avatars.get(uin)
        if cached is not None:
            age = time.monotonic() - cached[0]
            ttl = _AVATAR_TTL if cached[1] is not None else _AVATAR_NEG_TTL
            if cached[1] is not None:
                sender.avatar_data = cached[1]  # serve cached bytes immediately
            if age < ttl:
                return
        if uin not in self._avatar_inflight:
            self._avatar_inflight.add(uin)
            asyncio.create_task(self._refresh_avatar(uin))

    async def _refresh_avatar(self, uin: str) -> None:
        try:
            data = await self.transport.fetch_avatar("qq", uin)
            self._avatars[uin] = (time.monotonic(), data)
        except Exception:
            _LOGGER.warning("[qq] avatar fetch failed for %s", uin, exc_info=True)
        finally:
            self._avatar_inflight.discard(uin)

    async def send(
        self, msg: BridgeMessage, reply_to_native_id: str | None
    ) -> list[str]:
        segments = build_send_segments(msg, reply_to_native_id)
        resp = await self.transport.call_action(
            "send_group_msg", {"group_id": self.group_id, "message": segments}
        )
        if not resp:
            _LOGGER.warning("[qq.send] no response / send failed")
            return []
        mid = resp.get("message_id")
        return [str(mid)] if mid is not None else []
