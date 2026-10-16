"""A plain-language checklist of what ccr looked at before choosing a tier (model + effort).

Built from a live routing Decision (`from_decision`) or, for older history records, from the
routing signals kept in the record's `trace` (`from_trace`). Each check is
{n, title, looked_at, found, effect, move} where `move` is up | down | rule | none.
`chain` is the short story: where it started, what moved it, where it ended.
"""
from __future__ import annotations

import re

_MOVE = re.compile(r"^(\+\d|-\d)\b")


def _check(n: int, title: str, looked: str, found: str, effect: str, move: str = "none") -> dict:
    return {"n": n, "title": title, "looked_at": looked, "found": found, "effect": effect,
            "move": move}


def _terms(xs, k=4) -> str:
    xs = [str(x) for x in (xs or [])][:k]
    return ", ".join(f"“{x}”" for x in xs)


def _scope_effect(scope: str, bumps: list[str], downs: list[str]) -> tuple[str, str]:
    if "large scope" in bumps:
        return "Raises the tier by 1", "up"
    if any(d.startswith("small scope") for d in downs):
        return "Lowers the tier by 1", "down"
    return ("No change", "none") if scope != "unspecified" else ("No change (scope not stated)", "none")


def _downs(reasons: list[str]) -> list[str]:
    return [r.split(": ", 1)[1] for r in reasons if r.startswith("-1: ") and "history" not in r
            and "budget" not in r]


def from_decision(d, tier) -> dict:
    tc, loc, r = d.task_class, d.location, d.risk
    bumps = list(d.bumps)
    reasons = list(d.reasons)
    downs = _downs(reasons)
    checks = []
    kind_found = f"a {tc.kind} task (confidence {tc.confidence:.2f}, decided by {tc.source})"
    if tc.matched.get(tc.kind):
        kind_found += f"; matched words {_terms(tc.matched[tc.kind])}"
    if tc.secondary:
        kind_found += f"; runner-up: {tc.secondary}"
    if d.reclassed:
        kind_found += "; the report names no code, so it is treated as debugging"
    checks.append(_check(1, "What kind of task is it?", "the wording of the prompt", kind_found,
                         f"Starting point: {d.base}", "none"))
    eff, mv = _scope_effect(tc.scope, bumps, downs)
    scope_found = f"scope {tc.scope}" + (f" ({_terms(tc.scope_terms)})" if tc.scope_terms else "")
    if tc.steps:
        scope_found += f"; {tc.steps} explicit steps given"
        if any("step-by-step" in x for x in downs):
            eff, mv = "Lowers the tier by 1 (you already planned the work)", "down"
    if tc.sweeping:
        scope_found += f"; sweeping change {_terms(tc.sweeping)}"
        eff, mv = "Raises the tier (a plan is needed first)", "up"
    checks.append(_check(2, "How big is it?", "size words, explicit steps",
                         scope_found, eff, mv))
    vague = f"vagueness {tc.vagueness:.2f}" + (f" ({_terms(tc.vague_terms)})" if tc.vague_terms else "")
    extra = []
    if tc.hard:
        extra.append(f"hard-problem words {_terms(tc.hard)}")
    if tc.destructive:
        extra.append(f"destructive words {_terms(tc.destructive)}")
    wording_up = [b for b in bumps if b.startswith(("vague bug", "hard problem"))]
    checks.append(_check(3, "How clear and how hard does it sound?",
                         "vague wording, intermittent or hard-problem words, destructive words",
                         "; ".join([vague, *extra]),
                         ("Raises the tier: " + ", ".join(wording_up)) if wording_up else "No change",
                         "up" if wording_up else "none"))
    if loc.candidates:
        files = "; ".join(f"{c.path} ({c.confidence:.2f})" for c in loc.candidates[:3])
        found = f"{len(loc.candidates)} candidate file(s), location confidence {loc.confidence:.2f}: {files}"
    else:
        found = "no file could be tied to the prompt"
    checks.append(_check(4, "Which code does it touch?", "file names, symbols and the code graph",
                         found, "Needed for the risk checks below", "none"))
    comp = r.components or {}
    if r.score is None:
        risk_found, risk_eff, risk_mv = "unknown, because no files were located", "No change", "none"
    else:
        parts = [f"{n.replace('_', ' ')} {c.value:.2f}" for n, c in comp.items()
                 if n in ("blast_radius", "complexity", "test_proximity", "churn")]
        risk_found = f"risk {r.score:.2f} ({r.level}): " + ", ".join(parts)
        hi = [b for b in bumps if b.startswith(("risk high", "complex code", "unclassified"))]
        risk_eff = ("Raises the tier: " + ", ".join(hi)) if hi else "No change"
        risk_mv = "up" if hi else "none"
    checks.append(_check(5, "How risky is that code?",
                         "dependents (blast radius), complexity, tests nearby, recent churn",
                         risk_found, risk_eff, risk_mv))
    crit = [f"critical path: {'/'.join(r.critical_groups)}"] if r.critical_groups else []
    mem = sorted({f.kind for f in r.memory_flags}) if r.memory_flags else []
    if mem:
        crit.append("project notes mark it " + "/".join(mem))
    crit_up = [b for b in bumps if b.startswith(("critical path", "memory flags"))]
    checks.append(_check(6, "Is it a critical or protected area?",
                         "auth, payments, migrations, infra, secrets paths; CLAUDE.md notes",
                         "; ".join(crit) or "no critical path or flagged file",
                         "Raises the tier (never lowered here)" if crit_up else "No change",
                         "up" if crit_up else "none"))
    checks.append(_check(7, "What did past tasks like this do?", "this project's recorded outcomes",
                         str(d.history), "Moves the tier" if re.search(r"[+-]1", str(d.history)) and
                         "no change" not in str(d.history) else "No change", "none"))
    t4 = list(d.t4_rules)
    checks.append(_check(8, "Does a safety rule force the top tier?",
                         "security on a critical path, destroying production data, protected files",
                         "; ".join(t4) or "no", "Forces T4" if t4 else "No change",
                         "rule" if t4 else "none"))
    checks.append(_check(9, "Is the weekly budget under pressure?", "usage from the status line",
                         str(d.budget.detail), "Lowers the tier by 1" if d.budget.pressure
                         else "No change", "down" if d.budget.pressure else "none"))
    return {"checks": checks, "chain": _chain(d.base, reasons, tier), "effort_note": _effort(tier)}


