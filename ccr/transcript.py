"""Token usage from Claude Code transcript JSONL files (stdlib only; safe for hooks).

Claude Code writes one line per content block of an assistant message, each repeating the
message's `usage`, so usage is counted once per `message.id` (last line wins). Reads can be
incremental: pass the byte offset returned by the previous call (only complete lines are
consumed). Message text is never returned.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")
MAX_CHUNK = 64 * 1024 * 1024


@dataclass
class Usage:
    tokens: int = 0                  # input + output + cache-creation tokens
    cache_read: int = 0
    messages: int = 0                # distinct assistant messages
    tool_errors: int = 0             # tool results flagged is_error
    model: str | None = None         # model of the last assistant message
    offset: int = 0                  # byte offset after the last complete line read
    models: dict[str, int] = field(default_factory=dict)


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def read_usage(path, offset: int = 0, seen: set | None = None,
               complete_only: bool = True) -> Usage:
    """Usage in `path` after byte `offset`. `seen` (message ids already counted by an earlier
    incremental call) is updated in place so a message split across reads counts once.
    With `complete_only` a trailing line without newline is left for the next call."""
    u = Usage(offset=offset)
    seen = set() if seen is None else seen
    per_msg: dict[str, tuple[int, int, str | None]] = {}
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(MAX_CHUNK)
    except (OSError, TypeError, ValueError):
        return u
    end = data.rfind(b"\n") if complete_only else len(data) - 1
    if end < 0:
        return u
    u.offset = offset + end + 1
    anon = 0
    for raw in data[: end + 1].splitlines():
        if b'"usage"' not in raw and b'"is_error"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and \
                        block.get("is_error") is True:
                    u.tool_errors += 1
        usage = msg.get("usage")
        if obj.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        mid = msg.get("id")
        if not isinstance(mid, str):
            anon += 1
            mid = f"_anon{offset}_{anon}"
        tokens = sum(_int(usage.get(k)) for k in TOKEN_KEYS)
        model = msg.get("model") if isinstance(msg.get("model"), str) else None
        per_msg[mid] = (tokens, _int(usage.get("cache_read_input_tokens")), model)
        if model and not model.startswith("<"):
            u.model = model
    for mid, (tokens, cache_read, model) in per_msg.items():
        if mid in seen:
            continue
        seen.add(mid)
        u.tokens += tokens
        u.cache_read += cache_read
        u.messages += 1
        if model and not model.startswith("<"):
            u.models[model] = u.models.get(model, 0) + 1
    return u
