"""Plain-text rendering of a routing Decision (`ccr explain`)."""
from __future__ import annotations


def format_decision(task: str, d) -> list[str]:
    from ccr.evaluate.risk import COMPONENTS, LABELS
    tc, loc, r, b = d.task_class, d.location, d.risk, d.budget
    short = " ".join(task.split())
    out = [f"Task:      {short if len(short) <= 100 else short[:97] + '...'}",
           f"Tier:      {d.tier.name}  {d.tier.model} / {d.tier.effort}  (agent {d.tier.agent})"
           + ("   [T4 rule]" if d.t4_rules else ""),
           f"Class:     {tc.kind} ({tc.source}, class confidence {tc.confidence:.2f}"
           + (f", runner-up {tc.secondary}" if tc.secondary else "") + f")  scope {tc.scope}"
           + f", vagueness {tc.vagueness:.2f}"
           + (f", destructive: {', '.join(tc.destructive[:3])}" if tc.destructive else ""),
           f"Confidence: {d.confidence:.2f} overall"]
    named = len(loc.explicit)
    out.append(f"Files:     {len(loc.candidates)} candidate(s), location confidence "
               f"{loc.confidence:.2f}; {named} named/@-mentioned, {len(loc.symbols)} symbol(s)"
               + ("" if loc.graph else "  [no code graph]"))
    for c in loc.candidates[:6]:
        out.append(f"           {c.confidence:.2f}  {c.path}  ({'; '.join(c.reasons[:3])})")
    if len(loc.candidates) > 6:
        out.append(f"           ... {len(loc.candidates) - 6} more")
    if r.score is None:
        out.append("Risk:      unknown (nothing located)")
    else:
        out.append(f"Risk:      {r.score:.2f} {r.level}  (code-only {r.code_risk:.2f})")
    for n in COMPONENTS:
        c = r.components[n]
        out.append(f"           {LABELS[n]:<15} {c.value:.2f}  {c.detail}")
    out.append(f"Budget:    {b.detail}" + ("  -> pressure" if b.pressure else ""))
    out.append(f"Learning:  {d.history}")
    out.append(f"LLM:       {d.llm}")
    out.append("Reasoning:")
    out += [f"  - {x}" for x in d.reasons]
    for n in d.notes:
        out.append(f"Note:      {n}")
    out.append(f"(evaluated in {d.elapsed_ms:.0f} ms; explain runs nothing)")
    return out
