# tri-lug Bridge Design

## 1. Core constraints

* Three-way message interop (messages flow between all three platforms).
* Messages from the bridge's own bot accounts are never forwarded to other group chats under any circumstances.
* Forwarding is split into two independent queue stages: msg-in (inbound, order-preserving) and msg-out (outbound, rate-limited), with a fan distributing between them.
* Every message is timestamped at the moment it is first received: QQ is stamped by the relay on inbound (before entering RabbitMQ); TG/Matrix are stamped by their respective adapter when it builds the `BridgeMessage`. Note that "builds" is *after* media has been downloaded and (for TG animations) converted, so that work is not charged against the staleness budget below.
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
  - QQ's `at` segment: NapCat does not fill in `name`; alice resolves it via `get_group_member_info` into `<to:group-nickname>` (with a TTL cache inside the QQ adapter), rather than `<to:QQ-number>`. (QQ mentions render as `<to:name>` rather than `@name`.)
  - QQ `at` with `qq == "all"` renders as `@全体成员`.
  - When a user replies on QQ to a message forwarded by the bridge, QQ automatically inserts an `at` segment pointing at the bridge bot; that at (`qq == bridge bot uin`) is dropped entirely and is not forwarded as `@bridge`.
- Identity presentation: on the QQ/TG side use the prefix `[source] name:`, followed by a **newline** and then the message body (header and body on separate lines; if there is no body, only the header is sent); on the **Matrix side use an appservice ghost (puppet)**, with no prefix.
  The header is rendered by `header.render_header`, called from the TG and QQ adapters (never from the Router). Matrix keeps its own label map, since its displayname form differs.
  - On **Telegram the header is bold** (the whole `[source] name:`, colon included; the body after it is not). It is bolded with an explicit `MessageEntity(BOLD, offset=0, length=<header>)` rather than a parse_mode — see the note on parse_mode below. Offsets are UTF-16 code units, so the length is measured on the UTF-16-LE encoding. QQ has no rich text and is left plain.
  - QQ source: `[QQ] group-nickname` (note: the group nickname, not the QQ nickname).
  - Telegram source: `[TG] tg-nickname`.
  - Matrix source: `[Matrix] matrix-nickname`.
  - The Matrix ghost's displayname is `source-nickname (label)`, e.g. `Alice (QQ)` — the label suffix keeps identical names on different platforms distinct. It follows the source user: when the source nickname changes (e.g. a QQ group card change) the ghost displayname is refreshed, not frozen at the first value.
  - Optional header rewrites: `TriLugConfig.HEADER_REWRITES` (absent ⇒ no-op) is an ordered list of `{"pattern": <regex>, "repl": <str>, "target": <platform | None>}` applied with `re.sub` to the whole header string, in list order. A rule with a `target` fires only when rendering into that target platform; `target` absent/None applies to every target. Compiled once on first use ⇒ a change needs a restart.

- Text with links: both Telegram and Matrix produce rich text where "display text != link", and both are flattened to `[text](url)` plain text on inbound.
  - TG: the `text_link` message entity. Offsets/lengths are UTF-16 code units, so the slicing is done on the UTF-16-LE encoding. Bare URLs (`url` entity) are kept as-is.
  - Matrix: when the content is `org.matrix.custom.html`, the reply fallback is trimmed and `formatted_body` is flattened (`<a href>` → `[text](url)`, `<br>`/block tags → newline, other tags dropped). The plain `body` alone would lose the URL. A `matrix.to` link is a user/room pill — only its display text is kept. Content without HTML formatting falls back to `body`.
  - Bare URLs are sent as plain text (the Telegram client auto-detects them as clickable). **No platform uses parse_mode/HTML on outbound** — bridged text routinely contains `_`, `*`, `[text](url)` and raw `<`, none of which is ours to escape, and a parse mode would mangle or reject it. Telegram's bold header is therefore expressed as an explicit entity, which leaves the body untouched.

### Images
- Unified flow: the source side fetches the image **bytes** -> the target adapter uploads them (Matrix uploads to `mxc://` first; TG `send_photo`; QQ `image` segment).
- QQ images: the relay on machine B fetches the bytes (preferring the url provided by NapCat, falling back to the local file from `get_image`), inlines them as base64 into the image segment of `qq.event`, and delivers them over RabbitMQ; alice decodes to bytes and uploads directly. A segment with no `base64` degrades to carrying the url/file ref instead.
  - The relay maintains a disk cache keyed by QQ image file id; every hour it removes files older than 3 hours.
- Must correctly handle all combinations of image and text (including linked text) mixed together, multiple images in one message, etc. The normalized model uses `text:str + ordered attachments[]`: on inbound, merge all text and collect all images (the exact interleaving of text/images is not preserved). On outbound rendering:
  - QQ: `[reply] + text segment + each image segment`.
  - TG: images are walked in source order and split by whether they are *animated* (see below). A run of stills uses `send_photo` when alone, or a `send_media_group` album in batches of 10; each animated image goes out alone via `send_animation`.
  - Matrix: send one text event first (only if there is text), then one event per image.
  - Reply mapping: `adapter.send` returns **every** native id it produced and the Router links all of them into the same logical message, so a reply pointing at any part resolves. The first returned id is the reply anchor. Whichever leg goes out **first** owns the caption and the `reply_to_message_id`; every later leg is bare.
