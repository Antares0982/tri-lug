"""OneBot 11 ⇄ BridgeMessage translation (pure, no I/O).

Kept free of any transport so it can be unit-tested against captured OneBot
event JSON. Transport (RabbitMQ ↔ the machine-B NapCat relay) lives in the
QQ adapter.

v1 scope (see docs/tri-bridge/design.md §4): text, image, sticker→image, reply.
- `image` / `mface` (QQ market sticker) → image Attachment.
- `face` (QQ small emoji) → dropped (no name map; would otherwise be noise).
- `at` → downgraded to plain text `@name`, and the qq id collected in mentions.
"""

from __future__ import annotations

import base64
import time
from modules.tri_lug_utils.bridge_message import (
    QQ,
    Attachment,
    BridgeMessage,
    BridgeUser,
    sniff_image_mime,
)
from modules.tri_lug_utils.header import render_header


def parse_group_event(
    event: dict,
    room_key: str,
    self_uin: int | str | None = None,
    name_map: dict[str, str] | None = None,
) -> BridgeMessage | None:
    """Translate a OneBot 11 group message event into a BridgeMessage, or None
    if it isn't a group message we care about / carries no bridgeable content.

    `self_uin`: when a user replies to a bridge-relayed message, QQ auto-inserts
    an `at` segment targeting the bridge bot; that at is dropped so it isn't
    forwarded as a stray `@桥`. `name_map`: ``qq -> 群昵称`` resolved by the
    adapter (NapCat leaves the `at` segment's `name` empty), so mentions render
    as `@昵称` instead of `@QQ号`.
    """
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return None
    self_uin_str = str(self_uin) if self_uin not in (None, 0, "0") else None
    name_map = name_map or {}
    segments = event.get("message")
    if not isinstance(segments, list):
        return None

    sender = event.get("sender") or {}
    user_id = str(event.get("user_id") or sender.get("user_id") or "")
    display = sender.get("card") or sender.get("nickname") or user_id

    text_parts: list[str] = []
    attachments: list[Attachment] = []
    mentions: list[str] = []
    reply_to: str | None = None

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            text_parts.append(str(data.get("text", "")))
        elif stype == "at":
            qq = str(data.get("qq", ""))
            if qq == "all":
                text_parts.append("@全体成员 ")
            elif self_uin_str is not None and qq == self_uin_str:
                # Auto-at the bridge bot inserts on a reply to a relayed
                # message — drop it entirely (no text, no mention).
                continue
            else:
                name = name_map.get(qq) or data.get("name") or qq
                text_parts.append(f"@{name} ")
                mentions.append(qq)
        elif stype == "reply":
            reply_to = str(data.get("id", "")) or None
        elif stype in ("image", "mface"):
            att = _image_attachment(data)
            if att is not None:
                attachments.append(att)
        # face and everything else: ignored in v1

    text = "".join(text_parts).strip()
    msg_id = str(event.get("message_id") or "")
    if not msg_id or (not text and not attachments):
        return None

    return BridgeMessage(
        platform=QQ,
        room_key=room_key,
        msg_id=msg_id,
        sender=BridgeUser(QQ, user_id, display),
        text=text,
        reply_to_msg_id=reply_to,
        mentions=mentions,
        attachments=attachments,
        ts=_event_ts(event),
        raw=event,
    )


def _image_attachment(data: dict) -> Attachment | None:
    """Build an image Attachment from an OneBot image/mface segment. The relay
    inlines the fetched bytes as ``base64``; fall back to a url/file ref only if
    no bytes were provided (so a bridge still degrades gracefully)."""
    b64 = data.get("base64")
    if b64:
        try:
            raw_bytes = base64.b64decode(b64)
        except ValueError:
            raw_bytes = b""
        if raw_bytes:
            mime = data.get("mime") or sniff_image_mime(raw_bytes)
            return Attachment(
                "image",
                data=raw_bytes,
                mime=mime,
                filename=data.get("file"),
            )
    file_ref = data.get("url") or data.get("file")
    if file_ref:
        return Attachment("image", url=file_ref, filename=data.get("file"))
    return None


def _event_ts(event: dict) -> float:
    """Receipt timestamp for staleness: prefer the relay-injected ``ts`` (epoch
    float, stamped before RabbitMQ), else the OneBot ``time`` (epoch seconds,
    machine-B clock), else now."""
    for key in ("ts", "time"):
        val = event.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return time.time()


def is_noise_event(event: dict) -> bool:
    """Return True for QQ events that are pure noise and don't need logging.

    Suppressed types:
    - ``notice_type=group_msg_emoji_like``  (reaction notifications)
    - messages whose only segments are ``face``  (small emoji, not bridgeable)
    - ``sub_type=poke``  (poke / nudge notices)
    """
    post_type = event.get("post_type")
    if post_type == "notice":
        if event.get("notice_type") in ("group_msg_emoji_like", "group_recall"):
            return True
        if event.get("sub_type") == "poke":
            return True
    if post_type == "message":
        segments = event.get("message")
        if isinstance(segments, list) and segments:
            seg_types = {s.get("type") for s in segments if isinstance(s, dict)}
            if seg_types == {"face"}:
                return True
    return False


def describe_event(event: dict) -> str:
    """One-line summary of a QQ event we do NOT bridge, for the log-only path."""
    post_type = event.get("post_type")
    if post_type == "message":
        seg_types = []
        msg = event.get("message")
        if isinstance(msg, list):
            seg_types = [str(s.get("type")) for s in msg if isinstance(s, dict)]
        return (
            f"message segments={seg_types or '?'} message_id={event.get('message_id')}"
        )
    if post_type == "notice":
        return f"notice notice_type={event.get('notice_type')} sub_type={event.get('sub_type')}"
    if post_type == "request":
        return f"request request_type={event.get('request_type')}"
    return f"post_type={post_type}"


def build_send_segments(
    msg: BridgeMessage, reply_to_native_id: str | None
) -> list[dict]:
    """Render a BridgeMessage into a OneBot 11 message segment array for
    `send_group_msg`. A `[label] name:` header carries the cross-platform
    identity, since QQ has a single bot account (no puppeting)."""
    segments: list[dict] = []
    if reply_to_native_id:
        segments.append({"type": "reply", "data": {"id": str(reply_to_native_id)}})

    header = render_header(msg, QQ)
    body = f"{header}\n{msg.text}" if msg.text else header
    segments.append({"type": "text", "data": {"text": body}})

    for att in msg.attachments:
        if att.kind != "image":
            continue
        file_ref = _attachment_file_ref(att)
        if file_ref:
            segments.append({"type": "image", "data": {"file": file_ref}})
    return segments


def _attachment_file_ref(att: Attachment) -> str | None:
    """NapCat accepts a url, a local path, or `base64://<data>` as image file."""
    if att.data is not None:
        return "base64://" + base64.b64encode(att.data).decode("ascii")
    if att.url:
        return att.url
    return None
