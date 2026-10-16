"""`ccr tune`: propose edits to `.ccr/config.yaml` from misroute evidence, as a diff you approve.

ccr never edits its own rules on its own. This only reads history (the same evidence as
`ccr stats --misroutes`) and turns it into concrete, minimal config changes:
  hard_terms        a cue word whose tasks keep failing at the tier it routes to
  class_base.<k>    a class that keeps failing (raise one tier) or keeps being over-routed (lower)
Changes are plain text edits so your comments and ordering survive.
"""
from __future__ import annotations

import difflib
import re

from ccr.config import TIERS
from ccr.history import savings

MAX_BASE = TIERS.index("T3")


def propose(records: list[dict], cfg) -> list[dict]:
    hc = cfg["history"]
    k = int(hc["min_samples"])
    mis = savings.misroutes(records, cfg)
    changes: list[dict] = []
    have = {t.lower() for t in cfg["hard_terms"]}
    for c in mis["cues"]:
        term = c["term"]
        if term.lower() not in have and 0 < len(term) <= 24:
            n = c["ok"] + c["failed"]
            changes.append({"kind": "hard_term", "term": term,
                            "why": f"cue '{term}' failed {c['failed']}/{n} at {c['tier']}"})
            have.add(term.lower())
    by_class: dict[tuple[str, str], list[str]] = {}
    for it in mis["items"]:
        if it.get("class") and it.get("tier"):
            by_class.setdefault((it["kind"], it["class"]), []).append(it["tier"])
    for (kind, cls), tiers in sorted(by_class.items()):
        base = cfg["class_base"].get(cls)
        if base not in TIERS or len(tiers) < k or any(t != base for t in tiers[:k]):
            continue
        i = TIERS.index(base)
        new = i + 1 if kind == "under" else i - 1
        floor = 0 if cls in savings.NO_FLOOR else 1
        if kind == "under" and new > MAX_BASE or kind == "over" and new < floor:
            continue
        changes.append({"kind": "class_base", "class": cls, "from": base, "to": TIERS[new],
                        "why": f"{len(tiers)} {kind}-routed {cls} task(s) at {base}"})
    return changes


_HARD = re.compile(r"^hard_terms:\s*\[(.*?)\]\s*(#.*)?$", re.M)


def apply_text(text: str, changes: list[dict]) -> str:
    for ch in changes:
        if ch["kind"] == "hard_term":
            m = _HARD.search(text)
            if m:
                items = [x.strip() for x in m.group(1).split(",") if x.strip()]
                items.append(f'"{ch["term"]}"' if " " in ch["term"] else ch["term"])
                text = text[:m.start()] + f"hard_terms: [{', '.join(items)}]" \
                    + (f"  {m.group(2)}" if m.group(2) else "") + text[m.end():]
            else:
                text = text.rstrip("\n") + f'\n\nhard_terms: [{ch["term"]}]\n'
        else:
            cls, to = ch["class"], ch["to"]
            line = re.compile(rf"^(\s+){cls}:\s*\S+", re.M)
            if line.search(text) and re.search(r"^class_base:\s*$", text, re.M):
                text = line.sub(rf"\g<1>{cls}: {to}", text, count=1)
            elif re.search(r"^class_base:\s*$", text, re.M):
                text = re.sub(r"^class_base:\s*$", f"class_base:\n  {cls}: {to}", text, count=1,
                              flags=re.M)
            else:
                text = text.rstrip("\n") + f"\n\nclass_base:\n  {cls}: {to}\n"
    return text


def diff(old: str, new: str, name: str = ".ccr/config.yaml") -> list[str]:
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}",
                                     f"b/{name}", lineterm=""))
