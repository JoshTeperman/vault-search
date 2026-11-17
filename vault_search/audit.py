"""Append-only JSONL audit log. Never writes secret values."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vault_search.identity import Caller

_MAX_ARG = 200


def _safe_text(value: Any) -> str:
    from vault_search.secrets import mask

    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return mask(text)[:_MAX_ARG]


def summarize_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Compact, masked argument summary. Omits empty values."""
    if not args:
        return {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, bool):
            out[str(key)] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            out[str(key)] = value
        elif isinstance(value, (list, tuple)):
            out[str(key)] = [_safe_text(item) for item in value[:20]]
        else:
            out[str(key)] = _safe_text(value)
    return out


def result_count(tool: str, text: str) -> int:
    body = (text or "").strip()
    if body in {"", "not authorized", "not_found", "No results.", "No notes."}:
        return 0
    if tool in {"search", "list_notes", "list_projects"}:
        return sum(1 for line in body.splitlines() if line.startswith("- **"))
    if tool == "read_note":
        return 1
    return 1 if body else 0


def masked_hit_count(text: str) -> int:
    # Tools already mask output; count placeholders rather than raw secrets.
    return (text or "").count("[REDACTED:")


def append_event(
    path: Path | None,
    *,
    caller: Caller,
    tool: str,
    args: dict[str, Any] | None,
    result_text: str,
    denied: bool = False,
) -> dict[str, Any]:
    event = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "device": caller.device,
        "node": caller.node,
        "tags": list(caller.tags),
        "agent_name_unverified": caller.agent_name_unverified,
        "tool": tool,
        "args": summarize_args(args),
        "result_count": 0 if denied else result_count(tool, result_text),
        "masked_hits": 0 if denied else masked_hit_count(result_text),
        "denied": denied,
        "deny_reason": caller.deny_reason if denied else None,
    }
    if path is None:
        return event
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return event
