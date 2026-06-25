"""The bridge hub: a two-stage queue pipeline between platforms.

A message entering from platform A flows through:

  msg-in (per source)  ->  fan  ->  msg-out (per target)

  * msg-in: one queue per source platform. `submit` only enqueues and returns,
    so the caller (an adapter, or the RabbitMQ consumer) can ack immediately;
    the slow paced delivery happens off to the side. A single worker per source
    preserves that source's send order.
  * fan: at the hand-off it drops messages whose timestamp is older than
    `stale_seconds` (a backlog replayed after downtime / a message stuck in
    RabbitMQ), then links the origin id and enqueues a copy into each target's
    msg-out queue.
  * msg-out: one queue per target platform, drained by a worker that keeps at
    least `send_gap_seconds` between consecutive real sends to that target.
    Once a message is in msg-out it is never re-checked for staleness; the
    pacing delay does not count against it.

The native ids produced by delivery (plus the origin's own id) are linked in the
IdMap, so a later reply that references any one of them can be re-pointed at the
right native id on each target platform.

Clocks and sleep are injectable so the pipeline is unit-testable without real
time passing.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from antares_bot.bot_logging import get_logger

if TYPE_CHECKING:
    from modules.tri_lug_utils.adapters import BaseAdapter
    from modules.tri_lug_utils.bridge_message import BridgeMessage
    from modules.tri_lug_utils.idmap import IdMap

_LOGGER = get_logger(__name__)

# Plaintext control commands. Recognised on every platform: a bare message
# equal to one of these toggles the runtime pause instead of being bridged.
STOP_COMMAND = "/stop_bridge"
START_COMMAND = "/start_bridge"


def parse_control_command(text: str) -> str | None:
    """Return the canonical control command if `text` is exactly one (ignoring
    surrounding whitespace and an optional ``@botname`` suffix as Telegram
    appends in groups), else None."""
    stripped = (text or "").strip()
    head = stripped.split("@", 1)[0] if stripped.startswith("/") else stripped
    if head in (STOP_COMMAND, START_COMMAND):
        return head
    return None


class Router:
    def __init__(
        self,
        idmap: "IdMap",
        *,
        dry_run: bool = False,
        stale_seconds: float = 60.0,
        send_gap_seconds: float = 3.0,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._idmap = idmap
        self._dry_run = dry_run
        self._paused = False  # runtime pause (/stop_bridge); resets on restart
        self._stale = stale_seconds
        self._gap = send_gap_seconds
        self._wall = wall_clock
        self._mono = mono_clock
        self._sleep = sleep
        self._adapters: dict[str, "BaseAdapter"] = {}
        self._in_queues: dict[str, "asyncio.Queue[BridgeMessage]"] = {}
        self._out_queues: dict[str, "asyncio.Queue[BridgeMessage]"] = {}
        self._last_send: dict[str, float] = {}  # target platform -> mono ts
        self._workers: list[asyncio.Task] = []
        self._started = False

    def register(self, adapter: "BaseAdapter") -> None:
        adapter.bind(self)
        self._adapters[adapter.platform] = adapter
        self._in_queues[adapter.platform] = asyncio.Queue()
        self._out_queues[adapter.platform] = asyncio.Queue()

    @property
    def adapters(self) -> dict[str, "BaseAdapter"]:
        return self._adapters

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for platform in self._in_queues:
            self._workers.append(
                asyncio.create_task(
                    self._in_worker(platform), name=f"trilug-in-{platform}"
                )
            )
        for platform in self._out_queues:
            self._workers.append(
                asyncio.create_task(
                    self._out_worker(platform), name=f"trilug-out-{platform}"
                )
            )
        _LOGGER.info(
            "[router] started (dry_run=%s, stale=%.0fs, gap=%.1fs)",
            self._dry_run,
            self._stale,
            self._gap,
        )

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    async def join(self) -> None:
        """Block until every queued message has been fully processed. Drains
        msg-in first (which feeds msg-out), then msg-out. Test/shutdown helper."""
        for q in self._in_queues.values():
            await q.join()
        for q in self._out_queues.values():
            await q.join()

    # -------------------------------------------------------------- pause state
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> bool:
        """Toggle the runtime pause. Returns True if the state actually changed.
        While paused, inbound messages and pins are dropped at intake (the
        control commands themselves are handled by adapters before they reach
        here, so /start_bridge still works)."""
        changed = self._paused != paused
        self._paused = paused
        if changed:
            _LOGGER.info("[router] bridge %s", "paused" if paused else "resumed")
        return changed

    # ----------------------------------------------------------------- intake
    def submit(self, msg: "BridgeMessage") -> None:
        """Enqueue an inbound message and return immediately. Safe to call right
        before acking the underlying transport message."""
        if self._paused:
            return
        q = self._in_queues.get(msg.platform)
        if q is None:
            _LOGGER.warning("[router] no msg-in queue for platform %s", msg.platform)
            return
        q.put_nowait(msg)

    # ------------------------------------------------------------------- pins
    async def pin(self, src_platform: str, src_native_id: str) -> None:
        """Mirror a pin of `(src_platform, src_native_id)` onto every other
        platform in the same room, resolving each target's native id via the
        IdMap. Platforms with no native id for the logical message, or that
        don't model pinning, are skipped."""
        if self._paused:
            return
        src = self._adapters.get(src_platform)
        for platform, adapter in self._adapters.items():
            if platform == src_platform or (src and adapter.room_key != src.room_key):
                continue
            target_id = await self._idmap.native_id_for(
                src_platform, src_native_id, platform
            )
            if target_id is None:
                continue
            try:
                await adapter.set_pin(target_id, True)
            except Exception:
                _LOGGER.exception("[router] %s pin failed", platform)

    # ------------------------------------------------------------- msg-in / fan
    async def _in_worker(self, platform: str) -> None:
        q = self._in_queues[platform]
        while True:
            msg = await q.get()
            try:
                await self._fan(msg)
            except Exception:
                _LOGGER.exception("[router] fan failed for message from %s", platform)
            finally:
                q.task_done()

    async def _fan(self, msg: "BridgeMessage") -> None:
        age = self._wall() - msg.ts
        if age > self._stale:
            _LOGGER.warning(
                "[router] dropping stale message from %s (age %.1fs > %.0fs): %r",
                msg.platform,
                age,
                self._stale,
                msg.text,
            )
            return

        targets = [
            a
            for p, a in self._adapters.items()
            if p != msg.platform and a.room_key == msg.room_key
        ]
        if not targets:
            return

        _LOGGER.debug(
            "[router] [%s] %s: %r -> %s",
            msg.platform,
            msg.sender.display_name,
            msg.text,
            [a.platform for a in targets],
        )

        # Seed the logical group with the origin id so each target's delivery
        # accretes onto the same logical message when it links its native id.
        await self._idmap.link({msg.platform: msg.msg_id})
        for target in targets:
            self._out_queues[target.platform].put_nowait(msg)

    # ---------------------------------------------------------------- msg-out
    async def _out_worker(self, platform: str) -> None:
        q = self._out_queues[platform]
        adapter = self._adapters[platform]
        while True:
            msg = await q.get()
            try:
                await self._deliver(adapter, msg)
            except Exception:
                _LOGGER.exception("[router] %s delivery failed", platform)
            finally:
                q.task_done()

    async def _deliver(self, adapter: "BaseAdapter", msg: "BridgeMessage") -> None:
        await self._pace(adapter.platform)

        if self._dry_run:
            _LOGGER.warning(
                "[router] DRY-RUN would send to %s <- [%s] %s: %r%s",
                adapter.platform,
                msg.platform,
                msg.sender.display_name,
                msg.text,
                f" +[{','.join(a.kind for a in msg.attachments)}]"
                if msg.attachments
                else "",
            )
            return

        reply_native: str | None = None
        if msg.reply_to_msg_id is not None:
            reply_native = await self._idmap.native_id_for(
                msg.platform, msg.reply_to_msg_id, adapter.platform
            )
        native_id = await adapter.send(msg, reply_native)
        if native_id is not None:
            await self._idmap.link(
                {msg.platform: msg.msg_id, adapter.platform: native_id}
            )

    async def _pace(self, platform: str) -> None:
        """Hold off until at least `send_gap_seconds` have passed since the last
        send to this target. The window is measured from send-start to
        send-start, so a slow upload naturally counts toward the gap."""
        last = self._last_send.get(platform)
        if last is not None:
            wait = self._gap - (self._mono() - last)
            if wait > 0:
                await self._sleep(wait)
        self._last_send[platform] = self._mono()
