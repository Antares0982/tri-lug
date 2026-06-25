# tri-lug Bridge Design

## 1. Core constraints

* Three-way message interop (messages flow between all three platforms).
* Messages from the bridge's own bot accounts are never forwarded to other group chats under any circumstances.
* Forwarding is split into two independent queue stages: msg-in (inbound, order-preserving) and msg-out (outbound, rate-limited), with a fan distributing between them.
* Every message is timestamped at the moment it is first received: QQ is stamped by the relay on inbound (before entering RabbitMQ); TG/Matrix are stamped by their respective adapter when the event is received.
* msg-in: one queue per source, guaranteeing messages from the same source reach the fan in send order, so a later message never overtakes an earlier one (assuming low message frequency).
* At the fan -> msg-out handoff the timestamp is checked: if it differs from the current time by more than 60 seconds the message is dropped and a WARNING is logged (purpose: drop stale messages that lingered in RabbitMQ or were badly delayed by network jitter). Once in msg-out no further timeout check is done (the delay introduced by the 3-second rate limit does not count).
* msg-out: one queue per target; consecutive actually-sent messages to the same target are spaced at least 3 seconds apart.
* A dry-run mode is required: in dry-run no real messages are sent; instead the message that would be sent is printed to the log.
* Must correctly handle a single message containing text + image/sticker, a single message with multiple images, and similar cases.
* Must correctly handle message replies.
* All RabbitMQ send/receive must be acked.
* No send retry; ack must handle timeout situations.
* The QQ side communicates over RabbitMQ, but the communication layer is encapsulated as much as possible so the transport can later be switched from RabbitMQ to any other implementation.



## 2. Implementation notes per feature

### Text
- Direct passthrough. @mention: cross-platform account systems are not interoperable -> **degrade to plain text `@name`**, no exact pill mapping (v1).
  - QQ's `at` segment: NapCat does not fill in `name`; alice resolves it via `get_group_member_info` into `@group-nickname` (with a TTL cache inside the QQ adapter), rather than `@QQ-number`.
  - When a user replies on QQ to a message forwarded by the bridge, QQ automatically inserts an `at` segment pointing at the bridge bot; that at (`qq == bridge bot uin`) is dropped entirely and is not forwarded as `@bridge`.
- Identity presentation: on the QQ/TG side use the prefix `[source] name:`, followed by a **newline** and then the message body (header and body on separate lines; if there is no body, only the header is sent); on the **Matrix side use an appservice ghost (puppet)**, with no prefix.
  The prefix logic lives inside each adapter, not in the Router.
  - QQ source: `[QQ] group-nickname` (note: the group nickname, not the QQ nickname).
  - Telegram source: `[TG] tg-nickname`.
  - Matrix source: `[Matrix] matrix-nickname`.
  - The Matrix ghost's displayname follows the source user: when the source nickname changes (e.g. a QQ group card change) the ghost displayname is refreshed, not frozen at the first value.

- Text with links: only Telegram produces rich text where "display text != link" (message entity `text_link`). On inbound the TG adapter uses `parse_entities()` (offsets are UTF-16, so this API must be used for slicing) to convert those entities into `[text](url)` plain text before handing off to the fan; bare URLs (`url` entity) are kept as-is. Other platforms need no handling: Matrix only reads `body` and ignores `formatted_body`; bare URLs are sent as plain text (the Telegram client auto-detects them as clickable). No platform uses parse_mode/HTML on outbound.

### Images
- Unified flow: the source side fetches the image **bytes** -> the target adapter uploads them (Matrix uploads to `mxc://` first; TG `send_photo`; QQ `image` segment).
- QQ images: the relay on machine B fetches the bytes (preferring the url provided by NapCat, falling back to the local file from `get_image`), inlines them as base64 into the image segment of `qq.event`, and delivers them over RabbitMQ; alice decodes to bytes and uploads directly. The relay no longer just forwards NapCat's url.
  - The relay maintains a disk cache keyed by QQ image file id; every hour it removes files older than 3 hours.
- Must correctly handle all combinations of image and text (including linked text) mixed together, multiple images in one message, etc. The normalized model uses `text:str + ordered attachments[]`: on inbound, merge all text and collect all images (the exact interleaving of text/images is not preserved). On outbound rendering:
  - QQ: `[reply] + text segment + each image segment`.
  - TG: use a `send_media_group` album, with header+text as the caption of the first image (batched if more than 10 images).
  - Matrix: send one text event first, then one event per image.
  - Reply mapping: each target records the native id of its "first part" as the primary for that logical message in the IdMap.
- The relay must fetch and inline bytes for both `image` and `mface` segments (`face`, the small yellow-face emoji, is not fetched and is dropped by alice). Fetch order: first HTTP GET the url in the segment; on failure or no url, fall back to NapCat `get_image`/`get_file` to read the local cache file.
- TG inbound albums (multiple images sharing a `media_group_id`, arriving as separate Updates, only the first carrying a caption): buffer by `media_group_id` for about 1 second and merge into a single message before handing off to the fan.

