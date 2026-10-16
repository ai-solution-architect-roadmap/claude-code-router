"""Best-effort secret redaction for stored prompts (stdlib only; safe for hooks).

Prompts can paste keys, tokens, connection strings or customer emails. Redaction runs before a
prompt is written to `.ccr/local/prompts.jsonl` (`hooks.redact_prompts`, on by default). It is a
safety net, not a guarantee: unusual secrets can slip through, so keep `store_prompts` off for
projects where prompts hold sensitive data.
"""
from __future__ import annotations

import re

_I = re.I
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("api-key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", _I)),
    ("url-credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@", _I)),
    ("secret-assignment", re.compile(
        r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth)\b\s*[:=]\s*"
        r"[\"']?[^\s\"',;]{6,}", _I)),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("long-token", re.compile(
        r"\b(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}\b")),
]


def redact(text: str) -> tuple[str, list[str]]:
    """(text with secrets replaced by [redacted:kind], kinds that were found)."""
    found: list[str] = []
    for kind, rx in PATTERNS:
        def repl(m, kind=kind):
            if kind == "secret-assignment":
                head = re.split(r"[:=]", m.group(0), maxsplit=1)[0]
                return f"{head}=[redacted:{kind}]"
            return f"[redacted:{kind}]"
        new, n = rx.subn(repl, text)
        if n:
            found.append(kind)
            text = new
    return text, found
