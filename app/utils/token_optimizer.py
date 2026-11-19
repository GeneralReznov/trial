"""Bounded, deterministic context compaction for AI prompts.

This is intentionally not a lossy answer generator. It only removes empty
fields, normalizes whitespace, caps long values, and keeps recent conversation
turns. Provider calls remain understandable and bounded when a learner uploads
large context or has a long session history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_WHITESPACE = re.compile(r"\s+")


def estimate_tokens(value: Any) -> int:
    """Estimate model tokens conservatively without making another API call."""
    if isinstance(value, str):
        chars = len(value)
    else:
        chars = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
    return max(1, (chars + 3) // 4)


def compact_text(value: Any, max_chars: int = 2000, *, preserve_ends: bool = False) -> str:
    """Normalize and cap text while retaining useful context at both ends."""
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    limit = max(40, max_chars)
    if not preserve_ends:
        return f"{text[:limit]}\n[context shortened]"
    head = max(20, int(limit * 0.72))
    tail = max(20, limit - head - 24)
    return f"{text[:head]} … [context shortened] … {text[-tail:]}"


def compact_value(value: Any, *, depth: int = 0, max_items: int = 12) -> Any:
    """Return a small JSON-safe representation of nested context."""
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        return compact_text(value, 420)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                result["__more__"] = f"{len(value) - max_items} fields omitted"
                break
            compacted = compact_value(item, depth=depth + 1, max_items=max_items)
            if compacted not in (None, "", [], {}):
                result[str(key)[:80]] = compacted
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        items = list(value)
        result = [compact_value(item, depth=depth + 1, max_items=max_items) for item in items[:max_items]]
        if len(items) > max_items:
            result.append(f"{len(items) - max_items} items omitted")
        return result
    return str(value)


def compact_mapping(value: Mapping[str, Any] | None, max_chars: int = 3000) -> str:
    """Serialize a mapping compactly for a prompt, omitting empty settings."""
    compacted = compact_value(value or {})
    return compact_text(
        json.dumps(compacted, ensure_ascii=False, separators=(",", ":"), default=str),
        max_chars,
        preserve_ends=True,
    )


def compact_history(
    history: Sequence[Mapping[str, Any]] | None,
    *,
    max_turns: int = 8,
    max_chars: int = 5000,
) -> str:
    """Keep the most recent useful turns within a predictable character budget."""
    rows: list[str] = []
    for item in list(history or [])[-max_turns:]:
        if not isinstance(item, Mapping):
            continue
        role = compact_text(item.get("role", "user"), 32)
        content = compact_text(item.get("content", ""), 700)
        if content:
            rows.append(f"{role}: {content}")
    return compact_text("\n".join(rows), max_chars, preserve_ends=True)


def compact_transcript(transcript: str, max_chars: int = 9000) -> str:
    """Preserve the beginning and latest context of a long session transcript."""
    return compact_text(transcript, max_chars, preserve_ends=True)