### Stickers (always normalized to images)
- **TG sticker**: static webp -> use directly as an image; **animated/video sticker -> take the `sticker.thumbnail` static frame**, do not touch lottie/gif conversion.
- **QQ `mface`** (large store emoji) -> take its image URL, treat as an image.
- **QQ `face`** (small yellow face) -> a text/emoji placeholder, not treated as an image.
- **Matrix `m.sticker`** -> already an image, treat as an image.

### Replies
- On inbound record `reply_to_msg_id` (origin platform native id); the Router resolves it via the IdMap into the target platform native id.
- Native replies per platform: TG `reply_to_message_id`; QQ `reply` segment `{id}`;
  Matrix `m.relates_to.m.in_reply_to` plus the spec-required `formatted_body` fallback quote block.
- IdMap:
  - Only records replies from the last 24h; old ids are discarded on a 1h trigger.
  - Message id records need not be persisted.
  - When the replied-to id cannot be found, fall back to a normal message.


### Avatars (Matrix ghost)
- The Matrix ghost reflects the source user's avatar. The target side (Matrix) caches by avatar content key: it uploads to the Matrix media repo (mxc) only on cache miss; the same key is uploaded only once across ghosts; a ghost avatar is reset only when its source key changes.
- The source side attaches `avatar_key` (a stable content key, e.g. `qq:<uin>`, `tg:<file_unique_id>`) and (when the local cache hits) the `avatar_data` bytes onto the `BridgeUser`, so the target need not fetch bytes from the network itself.
- TG avatars: the adapter fetches bytes via `get_user_profile_photos`, caches per user with a TTL, and reuses the bytes while `file_unique_id` is unchanged.
- QQ avatars: alice does not access Tencent's CDN directly; instead it requests the bytes from machine B's relay via an RPC **encapsulated inside the RabbitMQ transport layer** (routing keys `qq.avatar_req`/`qq.avatar_resp`, correlated by echo); the relay fetches from `q1.qlogo.cn` by uin and returns base64. The QQ adapter caches bytes per uin (with positive/negative TTL hits) and only goes to RabbitMQ on a cache miss; fetching bytes happens **in the background and does not block message forwarding** -- the first message may arrive without an avatar, with the bytes attached to a later message once ready. This RPC is decoupled from the rest of the bridge logic (it exists only inside the transport and the QQ adapter).

### Pins (Telegram <-> Matrix only)
- Pin events are interoperable between TG and Matrix; QQ does not participate (the QQ adapter's pin is a no-op).
- Inbound: on TG, pins are recognized via the `pinned_message` service message (service messages produced by the bridge bot's own pinning are ignored to prevent loops); on Matrix, listen to the `m.room.pinned_events` state event, diff against the last known set, and trigger interop only for newly added pin ids (own writes and the startup baseline only update the local set and are not sent out).
- Outbound: resolve the source native id to the target native id via the IdMap; TG uses `pin_chat_message`, Matrix has the bridge bot rewrite `m.room.pinned_events`.
- Permissions: writing a Matrix pin requires the bridge bot to have the power level to send state events in the room (default `state_default` = 50 / moderator). Appservice registration itself does not grant a power level; it must be authorized by a room admin. On insufficient permission the pin fails with a log entry only and does not affect message forwarding.

### Other messages

- Messages outside the v1 scope (video/audio/file/card/poke/recall, etc.) are not forwarded, but must be logged.
- alice is responsible for the log annotation: any event that enters a bridged room and has no forwardable content after parsing is dropped after logging one `[<platform>][log-only - not forwarded] <type + content summary>`; this is the implementation of "explicitly state log-only, do not forward", rather than adding a flag bit into the RabbitMQ payload (the relay does no translation and cannot tell whether something is bridgeable).
- QQ side: the relay still sends the raw event to alice over RabbitMQ for logging, dropping only `meta_event` (heartbeat/lifecycle) to avoid pointlessly flooding RabbitMQ.



## 3. Runtime control (/stop_bridge, /start_bridge)

- All three ends support the plain-text commands `/stop_bridge` and `/start_bridge`: QQ/Matrix trigger on receiving the command as plain text (intercepted inside the adapter before forwarding; the command itself is not bridged as a message); TG handles it as a command (CommandHandler), effective only in the bridged group. Any member may trigger it.
- `/stop_bridge` pauses the forwarding of messages and pins; `/start_bridge` resumes it. The pause is in-process runtime state and is not persisted: a process restart returns to the running state.
- This is a different concept from `ENABLED`: when `ENABLED=False` the entire bridge module does not work at all, and even these two commands are not registered; pausing only suspends forwarding at runtime while the module keeps running.
- During a pause the commands themselves are still processed (commands are intercepted on inbound before the "pause drop"), so `/start_bridge` can resume from the paused state.
