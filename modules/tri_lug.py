"""Telegram ↔ QQ ↔ Matrix message bridge.

Stage: the Telegram adapter is live; QQ and Matrix are MockAdapters that log
their outbound renders and can simulate inbound messages. The Router + IdMap
spine is real, so once the QQ (NapCat/RabbitMQ) and Matrix (appservice)
transports are written they drop in by replacing the two mocks.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from antares_bot.bot_logging import get_logger
from antares_bot.framework import command_callback_wrapper, msg_handle_wrapper
from antares_bot.module_base import TelegramBotModuleBase

from bot_cfg import TriLugConfig
from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import MATRIX, QQ
from modules.tri_lug_utils.idmap import IdMap
from modules.tri_lug_utils.matrix_adapter import MatrixAdapter
from modules.tri_lug_utils.qq_adapter import QQAdapter
from modules.tri_lug_utils.qq_rabbitmq import RabbitMQQQTransport
from modules.tri_lug_utils.router import Router
from modules.tri_lug_utils.tg_adapter import TelegramAdapter

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application

    from antares_bot.context import RichCallbackContext

_LOGGER = get_logger(__name__)

# Single bridged room for now; multi-room support is a later config concern.
ROOM_KEY = "default"


class TriLug(TelegramBotModuleBase):
    def do_init(self) -> None:
        cfg = TriLugConfig
        self._enabled = getattr(cfg, "ENABLED", True)
        self._idmap = IdMap(cfg.DB_PATH)
        self._router = Router(self._idmap, dry_run=getattr(cfg, "DRY_RUN", False))
        self._purge_task: asyncio.Task | None = None
        self._tg = TelegramAdapter(ROOM_KEY, cfg.TG_CHAT_ID)
        self._qq = self._build_qq_adapter(cfg)
        self._matrix = self._build_matrix_adapter(cfg)
        for adapter in (self._tg, self._qq, self._matrix):
            self._router.register(adapter)

    def _build_qq_adapter(self, cfg):
        """Real QQAdapter (RabbitMQ → relay → NapCat) when configured, else a
        MockAdapter so the bot still runs before the QQ side is wired."""
        if not getattr(cfg, "QQ_ENABLED", False):
            return MockAdapter(QQ, ROOM_KEY)
        transport = RabbitMQQQTransport(
            host=cfg.RMQ_HOST,
            port=cfg.RMQ_PORT,
            user=cfg.RMQ_USER,
            password=cfg.RMQ_PASS,
            vhost=cfg.RMQ_VHOST,
            cafile=cfg.RMQ_CAFILE,
            certfile=cfg.RMQ_CERTFILE,
            keyfile=cfg.RMQ_KEYFILE,
            exchange=cfg.QQ_EXCHANGE,
        )
        qq = QQAdapter(ROOM_KEY, cfg.QQ_GROUP_ID, cfg.QQ_SELF_UIN, transport)
        transport.bind_adapter(qq)
        return qq

    def _build_matrix_adapter(self, cfg):
        """Real MatrixAdapter (appservice puppeting) when configured, else a
        MockAdapter so the bot runs before the appservice token is available."""
        if not getattr(cfg, "MATRIX_ENABLED", False):
            return MockAdapter(MATRIX, ROOM_KEY)
        return MatrixAdapter(
            ROOM_KEY,
            homeserver=cfg.MATRIX_HS_URL,
            server_name=cfg.MATRIX_SERVER_NAME,
            as_id=cfg.MATRIX_AS_ID,
            as_token=cfg.MATRIX_AS_TOKEN,
            hs_token=cfg.MATRIX_HS_TOKEN,
            bot_localpart=cfg.MATRIX_BOT_LOCALPART,
            ghost_prefix=cfg.MATRIX_GHOST_PREFIX,
            room_id=cfg.MATRIX_ROOM_ID,
            listen_host=cfg.MATRIX_LISTEN_HOST,
            listen_port=cfg.MATRIX_LISTEN_PORT,
        )

    async def post_init(self, app: "Application") -> None:
        if not self._enabled:
            _LOGGER.info("[tri_lug] disabled via config, not starting bridge")
            return
        self._tg.attach_app(app)
        await self._idmap.open()
        for adapter in (self._tg, self._qq, self._matrix):
            await adapter.start()
        await self._router.start()
        self._purge_task = asyncio.create_task(self._purge_loop())
        _LOGGER.info(
            "[tri_lug] bridge started (tg=%s, qq=%s, matrix=%s, dry_run=%s)",
            TriLugConfig.TG_CHAT_ID,
            "real" if getattr(TriLugConfig, "QQ_ENABLED", False) else "mock",
            "real" if getattr(TriLugConfig, "MATRIX_ENABLED", False) else "mock",
            getattr(TriLugConfig, "DRY_RUN", False),
        )

    async def _purge_loop(self) -> None:
        """Hourly: drop IdMap rows older than its TTL (24h)."""
        while True:
            try:
                await asyncio.sleep(3600)
                await self._idmap.purge_old()
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("[tri_lug] idmap purge failed")

    def mark_handlers(self):
        # When the bridge is disabled the module is fully inert — no handlers,
        # so /stop_bridge and /start_bridge are not registered either.
        if not self._enabled:
            return []
        # Command handlers first so a bare "/stop_bridge" is consumed as a
        # command (one handler per group) instead of also being bridged.
        return [
            command_callback_wrapper(self.stop_bridge),
            command_callback_wrapper(self.start_bridge),
            msg_handle_wrapper(filters=self._tg.matches)(self._on_tg_message),
        ]

    async def _on_tg_message(self, update: "Update", context: "RichCallbackContext"):
        if not self._enabled:
            return
        await self._tg.on_update(update)

    async def stop_bridge(self, update: "Update", context: "RichCallbackContext"):
        """暂停消息桥接，直到进程重启或 /start_bridge。"""
        await self._toggle_bridge(update, paused=True)

    async def start_bridge(self, update: "Update", context: "RichCallbackContext"):
        """恢复被 /stop_bridge 暂停的消息桥接。"""
        await self._toggle_bridge(update, paused=False)

    async def _toggle_bridge(self, update: "Update", *, paused: bool) -> None:
        if not self._enabled:
            return
        chat = update.effective_chat
        if chat is None or chat.id != TriLugConfig.TG_CHAT_ID:
            return
        self._router.set_paused(paused)

    async def do_stop(self) -> None:
        if self._purge_task is not None:
            self._purge_task.cancel()
            self._purge_task = None
        await self._router.stop()
        for adapter in (self._tg, self._qq, self._matrix):
            await adapter.stop()
        await self._idmap.close()
