"""Matrix appservice adapter (mautrix-python).

Per-user puppeting: each TG/QQ sender is mirrored by a ghost user
``@{prefix}{platform}_{id}:{server}`` — e.g. ``@_njulug_tg_123:li7g.com`` —
within the appservice's exclusive user namespace, so messages appear under the
real author's name instead of a single relay bot.

Skeleton status: written against mautrix 0.21.0; the HS→AS push path can only be
exercised live once the li7g.com admin registers the appservice (as_token) and
``https://tri-lug.chr.fan`` reaches this listener. v1 scope: text, image,
sticker(→image), reply.

Inbound: HS pushes events to our aiohttp listener; we keep m.room.message in the
bridged room from non-ghost senders. Loop prevention drops anything authored by
our bot or a ghost (namespace prefix).
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import cast

import aiohttp
from mautrix.appservice import AppService, IntentAPI
from mautrix.errors import MatrixError
from mautrix.types import (
    ContentURI,
    EventID,
    EventType,
    Format,
    ImageInfo,
    MediaMessageEventContent,
    MessageEvent,
    MessageType,
    RoomAlias,
    RoomID,
    RoomPinnedEventsStateEventContent,
    TextMessageEventContent,
    UserID,
)

from antares_bot.bot_logging import get_logger

from modules.tri_lug_utils.adapters import BaseAdapter
from modules.tri_lug_utils.bridge_message import (
    MATRIX,
    Attachment,
    BridgeMessage,
    BridgeUser,
    sniff_image_mime,
)
from modules.tri_lug_utils.router import STOP_COMMAND, parse_control_command

_LOGGER = get_logger(__name__)

# Suffix on ghost display names so identical names across platforms stay distinct.
_PLATFORM_LABEL = {"tg": "TG", "qq": "QQ", "matrix": "Matrix"}


class _HtmlToText(HTMLParser):
    """Flatten a Matrix `formatted_body` (org.matrix.custom.html) to plain text,
    rewriting `<a href="url">text</a>` hyperlinks to `[text](url)`.

    The plain-text `body` fallback keeps only a link's display text and drops the
    URL, so for a message whose display text ≠ URL we must read the HTML to
    recover the target. The output `[text](url)` matches the form a user gets by
    typing markdown directly, which the QQ side already renders fine. Non-anchor
    tags are dropped (text kept); `<br>` and block boundaries become newlines.
    """

    _BLOCK_TAGS = {"p", "div", "blockquote", "li", "tr", "h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._href: str | None = None
        self._link: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link = []
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            text = "".join(self._link)
            href = self._href
            self._href = None
            self._link = []
            # matrix.to links are user/room pills (mentions); keep just the
            # display text. A bare autolink (text == href) is already
            # self-describing; only display-text ≠ URL needs the markdown form.
            if not href or text == href or href.startswith("https://matrix.to/"):
                self._parts.append(text or href or "")
            elif text:
                self._parts.append(f"[{text}]({href})")
            else:
                self._parts.append(href)
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        (self._link if self._href is not None else self._parts).append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def flatten_matrix_html(formatted_body: str) -> str:
    parser = _HtmlToText()
    parser.feed(formatted_body)
    parser.close()
    return parser.get_text()


class MatrixAdapter(BaseAdapter):
    platform = MATRIX

    def __init__(
        self,
        room_key: str,
        *,
        homeserver: str,
        server_name: str,
        as_id: str,
        as_token: str,
        hs_token: str,
        bot_localpart: str,
        ghost_prefix: str,
        room_id: str,
        listen_host: str,
        listen_port: int,
    ) -> None:
        super().__init__(room_key)
        self._homeserver = homeserver
        self._server_name = server_name
        self._as_id = as_id
        self._as_token = as_token
        self._hs_token = hs_token
        self._bot_localpart = bot_localpart
        self._ghost_prefix = ghost_prefix
        self._room_id = RoomID(room_id)
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._appserv: AppService | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ensured_ghosts: set[str] = set()
        # ghost localpart -> last displayname / last mirrored avatar key, so a
        # renamed or re-avatared source user is reflected instead of being
        # frozen at first sight.
        self._ghost_names: dict[str, str] = {}
        self._ghost_avatars: dict[str, str] = {}
        # avatar content key -> uploaded mxc, shared across ghosts so identical
        # avatars upload to the Matrix media repo only once.
        self._avatar_mxc: dict[str, str] = {}
        # last-seen pinned event ids, to diff inbound m.room.pinned_events.
        self._pinned: set[str] = set()

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._http = aiohttp.ClientSession()
        self._appserv = AppService(
            server=self._homeserver,
            domain=self._server_name,
            as_token=self._as_token,
            hs_token=self._hs_token,
            bot_localpart=self._bot_localpart,
            id=self._as_id,
        )
        self._appserv.matrix_event_handler(self._on_matrix_event)
        await self._appserv.start(self._listen_host, self._listen_port)
        await self._appserv.intent.ensure_registered()
        self._room_id = await self._resolve_room_id(self._room_id)
        try:
            await self._appserv.intent.ensure_joined(self._room_id)
        except MatrixError:
            _LOGGER.warning(
                "[matrix] bot not in room %s yet — invite @%s:%s",
                self._room_id,
                self._bot_localpart,
                self._server_name,
            )
        self._pinned = await self._current_pins()
        _LOGGER.info(
            "[matrix] appservice listening on %s:%s",
            self._listen_host,
            self._listen_port,
        )

    async def _resolve_room_id(self, room: RoomID) -> RoomID:
        """Accept either a `!internal:server` id or a `#alias:server` in config.
        Aliases are resolved once at startup to the canonical room id, since
        send_message/ensure_joined require the `!` form."""
        assert self._appserv is not None
        if not str(room).startswith("#"):
            return room
        try:
            info = await self._appserv.intent.resolve_room_alias(RoomAlias(str(room)))
            _LOGGER.info("[matrix] resolved alias %s -> %s", room, info.room_id)
            return info.room_id
        except MatrixError:
            _LOGGER.warning(
                "[matrix] could not resolve alias %s — bridge inbound will not "
                "work until it resolves to a room id",
                room,
            )
            return room

    async def stop(self) -> None:
        if self._appserv is not None:
            try:
                await self._appserv.stop()
            except Exception:
                pass
        if self._http is not None:
            await self._http.close()

    # ------------------------------------------------------------------ inbound
    async def _on_matrix_event(self, evt) -> None:
        try:
            if str(evt.room_id) != str(self._room_id):
                return
            if evt.type == EventType.ROOM_PINNED_EVENTS:
                await self._handle_pin_event(evt)
                return
            if evt.type not in (EventType.ROOM_MESSAGE, EventType.STICKER):
                return
            if self._is_ours(str(evt.sender)):  # loop prevention
                return
            await self._handle_message(evt)
        except Exception:
            _LOGGER.exception("[matrix] error handling event")

    async def _handle_pin_event(self, evt) -> None:
        """Bridge newly added pins to the other platforms. Our own pin writes
        (and the startup baseline) only update the local set so they don't echo
        back out."""
        new = {str(e) for e in (getattr(evt.content, "pinned", None) or [])}
        if self._is_ours(str(evt.sender)):
            self._pinned = new
            return
        added = new - self._pinned
        self._pinned = new
        if self.router is None:
            return
        for event_id in added:
            await self.router.pin(MATRIX, event_id)

    async def _current_pins(self) -> set[str]:
        """Read the room's current m.room.pinned_events, so the diff baseline
        doesn't treat pre-existing pins as freshly added on startup."""
        assert self._appserv is not None
        try:
            content = await self._appserv.intent.get_state_event(
                self._room_id, EventType.ROOM_PINNED_EVENTS
            )
        except MatrixError:
            return set()
        return {str(e) for e in (getattr(content, "pinned", None) or [])}

    async def set_pin(self, native_id: str, pinned: bool) -> None:
        """Mirror a pin onto Matrix by rewriting m.room.pinned_events as the
        bot. Requires the bot to have power level >= the room's state_default
        (moderator, 50, by default); on insufficient power the write raises and
        is logged without affecting message delivery."""
        if self._appserv is None:
            return
        current = list(await self._current_pins())
        if pinned and native_id not in current:
            current.append(native_id)
        elif not pinned and native_id in current:
            current.remove(native_id)
        else:
            return
        await self._appserv.intent.send_state_event(
            self._room_id,
            EventType.ROOM_PINNED_EVENTS,
            RoomPinnedEventsStateEventContent(pinned=[EventID(e) for e in current]),
        )
        # Update the baseline so the echo of our own write isn't re-bridged.
        self._pinned = set(current)

    def _is_ours(self, user_id: str) -> bool:
        localpart = user_id.split(":", 1)[0].lstrip("@")
        return localpart == self._bot_localpart or localpart.startswith(
            self._ghost_prefix
        )

    async def _handle_message(self, evt: MessageEvent) -> None:
        assert self._appserv is not None
        content = evt.content
        text = ""
        attachments: list[Attachment] = []
        # m.sticker is its own event type but carries an image (url + info), so
        # normalize it to an image attachment like a regular m.image.
        is_image = (
            evt.type == EventType.STICKER
            or getattr(content, "msgtype", None) == MessageType.IMAGE
        )
        if is_image:
            media = cast(MediaMessageEventContent, content)
            if not media.url:
                return
            try:
                data = await self._appserv.intent.download_media(media.url)
            except MatrixError:
                _LOGGER.warning("[matrix] failed to download %s", media.url)
                return
            mime = (
                str(media.info.mimetype)
                if media.info and getattr(media.info, "mimetype", None)
                else None
            )
            # MSC2530: a captioned image carries the real file name in
            # `filename` and the caption in `body`/`formatted_body`; an
            # uncaptioned one has no `filename` and `body` is the file name.
            has_caption = bool(media.filename) and media.body != media.filename
            attachments.append(
                Attachment(
                    "image",
                    data=data,
                    mime=mime,
                    filename=str(media.filename or media.body or "image"),
                )
            )
            if has_caption:
                text = self._content_text(media)
        else:
            text = self._content_text(cast(TextMessageEventContent, content))

        if not text and not attachments:
            return

        # A bare /stop_bridge or /start_bridge toggles the runtime pause rather
        # than being bridged (checked before delivery so /start_bridge works
        # while paused).
        if not attachments:
            cmd = parse_control_command(text)
            if cmd is not None:
                if self.router is not None:
                    self.router.set_paused(cmd == STOP_COMMAND)
                return

        reply_to = cast(TextMessageEventContent, content).get_reply_to()
        bm = BridgeMessage(
            platform=MATRIX,
            room_key=self.room_key,
            msg_id=str(evt.event_id),
            sender=BridgeUser(
                MATRIX, str(evt.sender), await self._displayname(evt.sender)
            ),
            text=text,
            reply_to_msg_id=str(reply_to) if reply_to else None,
            attachments=attachments,
            raw=evt,
        )
        await self._emit(bm)

    def _content_text(self, content: TextMessageEventContent) -> str:
        """Plain text of a text message or a media caption. When the content is
        HTML-formatted, strip the reply fallback (`<mx-reply>` / `> quoted`) and
        flatten the HTML — recovering `<a href>` hyperlinks the plain `body`
        would have dropped — otherwise fall back to `body`."""
        if content.format == Format.HTML and content.formatted_body:
            content.trim_reply_fallback()
            text = flatten_matrix_html(content.formatted_body)
            return text
        return str(content.body or "")

    async def _displayname(self, user_id: UserID) -> str:
        assert self._appserv is not None
        try:
            name = await self._appserv.intent.get_displayname(user_id)
            if name:
                return name
        except MatrixError:
            pass
        return str(user_id).split(":", 1)[0].lstrip("@")

    # ----------------------------------------------------------------- outbound
    async def send(
        self, msg: BridgeMessage, reply_to_native_id: str | None
    ) -> list[str]:
        if self._appserv is None:
            return []
        intent = await self._ghost_intent(msg.sender)
        reply_evt = EventID(reply_to_native_id) if reply_to_native_id else None
        # A text + image source message splits into a text event and an image
        # event; both ids are returned so the Router links each one, otherwise a
        # reply to the image event resolves to nothing.
        event_ids: list[str] = []

        if msg.text:
            content = TextMessageEventContent(msgtype=MessageType.TEXT, body=msg.text)
            if reply_evt:
                content.set_reply(reply_evt)
            event_ids.append(str(await intent.send_message(self._room_id, content)))

        for att in msg.attachments:
            if att.kind != "image":
                continue
            data = await self._attachment_bytes(att)
            if data is None:
                continue
            mime = att.mime or sniff_image_mime(data)
            mxc = await intent.upload_media(data, mime_type=mime, filename=att.filename)
            content = MediaMessageEventContent(
                msgtype=MessageType.IMAGE,
                body=att.filename or "image",
                url=mxc,
                info=ImageInfo(mimetype=mime) if mime else None,
            )
            if reply_evt and not event_ids:
                content.set_reply(reply_evt)
            event_ids.append(str(await intent.send_message(self._room_id, content)))

        return event_ids

    async def _ghost_intent(self, sender: BridgeUser) -> IntentAPI:
        assert self._appserv is not None
        localpart = self._ghost_localpart(sender)
        intent = self._appserv.intent.user(UserID(f"@{localpart}:{self._server_name}"))
        first = localpart not in self._ensured_ghosts
        if first:
            await intent.ensure_registered()

        # Refresh the displayname whenever the source name changes (a QQ card
        # rename otherwise stays frozen at first sight).
        label = _PLATFORM_LABEL.get(sender.platform, sender.platform)
        desired_name = f"{sender.display_name} ({label})"
        if self._ghost_names.get(localpart) != desired_name:
            try:
                await intent.set_displayname(desired_name)
                self._ghost_names[localpart] = desired_name
            except MatrixError:
                pass

        await self._ensure_avatar(intent, localpart, sender)

        if first:
            try:
                await intent.ensure_joined(self._room_id)
            except MatrixError:
                _LOGGER.warning("[matrix] ghost %s could not join room", localpart)
            self._ensured_ghosts.add(localpart)
        return intent

    async def _ensure_avatar(
        self, intent: IntentAPI, localpart: str, sender: BridgeUser
    ) -> None:
        """Mirror the source user's avatar onto the ghost. Uploads to the Matrix
        media repo only on cache miss (per avatar content key), and only re-sets
        a ghost's avatar when its source avatar key changes."""
        key = sender.avatar_key
        if not key or self._ghost_avatars.get(localpart) == key:
            return
        mxc = self._avatar_mxc.get(key)
        if mxc is None:
            if not sender.avatar_data:
                return  # key known but no bytes available — leave avatar as is
            try:
                uploaded = await intent.upload_media(
                    sender.avatar_data,
                    mime_type=sniff_image_mime(sender.avatar_data),
                    filename="avatar",
                )
            except MatrixError:
                _LOGGER.warning("[matrix] avatar upload failed for %s", localpart)
                return
            mxc = str(uploaded)
            self._avatar_mxc[key] = mxc
        try:
            await intent.set_avatar_url(ContentURI(mxc))
            self._ghost_avatars[localpart] = key
        except MatrixError:
            pass

    def _ghost_localpart(self, sender: BridgeUser) -> str:
        raw = f"{self._ghost_prefix}{sender.platform}_{sender.user_id}"
        return "".join(c if (c.isalnum() or c in "._=-/") else "_" for c in raw).lower()

    async def _attachment_bytes(self, att: Attachment) -> bytes | None:
        if att.data is not None:
            return att.data
        if att.url and self._http is not None:
            try:
                async with self._http.get(att.url) as resp:
                    return await resp.read()
            except Exception:
                _LOGGER.warning("[matrix] failed to fetch %s", att.url, exc_info=True)
        return None
