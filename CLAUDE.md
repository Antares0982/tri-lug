# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tri-lug` is a three-way group-chat message bridge between **Telegram ⇄ QQ ⇄ Matrix**. It is not a standalone app: it is a plugin **module** (`modules/tri_lug.py`) for the external [antares-bot](https://github.com/Antares0982/antares-bot) Telegram bot framework. `main.py` just calls `antares_bot.__main__.bootstrap().run()`; antares-bot discovers and loads the module, drives its lifecycle (`do_init` → `post_init` → `do_stop`), and registers its Telegram handlers (`mark_handlers`).

`docs/design.md` is the authoritative spec — read it before changing forwarding, reply, media, avatar, pin, or pause behavior. The notes below are the map; `design.md` has the per-feature rules.

## Environment, run, test

The Python environment (incl. the `antares_bot` dependency, `mautrix`, `aiosqlite`, `aio_pika`) is provided by Nix:

```bash
nix develop          # enter the dev shell (flake provides python3.14 + all deps)
python main.py       # run the bot (needs a populated bot_cfg.py + live broker/appservice)
```

`bot_cfg.py` holds all secrets and config (`TriLugConfig`, broker creds, Matrix tokens) and is **gitignored** — it exists locally but is never committed.

Tests are **plain assert-based scripts, not pytest**, and are fully network-free (RabbitMQ/NapCat/Matrix/Telegram all stubbed, `asyncio.sleep` injected so the 3s pacing is asserted via recorded durations rather than real waiting). Run each from the repo root:

```bash
python -m tests.tri_lug_mock_demo           # Router + IdMap + MockAdapters spine
python -m tests.tri_lug_qq_demo             # OneBot11 <-> BridgeMessage + QQAdapter
python -m tests.tri_lug_qq_transport_demo   # RabbitMQ transport RPC/echo correlation
```

Each prints `ALL ... CHECKS PASSED` on success; a failed `assert` is the failure signal.

Lint with **ruff** (`ruff check` / `ruff format`; only the `.ruff_cache/` is checked in, no config file ⇒ defaults).

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

### Adapters (`adapters.py` + per-platform files)
`BaseAdapter` defines `send` (outbound render) and `_emit` (inbound → `Router.submit`). One adapter owns one platform's side of one room.
- **`TelegramAdapter`** (`tg_adapter.py`) — live, driven by antares-bot's handler dispatch via `on_update`.
- **`QQAdapter`** (`qq_adapter.py`) — talks to a remote NapCat instance through the **`QQTransport` abstraction** (`onebot.py` does the pure OneBot11 ⇄ BridgeMessage translation). The only concrete transport is `RabbitMQQQTransport` (`qq_rabbitmq.py`), which pairs with a separate `tri-lug-qq-relay` on the QQ machine; both dial out to the broker. The transport is intentionally swappable — keep transport concerns out of `onebot.py`/`qq_adapter.py`. Avatar bytes come over a separate request/response RPC (`qq.avatar_req`/`qq.avatar_resp`, echo-correlated) so alice never touches Tencent's CDN.
- **`MatrixAdapter`** (`matrix_adapter.py`) — mautrix appservice; uses **ghost/puppet** users (no text prefix) instead of the `[label] name:` header that TG/QQ use (`header.py`).
- **`MockAdapter`** — logging-only stand-in. Each platform independently degrades to a mock when its `*_ENABLED` flag is off, so the bot still runs (and slash commands still work) before that transport is wired.

### IdMap (`idmap.py`)
aiosqlite-backed cross-platform id map. Rows sharing a `logical_id` represent one logical message's native ids across platforms, enabling reply re-pointing. A `_link_lock` serializes the read→allocate→insert in `link()` so concurrent fan-outs can't merge into one logical id. TTL 24h, purged hourly by a background task in `tri_lug.py`. Use `":memory:"` for tests.

## Conventions that matter

- **Loop prevention lives in each adapter**, before `_emit` — an adapter must drop messages authored by its own bridge identity (e.g. `QQ_SELF_UIN`, the Matrix bot, the TG bot) or the bridge echoes forever.
- **Two kinds of "off":** `ENABLED=False` makes the module fully inert (no handlers, not even the pause commands). The runtime **pause** (`/stop_bridge` / `/start_bridge`, recognized on all three platforms) only suspends forwarding while the module keeps running, and is **not persisted** — a restart returns to running. Control commands are intercepted in the adapter *before* the pause drop, so `/start_bridge` always works.
- **Per-platform enable flags** (`TG_ENABLED`/`QQ_ENABLED`/`MATRIX_ENABLED` in `TriLugConfig`) select real adapter vs `MockAdapter` in the `_build_*_adapter` methods — the wiring pattern for bringing one side up at a time.
- Out-of-scope events (video/audio/file/poke/recall, QQ small `face` emoji, etc.) are **not forwarded but must be logged** (`[<platform>][log-only - not forwarded] ...`); alice does this annotation since the relay can't tell what's bridgeable.
