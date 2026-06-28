"""Shared fixtures and helpers for the network-free tri_lug test suite.

Everything here is stubbed: no RabbitMQ / NapCat / Matrix / Telegram, and the
Router's `sleep` is injected so the 3s pacing is asserted via recorded durations
rather than by blocking.
"""

from __future__ import annotations

import logging

import pytest

from antares_bot.bot_logging import PikaHandler

from modules.tri_lug_utils.adapters import MockAdapter
from modules.tri_lug_utils.bridge_message import MATRIX, QQ, TG
from modules.tri_lug_utils.idmap import IdMap
from modules.tri_lug_utils.qq_adapter import QQTransport
from modules.tri_lug_utils.qq_rabbitmq import (
    RK_ACTION,
    RK_AVATAR_REQ,
    RabbitMQQQTransport,
)
from modules.tri_lug_utils.router import Router

ROOM = "default"
GROUP_ID = 222
SELF_UIN = 10000


def _strip_pika_log_handlers() -> None:
    """The suite is network-free, but in a dev environment with broker creds the
    antares_bot Pika logging handler is active: on every WARNING+ record it
    schedules a background RabbitMQ publish (`create_task(send_message(...))`)
    that never resolves without a broker and wedges the per-test event loop at
    teardown. Strip those handlers so test logging stays local."""
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            lg.handlers = [h for h in lg.handlers if not isinstance(h, PikaHandler)]


_strip_pika_log_handlers()


class FakeQQTransport(QQTransport):
    """Records action calls and hands back a synthetic message_id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._mid = 1000

    async def call_action(self, action: str, params: dict) -> dict | None:
        self.calls.append((action, params))
        self._mid += 1
        return {"message_id": self._mid}


def make_group_event(segments, user_id=456, message_id=789):
    """A OneBot11 group message event wrapping the given message segments."""
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": user_id,
        "message_id": message_id,
        "sender": {"user_id": user_id, "nickname": "Alice", "card": "GroupAlice"},
        "message": segments,
    }


def make_text_event(text):
    """A minimal single-text-segment group event (no card on the sender)."""
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": 456,
        "message_id": 789,
        "sender": {"user_id": 456, "nickname": "Alice"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


@pytest.fixture
async def make_bridge():
    """Factory yielding (router, idmap, sleeps, {platform: MockAdapter}).

    A fresh in-memory IdMap + Router (sleep stubbed) wired to three mock
    adapters. All created bridges are torn down at the end of the test.
    """
    created: list[tuple[Router, IdMap]] = []

    async def _factory(dry_run=False):
        idmap = IdMap(":memory:")
        await idmap.open()
        sleeps: list[float] = []

        async def fake_sleep(d):
            sleeps.append(d)  # record but don't actually wait

        router = Router(idmap, dry_run=dry_run, send_gap_seconds=3.0, sleep=fake_sleep)
        adapters = {p: MockAdapter(p, ROOM) for p in (TG, QQ, MATRIX)}
        for a in adapters.values():
            router.register(a)
        await router.start()
        created.append((router, idmap))
        return router, idmap, sleeps, adapters

    yield _factory

    for router, idmap in created:
        await router.stop()
        await idmap.close()


@pytest.fixture
async def idmap():
    """A fresh, opened in-memory IdMap, closed on teardown."""
    im = IdMap(":memory:")
    await im.open()
    yield im
    await im.close()


@pytest.fixture
def make_qq_transport():
    """Factory for a test-wired RabbitMQQQTransport: no real connection; `_send`
    immediately fakes a NapCat action response so call_action resolves."""

    def _factory():
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

    return _factory
