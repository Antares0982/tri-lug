"""Matrix adapter: which homeserver failures are a transient outage (warn and
keep going / retry) versus a real error (traceback, stay down)."""

from types import SimpleNamespace

import aiohttp
import pytest
from mautrix.errors import MatrixError, MatrixUnknownRequestError, MForbidden
from mautrix.types import RoomID

from modules.tri_lug_utils import matrix_adapter as ma
from modules.tri_lug_utils.matrix_adapter import MatrixAdapter, _is_transient

_CF_522 = "<!DOCTYPE html>\n<title>522: Connection timed out</title>"


def test_is_transient():
    # The Cloudflare 522 in front of the homeserver, HTML body and all.
    assert _is_transient(MatrixUnknownRequestError(522, _CF_522))
    assert _is_transient(MatrixUnknownRequestError(502, "bad gateway"))
    assert _is_transient(aiohttp.ClientConnectionError("nope"))
    assert _is_transient(TimeoutError())

    # Bridge-side mistakes must keep their traceback.
    assert not _is_transient(MForbidden(403, "not in room"))
    assert not _is_transient(MatrixUnknownRequestError(400, "bad json"))
    assert not _is_transient(ValueError("bug"))


class _FakeIntent:
    """Enough of IntentAPI for `_setup_homeserver`; `fail` is raised by
    `ensure_registered` for the first `fail_times` calls."""

    def __init__(self, fail: Exception | None = None, fail_times: int = 0) -> None:
        self._fail = fail
        self._fail_times = fail_times
        self.registered = 0
        self.joined: list[str] = []

    async def ensure_registered(self) -> None:
        self.registered += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            assert self._fail is not None
            raise self._fail

    async def resolve_room_alias(self, alias):
        return SimpleNamespace(room_id=RoomID("!real:example.org"))

    async def ensure_joined(self, room) -> None:
        self.joined.append(str(room))

    async def get_state_event(self, room, event_type):
        raise MatrixError("no pins")


def _adapter(intent: _FakeIntent) -> MatrixAdapter:
    adapter = MatrixAdapter(
        "room",
        homeserver="https://matrix.example.org",
        server_name="example.org",
        as_id="tri-lug",
        as_token="as",
        hs_token="hs",
        bot_localpart="trilugbot",
        ghost_prefix="trilug_",
        room_id="#chat:example.org",
        listen_host="127.0.0.1",
        listen_port=1,
    )
    adapter._appserv = SimpleNamespace(intent=intent)  # type: ignore[assignment]
    return adapter


async def test_setup_retries_through_outage(monkeypatch):
    monkeypatch.setattr(ma, "_SETUP_RETRY_SECONDS", 0)
    intent = _FakeIntent(MatrixUnknownRequestError(522, _CF_522), fail_times=2)
    adapter = _adapter(intent)

    await adapter._setup_homeserver()

    assert intent.registered == 3  # two 522s, then success
    # The alias must end up resolved, otherwise every later send is broken.
    assert str(adapter._room_id) == "!real:example.org"
    assert intent.joined == ["!real:example.org"]


async def test_setup_gives_up_on_real_error(monkeypatch):
    monkeypatch.setattr(ma, "_SETUP_RETRY_SECONDS", 0)
    intent = _FakeIntent(MForbidden(403, "bad token"), fail_times=99)
    adapter = _adapter(intent)

    await adapter._setup_homeserver()

    assert intent.registered == 1  # no retry loop on a config error
    assert str(adapter._room_id) == "#chat:example.org"


async def test_send_before_alias_resolves_is_dropped():
    from modules.tri_lug_utils.bridge_message import BridgeMessage, BridgeUser

    adapter = _adapter(_FakeIntent())
    msg = BridgeMessage(
        platform="tg",
        room_key="room",
        msg_id="1",
        sender=BridgeUser(platform="tg", user_id="1", display_name="alice"),
        text="hi",
    )
    assert await adapter.send(msg, None) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
