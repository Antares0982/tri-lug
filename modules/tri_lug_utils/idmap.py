"""Cross-platform message id mapping.

Every message that gets bridged is one *logical* message with several native
ids (one per platform it appears on). They are stored as rows sharing a
`logical_id`, so a reply that references a native id on platform A can be
re-pointed at the corresponding native id on platform B.

Backed by aiosqlite (already a dependency of antares_bot). Use `":memory:"` as
`db_path` for tests.
"""

from __future__ import annotations

import asyncio
import os
import time
import aiosqlite

from antares_bot.bot_logging import get_logger

_LOGGER = get_logger(__name__)


class IdMap:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # Serializes the read-existing -> allocate -> insert sequence in link()
        # so two messages fanning out concurrently can't grab the same fresh
        # logical_id and get wrongly merged into one logical message.
        self._link_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._db_path != ":memory:":
            parent = os.path.dirname(self._db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS msg_link (
                logical_id INTEGER NOT NULL,
                platform   TEXT    NOT NULL,
                native_id  TEXT    NOT NULL,
                created_at REAL    NOT NULL,
                PRIMARY KEY (platform, native_id)
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_link_logical ON msg_link(logical_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_link_created ON msg_link(created_at)"
        )
        await self._db.commit()
        _LOGGER.info("[tri_lug] IdMap opened at %s", self._db_path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def link(self, native_ids: dict[str, str]) -> int:
        """Link several ``platform -> native_id`` pairs into one logical
        message. If any pair is already known, its logical group is reused so
        later fan-outs accrete onto the same logical message."""
        assert self._db is not None
        async with self._link_lock:
            logical_id: int | None = None
            for platform, native_id in native_ids.items():
                existing = await self._logical_of(platform, native_id)
                if existing is not None:
                    logical_id = existing
                    break
            if logical_id is None:
                logical_id = await self._next_logical_id()
            now = time.time()
            await self._db.executemany(
                "INSERT OR IGNORE INTO msg_link(logical_id, platform, native_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                [(logical_id, p, n, now) for p, n in native_ids.items()],
            )
            await self._db.commit()
            return logical_id

    async def purge_old(self, max_age_seconds: float = 24 * 3600) -> int:
        """Drop link rows older than ``max_age_seconds`` (called hourly). Returns
        the number of rows removed. Replies to a purged message simply fall back
        to a plain (non-reply) message, which is the accepted degradation."""
        assert self._db is not None
        cutoff = time.time() - max_age_seconds
        cur = await self._db.execute(
            "DELETE FROM msg_link WHERE created_at < ?", (cutoff,)
        )
        await self._db.commit()
        removed = cur.rowcount
        if removed:
            _LOGGER.debug("[tri_lug] IdMap purged %d stale link rows", removed)
        return removed

    async def native_id_for(
        self, src_platform: str, src_native_id: str, target_platform: str
    ) -> str | None:
        """Return the `target_platform` native id of the same logical message as
        ``(src_platform, src_native_id)``, or None if unknown."""
        assert self._db is not None
        logical_id = await self._logical_of(src_platform, src_native_id)
        if logical_id is None:
            return None
        cur = await self._db.execute(
            "SELECT native_id FROM msg_link WHERE logical_id = ? AND platform = ?",
            (logical_id, target_platform),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def _logical_of(self, platform: str, native_id: str) -> int | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT logical_id FROM msg_link WHERE platform = ? AND native_id = ?",
            (platform, native_id),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else None

    async def _next_logical_id(self) -> int:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT COALESCE(MAX(logical_id), 0) + 1 FROM msg_link"
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 1
