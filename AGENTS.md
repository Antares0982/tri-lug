# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## What this is

`tri-lug` is a three-way group-chat message bridge between **Telegram ⇄ QQ ⇄ Matrix**. It is not a standalone app: it is a plugin **module** (`modules/tri_lug.py`) for the external [antares-bot](https://github.com/Antares0982/antares-bot) Telegram bot framework. `main.py` just calls `antares_bot.__main__.bootstrap().run()`; antares-bot discovers and loads the module, drives its lifecycle (`do_init` → `post_init` → `do_stop`), and registers its Telegram handlers (`mark_handlers`).

`docs/design.md` is the authoritative spec — read it before changing forwarding, reply, media, avatar, pin, or pause behavior. The notes below are the map; `design.md` has the per-feature rules.

External API reference for the QQ side: **NapCat** — <https://napneko.github.io/api/4.18.13> (the trailing `4.18.13` is the NapCat version and changes over time; bump it to whatever version the relay machine actually runs before trusting the page). It documents the OneBot11 actions/segments this repo calls, e.g. `send_group_msg`, `get_group_member_info`, `get_image`.

## Environment, run, test

The Python environment (incl. the `antares_bot` dependency, `mautrix`, `aiosqlite`, `aio_pika`) is provided by Nix:

```bash
nix develop          # enter the dev shell (flake provides python3.14 + all deps)
python main.py       # run the bot (needs a populated bot_cfg.py + live broker/appservice)
```

`bot_cfg.py` holds all secrets and config (`TriLugConfig`, broker creds, Matrix tokens) and is **gitignored** — it exists locally but is never committed.

Tests are **pytest**, assert-based, and fully network-free (RabbitMQ/NapCat/Matrix/Telegram all stubbed, `asyncio.sleep` injected so the 3s pacing is asserted via recorded durations rather than real waiting). Shared setup lives in `tests/conftest.py` (the `make_bridge`/`idmap`/`make_qq_transport` fixtures and the `FakeQQTransport` / event-builder helpers); `pyproject.toml` sets `asyncio_mode = "auto"` so `async def test_*` run without per-test markers. Run from the repo root:

```bash
pytest                                  # whole suite
pytest tests/test_tri_lug_qq.py         # one file
pytest tests/test_tri_lug_mock.py::test_pacing_gap   # one test
```

- `test_tri_lug_mock.py` — Router + IdMap + MockAdapters spine
- `test_tri_lug_qq.py` — OneBot11 ⇄ BridgeMessage + QQAdapter
- `test_tri_lug_qq_transport.py` — RabbitMQ transport RPC/echo correlation

Lint with **ruff** (`ruff check` / `ruff format`). There is no ruff config ⇒ defaults, and the devShell does not ship ruff — use the system one.

## Architecture

### The Router pipeline (`router.py`)
The hub is a **two-stage async-queue pipeline**, deliberately split so receipt can ack immediately while delivery is paced off to the side:

```
inbound event → adapter._emit → Router.submit
  → msg-in queue (one per SOURCE, single worker ⇒ preserves that source's order)
  → fan  (drops messages older than stale_seconds=60s; seeds IdMap with origin id)
  → msg-out queue (one per TARGET, worker keeps ≥ send_gap_seconds=3s between real sends)
```

Staleness is checked **only at the fan handoff**, never again in msg-out (the pacing delay must not count against a message). Clocks and `sleep` are constructor-injected so the whole pipeline is testable without real time. A single source message may fan into several native messages on a target (e.g. Matrix text event + image event); `adapter.send` returns **all** native ids and the Router links every one into the IdMap.

### Neutral message model (`bridge_message.py`)
Adapters translate platform events into `BridgeMessage` (text + ordered `Attachment[]` + `BridgeUser` sender + `reply_to_msg_id`) on the way in and render it back out. `msg_id`/`reply_to_msg_id` are always the **origin platform's** native ids; the Router resolves replies to the target's id via the IdMap before calling `send`. Stickers are normalized to `kind="image"`. `sniff_image_mime` exists because mautrix won't auto-detect MIME without libmagic.

**Animated media** (`media.py`) is converted to a single 256px GIF on the TG **inbound** path — deliberately before the fan, since `_fan` hands the *same* `BridgeMessage` object to every target, so one conversion serves both and no adapter has to mutate shared state. It shells out to `lottieconverter`/`ffmpeg` (in the devShell) and degrades to the sticker's static thumbnail on any failure, so a host without those binaries still bridges. Results are cached by `file_unique_id`, failures included. Outbound to TG, animated images must go via `send_animation`: `send_photo` re-encodes to a static JPEG, which was the original "GIFs don't move" bug.

### Adapters (`adapters.py` + per-platform files)
`BaseAdapter` defines `send` (outbound render) and `_emit` (inbound → `Router.submit`). One adapter owns one platform's side of one room.
- **`TelegramAdapter`** (`tg_adapter.py`) — driven by antares-bot's handler dispatch via `on_update`.
- **`QQAdapter`** (`qq_adapter.py`) — talks to a remote NapCat instance through the **`QQTransport` abstraction** (`onebot.py` does the pure OneBot11 ⇄ BridgeMessage translation). The only concrete transport is `RabbitMQQQTransport` (`qq_rabbitmq.py`), which pairs with a separate `tri-lug-qq-relay` on the QQ machine; both dial out to the broker. The transport is intentionally swappable — keep transport concerns out of `onebot.py`/`qq_adapter.py`. Avatar bytes come over a separate request/response RPC (`qq.avatar_req`/`qq.avatar_resp`, echo-correlated) so alice never touches Tencent's CDN.
  - **The relay lives outside this repo**, in `$NIX_DOT_FILES/rpi/qq-relay/qq_napcat_relay.py` (single file: NapCat websocket ⇄ RabbitMQ, image byte fetching + disk cache, the avatar RPC). The two sides share an undeclared wire contract — routing keys, the `echo` correlation, the `base64`/`ts` fields injected into `qq.event`, `TRI_LUG_EXCHANGE`. **Anything that touches that contract has to be changed on both sides in the same pass**, and neither repo's tests will catch a mismatch (this side's suite stubs the transport entirely).
