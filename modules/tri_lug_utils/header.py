"""Outbound identity header — `[label] name:` plus optional regex rewrites.

Every text-prefix target (TG and QQ; Matrix puppets the displayname instead and
does not use this) renders the cross-platform author header through
`render_header`, so the platform-label map and the rewrite rules live in exactly
one place.

Rewrites come from `TriLugConfig.HEADER_REWRITES` (optional, absent ⇒ no-op): an
ordered list of dicts

    {"pattern": <regex str>, "repl": <str>, "target": <platform str | None>}

Each rule is applied with `re.sub` to the *whole* header string, in list order;
a rule carrying a `target` only fires when rendering into that target platform
(e.g. `"qq"`), while `target` absent/None means it applies to every target. The
rules are compiled once on first use, so config changes need a restart — same as
the label map itself.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.tri_lug_utils.bridge_message import BridgeMessage

# How a message's *origin* platform is labelled when rendered into a text-prefix
# target. (Matrix keeps its own copy — its displayname form differs.)
_PLATFORM_LABEL = {"tg": "TG", "qq": "QQ", "matrix": "Matrix"}


@lru_cache(maxsize=1)
def _rules() -> tuple[tuple[re.Pattern[str], str, str | None], ...]:
    try:
        from bot_cfg import TriLugConfig

        raw = getattr(TriLugConfig, "HEADER_REWRITES", []) or []
    except Exception:
        raw = []
    return tuple((re.compile(r["pattern"]), r["repl"], r.get("target")) for r in raw)


def render_header(msg: "BridgeMessage", target_platform: str) -> str:
    """Build `[label] name:` for `msg`'s author and apply the configured
    rewrites for the given `target_platform`."""
    label = _PLATFORM_LABEL.get(msg.platform, msg.platform)
    header = f"[{label}] {msg.sender.display_name}:"
    for pattern, repl, target in _rules():
        if target is not None and target != target_platform:
            continue
        header = pattern.sub(repl, header)
    return header
