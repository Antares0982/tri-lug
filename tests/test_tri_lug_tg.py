"""Network-free tests for the Telegram adapter's outbound rendering.

No python-telegram-bot Application / network: a `_FakeBot` records the kwargs of
every send_* call and hands back a synthetic message id.

Covers:
  1. the `[label] name:` header is bolded via an explicit entity (UTF-16 offsets)
  2. text / photo / album / audio all carry that entity, on the right leg
  3. a bridged QQ voice note goes out via send_audio, not send_voice
"""

from __future__ import annotations

from dataclasses import dataclass, field

from telegram import MessageEntity

from modules.tri_lug_utils.bridge_message import (
    QQ,
    Attachment,
    BridgeMessage,
    BridgeUser,
)
from modules.tri_lug_utils.tg_adapter import TelegramAdapter

from conftest import ROOM

CHAT_ID = 4242
# A non-BMP character in the name makes the entity length differ from the plain
# character count, so the UTF-16 measurement is actually exercised.
SENDER = "小明🎉"
HEADER = f"[QQ] {SENDER}:"
HEADER_U16_LEN = 10  # "[QQ] " 5 + 小明 2 + 🎉 2 (surrogate pair) + ":" 1


@dataclass
class _Sent:
    message_id: int


@dataclass
class _FakeBot:
    id: int = 1
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _next_id: int = 100

    def _record(self, name: str, kwargs: dict, count: int = 1):
        self.calls.append((name, kwargs))
        sent = []
        for _ in range(count):
            self._next_id += 1
            sent.append(_Sent(self._next_id))
        return sent

    async def send_message(self, **kw):
        return self._record("send_message", kw)[0]

    async def send_photo(self, **kw):
        return self._record("send_photo", kw)[0]

    async def send_audio(self, **kw):
        return self._record("send_audio", kw)[0]

    async def send_media_group(self, **kw):
        return self._record("send_media_group", kw, count=len(kw["media"]))


@dataclass
class _FakeApp:
    bot: _FakeBot = field(default_factory=_FakeBot)


def _adapter() -> tuple[TelegramAdapter, _FakeBot]:
    adapter = TelegramAdapter(ROOM, CHAT_ID)
    app = _FakeApp()
    adapter.attach_app(app)  # type: ignore[arg-type]
    return adapter, app.bot


def _message(text: str = "", attachments: list[Attachment] | None = None):
    return BridgeMessage(
        platform=QQ,
        room_key=ROOM,
        msg_id="1",
        sender=BridgeUser(QQ, "10086", SENDER),
        text=text,
        attachments=attachments or [],
    )


def _assert_header_bold(entities) -> None:
    assert len(entities) == 1, entities
    entity = entities[0]
    assert entity.type == MessageEntity.BOLD, entity.type
    assert entity.offset == 0, entity.offset
    assert entity.length == HEADER_U16_LEN, entity.length


async def test_text_header_bold():
    adapter, bot = _adapter()
    ids = await adapter.send(_message("hello"), reply_to_native_id="7")
    assert ids == ["101"], ids
    name, kw = bot.calls[0]
    assert name == "send_message"
    assert kw["text"] == f"{HEADER}\nhello", kw["text"]
    assert kw["reply_to_message_id"] == 7
    _assert_header_bold(kw["entities"])


async def test_photo_caption_entities():
    adapter, bot = _adapter()
    msg = _message("look", [Attachment("image", data=b"\x89PNG", mime="image/png")])
    await adapter.send(msg, reply_to_native_id=None)
    name, kw = bot.calls[0]
    assert name == "send_photo"
    assert kw["caption"] == f"{HEADER}\nlook"
    _assert_header_bold(kw["caption_entities"])


async def test_album_caption_only_on_first_item():
    adapter, bot = _adapter()
    msg = _message("three", [Attachment("image", data=b"i%d" % i) for i in range(3)])
    ids = await adapter.send(msg, reply_to_native_id=None)
    assert len(ids) == 3, ids
    name, kw = bot.calls[0]
    assert name == "send_media_group"
    media = kw["media"]
    assert media[0].caption == f"{HEADER}\nthree"
    _assert_header_bold(media[0].caption_entities)
    # The rest of the album carries neither caption nor entities.
    assert all(m.caption is None and not m.caption_entities for m in media[1:])


async def test_voice_note_uses_send_audio():
    """A bridged QQ voice note: send_audio (send_voice would reject mp3), with
    the bold header as its caption and the reply anchored on it."""
    adapter, bot = _adapter()
    msg = _message(
        "",
        [Attachment("audio", data=b"ID3mp3", mime="audio/mpeg", filename="voice.mp3")],
    )
    ids = await adapter.send(msg, reply_to_native_id="55")
    assert ids == ["101"], ids
    assert [c[0] for c in bot.calls] == ["send_audio"], bot.calls
    kw = bot.calls[0][1]
    assert kw["audio"] == b"ID3mp3"
    assert kw["filename"] == "voice.mp3"
    assert kw["caption"] == HEADER  # no body text -> header alone
    assert kw["reply_to_message_id"] == 55
    _assert_header_bold(kw["caption_entities"])


async def test_image_and_audio_anchor_reply_once():
    """With both kinds present the image leg goes first and owns the caption +
    reply; the audio leg follows bare, and every id is returned for linking."""
    adapter, bot = _adapter()
    msg = _message(
        "mixed",
        [
            Attachment("image", data=b"\x89PNG", mime="image/png"),
            Attachment("audio", data=b"ID3mp3", mime="audio/mpeg"),
        ],
    )
    ids = await adapter.send(msg, reply_to_native_id="9")
    assert ids == ["101", "102"], ids
    assert [c[0] for c in bot.calls] == ["send_photo", "send_audio"]
    audio_kw = bot.calls[1][1]
    assert audio_kw["caption"] is None
    assert audio_kw["caption_entities"] is None
    assert audio_kw["reply_to_message_id"] is None
    assert audio_kw["filename"] == "voice.mp3"  # no filename -> default