- **Animated images -> TG must use `send_animation`.** `sendPhoto` re-encodes its input into a static JPEG, so a GIF pushed through it arrives frozen. Two conditions gate the choice:
  - The format must be **GIF**. `sendAnimation` accepts only GIF or silent MP4, so an animated WebP or APNG (a QQ `mface` can be either) would be rejected outright and the message lost — those deliberately keep going through `send_photo`: still, but delivered. **This is a known gap**; closing it would need an *outbound* ffmpeg hop, which cuts against doing all conversion on the TG inbound path.
  - Within GIF the call is made on the **bytes** (`media.is_animated_image` walks the GIF/APNG/WebP block structure), not the mime: a *still* GIF sent via `sendAnimation` becomes an animation bubble that will not play. Only a url-only attachment, whose bytes alice never sees, falls back to trusting `mime == "image/gif"`.
  - An animation can never ride in an album: `sendMediaGroup` accepts only photo/video/audio/document. Hence the run-grouping above rather than a simpler stills/animations partition, which would reorder the sender's images.
- The relay must fetch and inline bytes for both `image` and `mface` segments (`face`, the small yellow-face emoji, is not fetched and is dropped by alice). Fetch order: first HTTP GET the url in the segment; on failure or no url, fall back to NapCat `get_image`/`get_file` to read the local cache file.
- TG inbound albums (multiple images sharing a `media_group_id`, arriving as separate Updates, exactly one of them carrying the caption): buffer by `media_group_id` for 1 second, then merge into a single message (ordered by `message_id`; the lowest one supplies the msg id and the reply target, whichever item has the caption supplies the text) before handing off to the fan.
- Matrix inbound captions follow MSC2530: a captioned image carries the real file name in `filename` and the caption in `body`/`formatted_body`; an uncaptioned one has no `filename` and its `body` *is* the file name (so it is not treated as text).

