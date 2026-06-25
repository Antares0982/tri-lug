"""Platform-agnostic message model shared by every bridge adapter.

Each adapter translates its platform's events into a `BridgeMessage` on the way
in, and renders a `BridgeMessage` into platform-native form on the way out. The
Router only ever sees this neutral shape, so cross-platform concerns (reply
mapping, identity prefixing, media) are handled in one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# Platform identifiers. Used as adapter keys and as the `platform` field on
# every BridgeMessage / BridgeUser.
TG = "tg"
QQ = "qq"
MATRIX = "matrix"


@dataclass
class BridgeUser:
    """The author of a message, on their origin platform.

    `avatar_key` is a stable content key for the author's avatar (e.g.
    ``qq:12345`` or ``tg:<file_unique_id>``); a target adapter uses it to skip
    re-uploading an avatar it has already mirrored. `avatar_data` carries the
    raw bytes when the origin adapter has them cached, so the target never has
    to fetch them off the network itself. Both are optional — when absent the
    target leaves the avatar untouched.
    """

    platform: str
    user_id: str
    display_name: str
    avatar_key: str | None = None
    avatar_data: bytes | None = None


@dataclass
class Attachment:
    """A non-text payload. Stickers are normalized to `kind="image"` wherever a
    platform can't represent them natively; `kind="sticker"` is kept only when
    the target can render it as such."""

    kind: str  # "image" | "sticker" | "file" | "video" | "audio"
    url: str | None = None
    data: bytes | None = None
    mime: str | None = None
    filename: str | None = None


@dataclass
class BridgeMessage:
    """A single message flowing through the bridge.

    `msg_id` and `reply_to_msg_id` are always the *origin platform's* native
    ids (as strings). The Router resolves `reply_to_msg_id` into the target
    platform's id via the IdMap before handing the message to an adapter.
    """

    platform: str
    room_key: str
    msg_id: str
    sender: BridgeUser
    text: str = ""
    reply_to_msg_id: str | None = None
    mentions: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # Epoch seconds stamped at the earliest point of receipt (relay for QQ,
    # adapter on-receive for TG/Matrix). The Router drops messages older than a
    # threshold at the fan -> msg-out handoff. Defaults to "now" so any message
    # constructed without an explicit stamp is treated as fresh.
    ts: float = field(default_factory=time.time)
    raw: Any = None  # original platform object, kept for debugging only