def _chain(base: str, reasons: list[str], tier) -> list[str]:
    out = [f"Start at {base}"]
    for x in reasons:
        if _MOVE.match(x) or x.startswith(("T4 rule", "capped")):
            out.append(x if len(x) < 140 else x[:137] + "…")
    out.append(f"Final {tier.name}: {tier.model} / {tier.effort}")
    return out


def _effort(tier) -> str:
    return (f"Model and effort come together with the tier: {tier.name} runs {tier.model} at "
            f"{tier.effort} effort, through the {tier.agent} agent.")


def from_trace(row: dict, tier_models: dict) -> dict:
    """Checklist for a record without a stored decision (only the trace signals are known)."""
    t = row.get("trace") if isinstance(row.get("trace"), dict) else {}
    if not t:
        return {"checks": [], "chain": [], "effort_note": ""}
    bumps = list(t.get("bumps") or [])
    final = row.get("tier")
    checks = [
        _check(1, "What kind of task is it?", "the wording of the prompt",
               f"{row.get('class') or '?'} (confidence {t.get('class_conf', '?')}, "
               f"{t.get('class_source', 'rules')})"
               + (f"; matched {_terms(t.get('rules'))}" if t.get("rules") else ""),
               f"Starting point: {t.get('base', '?')}"),
        _check(2, "How big is it?", "size words, explicit steps",
               f"scope {t.get('scope', '?')}" + (f"; {t['steps']} steps" if t.get("steps") else ""),
               "Raises the tier by 1" if "large scope" in bumps else "No change",
               "up" if "large scope" in bumps else "none"),
        _check(3, "How clear and how hard does it sound?", "vague, intermittent or destructive words",
               f"vagueness {t.get('vagueness', '?')}"
               + (f"; hard words {_terms(t.get('hard_terms'))}" if t.get("hard_terms") else "")
               + ("; destructive" if t.get("destructive") else ""),
               "Raises the tier" if any(b.startswith(("vague bug", "hard problem")) for b in bumps)
               else "No change",
               "up" if any(b.startswith(("vague bug", "hard problem")) for b in bumps) else "none"),
        _check(4, "Which code does it touch?", "the code graph",
               "code located" if t.get("located") else "no file could be tied to the prompt",
               "Needed for the risk checks", "none"),
        _check(5, "How risky is that code?", "dependents, complexity, tests, churn",
               f"risk {t.get('risk_level', 'unknown')}"
               + (f" (code-only {t['code_risk']})" if t.get("code_risk") is not None else ""),
               "Raises the tier" if any(b.startswith(("risk high", "complex code", "unclassified"))
                                        for b in bumps) else "No change",
               "up" if any(b.startswith(("risk high", "complex code", "unclassified"))
                           for b in bumps) else "none"),
        _check(6, "Is it a critical or protected area?", "critical paths and project notes",
               "; ".join([*(f"critical path {c}" for c in t.get("critical") or []),
                          *(f"notes mark it {m}" for m in t.get("memory_flags") or [])]) or "no",
               "Raises the tier" if any(b.startswith(("critical path", "memory flags")) for b in bumps)
               else "No change",
               "up" if any(b.startswith(("critical path", "memory flags")) for b in bumps) else "none"),
        _check(7, "Does a safety rule force the top tier?", "critical-path and production rules",
               "yes" if t.get("t4") else "no", "Forces T4" if t.get("t4") else "No change",
               "rule" if t.get("t4") else "none"),
        _check(8, "Is the weekly budget under pressure?", "usage from the status line",
               "yes" if t.get("budget_pressure") else "no",
               "Lowers the tier by 1" if t.get("budget_pressure") else "No change",
               "down" if t.get("budget_pressure") else "none"),
    ]
    chain = [f"Start at {t.get('base', '?')}", *[f"+1 {b}" for b in bumps]]
    chain.append(f"Final {final}: {tier_models.get(final, '?').replace('/', ' / ')}")
    return {"checks": checks, "chain": chain, "partial": True,
            "effort_note": f"Model and effort come with the tier: {final} runs "
                           f"{tier_models.get(final, '?').replace('/', ' at ')} effort."}