### Stickers (always normalized to images)
- **TG sticker**: static webp -> use directly as an image. **Animated (`.tgs`, gzipped Lottie) and video (`.webm`) stickers are converted to an animated GIF on alice**, on the TG *inbound* path, before the message reaches the Router.
  - One target format (256px GIF, 25fps) for both destinations, because it is the only moving format QQ and Matrix both render natively. Converting once on inbound also means the single `BridgeMessage` the fan hands to every target already carries the finished bytes — no adapter mutates shared state, and no work is done twice.
  - Conversion is done by `modules/tri_lug_utils/media.py`, shelling out to `lottieconverter` (`.tgs`, after a stdlib gunzip) or `ffmpeg` (`.webm`). Both binaries come from the devShell (`shell.nix`); **they must exist on the machine running the bot**.
  - Results are cached by `file_unique_id` (Telegram's stable content key), capped at 128 entries. **Failures are cached too** — a sticker that will not convert should not cost a subprocess every time it is posted.
  - **Any failure degrades to the old behaviour: the static `sticker.thumbnail` frame**, plus one WARNING. Missing binary, non-zero exit, timeout, or an undownloadable file all take this path. Animated media never costs the message.
  - Conversion latency does **not** eat the §1 staleness budget: `BridgeMessage.ts` is stamped when the message is *built*, which happens after attachment extraction returns, so the 60s check at the fan never sees it. It does not block the Router either — `Router.submit` is a synchronous `put_nowait` downstream of conversion.
  - What it does affect is **inbound ordering**: python-telegram-bot dispatches handlers concurrently, so a message whose sticker takes seconds to convert can be overtaken by a plain text message posted after it. This race is pre-existing (any slow media download does the same) but conversion widens the window; the 20s timeout, the 2-way concurrency cap, and the cache are what bound it. A reorder buffer keyed on a receive sequence number is the fix if this ever actually bites — deliberately not done yet.
- **TG GIF** (`msg.animation`, actually a silent MP4) -> converted to GIF by the same path, but only when it is under **10 seconds and 5 MB**. Over either limit the static thumbnail is bridged instead: a long MP4 makes an enormous GIF, which is slow to produce, overruns QQ's image limits, and bloats the base64 payload on the QQ transport.
- **QQ `mface`** (large store emoji) -> handled exactly like an `image` segment (relay-inlined bytes, falling back to the url).
- **QQ `face`** (small yellow face) -> dropped entirely; there is no name map, so a placeholder would just be noise. A message whose only segments are `face` therefore parses to nothing and is dropped silently.
- **Matrix `m.sticker`** -> already an image, treat as an image.

### Voice notes (QQ -> TG/Matrix, one-way)

- A QQ voice message arrives as a `record` segment. It is forwarded as an **audio attachment**; the reverse direction is out of scope (a Telegram or Matrix voice message is still dropped silently on those sides).
- QQ stores voice as **SILK**, which neither Telegram nor Matrix can play, and the segment's own `url` points at that raw SILK — so unlike images there is no url/file fallback. The relay is the only source of usable bytes: it calls NapCat `get_record` with `out_format=mp3` (NapCat runs it through ffmpeg and returns the converted file inline as base64), stamps the resulting `mime` onto the segment, and inlines the bytes like an image. Converted audio is disk-cached under its own key namespace so it can't collide with an image sharing the same file id.
- A segment that reaches alice without `base64` yields no attachment, so a voice-only message parses to nothing and takes the ordinary log-only path (`segments=['record']`). That is the intended degradation when NapCat has no ffmpeg or the action times out — nothing unplayable is ever forwarded.
- Rendering: TG uses `send_audio` (**not** `send_voice`, which only accepts OGG/OPUS) with the header as caption; Matrix sends an `m.audio` event with an `AudioInfo` mimetype. Since QQ never receives its own messages back, the QQ renderer ignores audio attachments entirely.

### Replies
- On inbound record `reply_to_msg_id` (origin platform native id); the Router resolves it via the IdMap into the target platform native id.
- Native replies per platform: TG `reply_to_message_id`; QQ `reply` segment `{id}`;
  Matrix `m.relates_to.m.in_reply_to` only (mautrix's `set_reply` writes the relation; no `formatted_body` fallback quote block is generated). Inbound, the fallback quote block is stripped before flattening, so a quoted reply never leaks into the bridged text.
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

### Share cards (QQ mini-app / 小卡片)

- QQ share cards arrive as a `json` message segment. Three share sources are recognized and forwarded as a plain `"{title}\n{url}"` line; everything else (unknown apps) falls through to the log-only path below.
  - **bilibili** (`meta.detail_1`, link in `qqdocurl` on host `b23.tv`): title is the card `desc`; the `b23.tv` short link is expanded to its canonical long URL.
  - **zhihu** (`meta.detail_1`, link in `qqdocurl` on host `*.zhihu.com`): title is the card `desc`; URL used as-is.
  - **weixin** (`meta.news`, link in `jumpUrl` on host `mp.weixin.qq.com`): title is the card `title`; URL used as-is.
- Dispatch is by URL host (not appid), and share-tracking query params are stripped from every URL. The card JSON may carry CQ HTML entities (`&#44;` etc.); it is JSON-decoded with an unescape fallback.
- Card-to-text translation is pure (`onebot.py`). The bilibili short-link expansion is the one network hop and lives in the QQ adapter (following a single redirect, keeping scheme+host+path), so the pure parser stays testable; a failed resolve forwards the short link unchanged. The resolver is injectable, which keeps the tests network-free. The same expansion also applies to any `b23.tv` link pasted as plain text.

### Other messages

- Messages outside the v1 scope (video/file, unrecognized cards, TG/Matrix voice, etc.) are not forwarded. (QQ voice *is* forwarded — see Voice notes above; TG GIFs *are* forwarded, as converted animations — see Stickers above.)
- The log annotation is a **QQ-side feature only**: a group event that parses to nothing bridgeable is dropped after one WARNING `[QQ][log-only · not forwarded] <type + segment/notice summary>`. alice does the annotating rather than flagging it in the RabbitMQ payload, because the relay does no translation and cannot tell whether something is bridgeable.
  - Exception — **noise types are dropped silently, without any log line**: the `group_msg_emoji_like` and `group_recall` notices, `sub_type=poke`, and messages whose segments are all `face`. These recur often enough that logging them is pure spam.
- TG and Matrix have no equivalent annotation: an event with no bridgeable content is dropped silently on those sides.
- QQ side: the relay sends the raw event to alice over RabbitMQ even when it isn't bridgeable (that is what makes the annotation above possible), dropping only `meta_event` (heartbeat/lifecycle) to avoid pointlessly flooding RabbitMQ.



## 3. Runtime control (/stop_bridge, /start_bridge)

- All three ends support the plain-text commands `/stop_bridge` and `/start_bridge`: QQ/Matrix trigger on receiving the command as plain text (intercepted inside the adapter before forwarding; the command itself is not bridged as a message); TG handles it as a command (CommandHandler), effective only in the bridged group. Any member may trigger it.
- `/stop_bridge` pauses the forwarding of messages and pins; `/start_bridge` resumes it. The pause is in-process runtime state and is not persisted: a process restart returns to the running state.
- This is a different concept from `ENABLED`: when `ENABLED=False` the entire bridge module does not work at all, and even these two commands are not registered; pausing only suspends forwarding at runtime while the module keeps running.
- During a pause the commands themselves are still processed (commands are intercepted on inbound before the "pause drop"), so `/start_bridge` can resume from the paused state.