- **`MatrixAdapter`** (`matrix_adapter.py`) — mautrix appservice; uses **ghost/puppet** users (no text prefix) instead of the `[label] name:` header that TG/QQ use (`header.py`). It is also the only adapter that reads `formatted_body`, flattening the HTML so `<a href>` hyperlinks survive as `[text](url)`.
- **`MockAdapter`** — logging-only stand-in. Each platform independently degrades to a mock when its `*_ENABLED` flag is off, so the bot still runs (and slash commands still work) with that transport detached.

### IdMap (`idmap.py`)
aiosqlite-backed cross-platform id map. Rows sharing a `logical_id` represent one logical message's native ids across platforms, enabling reply re-pointing. A `_link_lock` serializes the read→allocate→insert in `link()` so concurrent fan-outs can't merge into one logical id. TTL 24h, purged hourly by a background task in `tri_lug.py`. Use `":memory:"` for tests.

## Conventions that matter

- **Loop prevention lives in each adapter**, before `_emit` — an adapter must drop messages authored by its own bridge identity (e.g. `QQ_SELF_UIN`, the Matrix bot, the TG bot) or the bridge echoes forever.
- **Two kinds of "off":** `ENABLED=False` makes the module fully inert (no handlers, not even the pause commands). The runtime **pause** (`/stop_bridge` / `/start_bridge`, recognized on all three platforms) only suspends forwarding while the module keeps running, and is **not persisted** — a restart returns to running. Control commands are intercepted in the adapter *before* the pause drop, so `/start_bridge` always works.
- **Per-platform enable flags** (`TG_ENABLED`/`QQ_ENABLED`/`MATRIX_ENABLED` in `TriLugConfig`) select real adapter vs `MockAdapter` in the `_build_*_adapter` methods — the wiring pattern for bringing one side up at a time.
- **The `[label] name:` header has exactly one renderer** (`header.render_header`), called by the TG and QQ adapters. `TriLugConfig.HEADER_REWRITES` (optional) post-processes it with an ordered list of `{"pattern", "repl", "target"}` `re.sub` rules — `target` scopes a rule to one destination platform. Rules are `lru_cache`d ⇒ a config change needs a restart.
- Out-of-scope events (file, unrecognized cards, etc.) are not forwarded — but QQ `record` (voice) and `video` **are**, from the bytes the relay inlines; the relay downscales an oversized video with ffmpeg first, off its ordered event queue (see `docs/design.md`). **Only the QQ side annotates them**: `qq_adapter` logs one WARNING `[QQ][log-only · not forwarded] <describe_event(...)>`, since the relay can't tell what's bridgeable. `onebot.is_noise_event` carves out the high-frequency noise that is dropped *without* a log line (emoji-like and recall notices, pokes, `face`-only messages). TG and Matrix drop unbridgeable events silently.
