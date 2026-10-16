"""Routing: task class + located code + risk + history + budget -> tier T0..T4, with reasons.

Policy (all thresholds in .ccr/config.yaml):
  * start from the class base tier (question/trivial T0 ... migration/security/debugging T3);
  * adjustments: large scope, vague bug report, high risk, critical path, memory flags push up;
    small scope pushes down. Ups and downs each count once, and escalation above the base is
    capped at `routing.max_escalation` (2) tiers (the second only when 3+ signals agree);
  * history: repeated failures at the chosen tier push up (within the cap); consistent
    success one tier lower pushes down;
  * without a T4 rule the tier never exceeds `routing.ceiling_without_critical` (T3);
  * T4 rules (critical path) may jump straight to T4;
  * budget pressure moves non-critical tasks one tier down; critical / T4 tasks never move.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from ccr.config import TIERS, TOP, Config, Tier, load_config
from ccr.evaluate.classify import TaskClass, classify
from ccr.evaluate.locate import Candidate, Location, locate
from ccr.evaluate.risk import Risk, assess_risk
from ccr.route.budget import Budget, budget_pressure

CHANGE = {"trivial", "tests", "bugfix", "feature", "refactor", "perf", "migration", "security",
          "debugging"}
SUBSTANTIVE = (CHANGE - {"trivial"}) | {"design"}   # design: plans and architecture, no edit
DEEP_COMPLEXITY = 0.6    # complexity component at which code counts as hard to change
UNSURE_CLASS = 0.4       # class confidence below this, with no cue words, is a guess
DEEP_SUPPORT = 0.4       # blast radius / missing-test level that makes complex code risky
_AUTO = object()


@dataclass
class Decision:
    task_class: TaskClass
    location: Location
    risk: Risk
    budget: Budget
    tier: Tier
    base: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    t4_rules: list[str] = field(default_factory=list)
    history: str = "no history"
    llm: str = "not requested"
    llm_used: bool = False
    area: str = "*"
    notes: list[str] = field(default_factory=list)
    bumps: list[str] = field(default_factory=list)   # names of the +1 signals that fired
    elapsed_ms: float = 0.0
    reclassed: bool = False     # an unanchored bug report was routed as debugging

    def record(self) -> dict:
        """History fields for this decision (no prompt text)."""
        return {"class": self.task_class.kind, "area": self.area, "tier": self.tier.name,
                "model": self.tier.model, "effort": self.tier.effort,
                "risk": self.risk.score, "confidence": self.confidence,
                "source": "llm" if self.llm_used else "rules", "trace": self.trace()}

    def trace(self) -> dict:
        """Compact signals behind this decision, for tuning the rules from real data.
        Numbers, flags and rule names only: no prompt text, no file paths."""
        tc, r = self.task_class, self.risk
        return {"base": self.base, "scope": tc.scope, "vagueness": tc.vagueness,
                "steps": tc.steps, "class_conf": tc.confidence, "secondary": tc.secondary,
                "class_source": tc.source, "destructive": bool(tc.destructive),
                "hard": bool(tc.hard), "rules": tc.evidence_terms(), "hard_terms": tc.hard[:3],
                "bumps": self.bumps, "located": self.location.located,
                "loc_conf": self.location.confidence, "risk_level": r.level,
                "code_risk": r.code_risk, "critical": r.critical_groups,
                "memory_flags": sorted({f.kind for f in r.memory_flags}),
                "t4": bool(self.t4_rules), "budget_pressure": bool(self.budget.pressure),
                "elapsed_ms": round(self.elapsed_ms, 1)}

    def to_dict(self) -> dict:
        tc, loc, r, b = self.task_class, self.location, self.risk, self.budget
        return {
            "tier": self.tier.name, "model": self.tier.model, "effort": self.tier.effort,
            "agent": self.tier.agent, "base": self.base, "confidence": self.confidence,
            "class": {"kind": tc.kind, "confidence": tc.confidence, "source": tc.source,
                      "secondary": tc.secondary, "scope": tc.scope,
                      "steps": tc.steps, "vagueness": tc.vagueness, "evidence": tc.matched.get(tc.kind, []),
                      "destructive": tc.destructive, "hard": tc.hard},
            "location": {"confidence": loc.confidence, "explicit": loc.explicit,
                         "mentioned": loc.mentioned, "unresolved": loc.unresolved,
                         "symbols": loc.symbols, "terms": loc.terms,
                         "memory_terms": loc.memory_terms, "graph": loc.graph,
                         "candidates": [{"path": c.path, "confidence": c.confidence,
                                         "reasons": c.reasons} for c in loc.candidates[:15]]},
            "risk": {"score": r.score, "level": r.level, "code_risk": r.code_risk,
                     "components": {n: {"value": c.value, "detail": c.detail}
                                    for n, c in r.components.items()},
                     "critical_groups": r.critical_groups, "critical_files": r.critical_files,
                     "critical_terms": r.critical_terms,
                     "memory_flags": [{"kind": f.kind, "term": f.term, "path": f.path}
                                      for f in r.memory_flags]},
            "budget": {"used_pct": b.used_pct, "source": b.source, "pressure": b.pressure,
                       "soft_limit_pct": b.soft_limit_pct, "detail": b.detail},
            "history": self.history, "llm": self.llm, "t4_rules": self.t4_rules,
            "area": self.area, "reasons": self.reasons, "notes": self.notes,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def overall_confidence(tc: TaskClass, loc: Location) -> float:
    loc_factor = 0.85 + 0.15 * loc.confidence if tc.kind == "question" else 0.6 + 0.4 * loc.confidence
    return round(tc.confidence * loc_factor * (1 - 0.35 * tc.vagueness), 3)


def decide(task: str, root: str | Path = ".", *, cfg: Config | None = None,
           allow_llm: bool = False, llm_runner=None, graph=_AUTO, memory=_AUTO,
           budget: Budget | None = None, history_records: list[dict] | None = None,
           now: float | None = None, transcript_path: str | None = None,
           budget_fallback=None, nested_memory: bool = True) -> Decision:
    """Evaluate and route a task. Token-free unless `allow_llm` and rule confidence is low."""
    t0 = time.perf_counter()
    root = Path(root).resolve()
    cfg = cfg or load_config(root)
    now = time.time() if now is None else now
    notes = [f"config: {e}" for e in cfg.errors]
    own_graph = None
    if graph is _AUTO:
        from ccr.graph import Graph
        own_graph = graph = Graph.open(root)
    if memory is _AUTO:
        from ccr.memory import collect
        memory = collect(root, nested=nested_memory)
    try:
        loc = locate(task, root, graph, memory)
        tc = classify(task, located=loc.located, hard_terms=cfg["hard_terms"])
        risk = assess_risk(loc, task, cfg, graph, memory)
        conf = overall_confidence(tc, loc)
        llm_status, llm_used = "not requested", False
        threshold = float(cfg["llm"]["threshold"])
        if allow_llm and conf < threshold:
            from ccr.evaluate import llm as L
            guess = f"{tc.kind} (confidence {tc.confidence:.2f}), scope {tc.scope}"
            hint, status = L.assist(task, L.graph_summary(root, graph, loc), guess,
                                    model=cfg["llm"]["model"], effort=cfg["llm"]["effort"],
                                    timeout=float(cfg["llm"]["timeout_s"]), runner=llm_runner)
            llm_status = status
            if hint is not None:
                llm_used = True
                changes = _apply_hint(hint, tc, loc, root, graph)
                risk = assess_risk(loc, task, cfg, graph, memory)
                if hint.confidence is not None:
                    conf = round(max(conf, 0.9 * hint.confidence), 3)
                llm_status += ": " + (", ".join(changes) or "agreed with rules") + (
                    f" ({hint.reason})" if hint.reason else "") + (
                    f" [~${hint.cost_usd:.4f}]" if hint.cost_usd is not None else "")
        elif allow_llm:
            llm_status = f"not needed (confidence {conf:.2f} >= {threshold:.2f})"
        elif conf < threshold:
            llm_status = (f"off (confidence {conf:.2f} < {threshold:.2f}; "
                          f"`ccr explain --llm` would ask haiku)")
        if budget is None:
            budget = budget_pressure(root, cfg, now=now, fallback=budget_fallback,
                                     transcript_path=transcript_path)
        if history_records is None:
            from ccr import history as H
            history_records = H.read(root, window_days=cfg["history"]["window_days"], now=now)
        d = _route(tc, loc, risk, budget, history_records, cfg, conf)
        d.llm, d.llm_used = llm_status, llm_used
        d.notes = notes + loc.notes + risk.notes
        d.elapsed_ms = (time.perf_counter() - t0) * 1000
        return d
    finally:
        if own_graph is not None:
            own_graph.close()


def _apply_hint(hint, tc: TaskClass, loc: Location, root: Path, graph) -> list[str]:
    changes = []
    if hint.kind and hint.kind != tc.kind:
        changes.append(f"class {tc.kind} -> {hint.kind}")
        tc.kind = hint.kind
    tc.source = "llm"
    if hint.scope:
        new_scope = {"small": "small", "medium": "unspecified", "large": "large"}[hint.scope]
        if new_scope != tc.scope:
            changes.append(f"scope {tc.scope} -> {hint.scope}")
            tc.scope = new_scope
    if hint.vague is not None:
        nv = max(tc.vagueness, 0.6) if hint.vague else min(tc.vagueness, 0.3)
        if (nv >= 0.6) != tc.vague:
            changes.append("vague" if hint.vague else "not vague")
        tc.vagueness = nv
    known = {c.path: c for c in loc.candidates}
    added = []
    for f in hint.files:
        exists = (graph is not None and graph.file_row(f) is not None) or (root / f).is_file()
        if not exists:
            continue
        if f in known:
            c = known[f]
            c.confidence = round(1 - (1 - c.confidence) * 0.4, 3)
            c.reasons.append("suggested by LLM")
        else:
            indexed = graph is not None and graph.file_row(f) is not None
            loc.candidates.append(Candidate(f, 0.6, ["suggested by LLM"], indexed))
            added.append(f)
    if added:
        changes.append("files +" + ", ".join(added[:3]))
    loc.candidates.sort(key=lambda c: (-c.confidence, len(c.path), c.path))
    if loc.candidates and loc.confidence < loc.candidates[0].confidence:
        loc.confidence = loc.candidates[0].confidence
    return changes


def _deep_code(risk: Risk) -> str | None:
    """Why the located code is hard to change safely, or None. Needs complex code plus
    either many dependents or weak test coverage."""
    c = risk.components
    cx, br, tp = (c[n].value if n in c else 0.0
                  for n in ("complexity", "blast_radius", "test_proximity"))
    if cx < DEEP_COMPLEXITY or (br < DEEP_SUPPORT and tp < DEEP_SUPPORT):
        return None
    why = [f"complexity {cx:.2f}"]
    if br >= DEEP_SUPPORT:
        why.append(f"blast radius {br:.2f}")
    if tp >= DEEP_SUPPORT:
        why.append("weak tests")
    return ", ".join(why)


def _impactful(risk: Risk) -> str | None:
    """Why the located code is costly to get wrong (complex or widely depended on)."""
    c = risk.components
    cx, br = (c[n].value if n in c else 0.0 for n in ("complexity", "blast_radius"))
    if cx >= DEEP_COMPLEXITY:
        return f"complexity {cx:.2f}"
    if br >= DEEP_SUPPORT:
        return f"blast radius {br:.2f}"
    return None


_NO_DOWN = {"migration", "security", "debugging"}
_CRITICAL_UP = ("critical path", "memory flags")
_WORDING_UP = ("hard problem", "vague bug", "large scope")
_SWEEPING_UP = "sweeping change"


def _scope_label(tc: TaskClass) -> str:
    return ", ".join(tc.scope_terms[:3]) or "LLM"


# Each signal rule inspects the task and returns a reason string when it applies. New rules
# are added to _UP_RULES / _DOWN_RULES; _signals and _route do not change. Order is the order
# reasons are reported in.

def _up_large_scope(tc, loc, risk):
    if tc.scope == "large":
        return f"large scope ({_scope_label(tc)})"


VAGUE_BUG_WORDS = 8          # a short bug report that names no file or function


def _up_vague_bug(tc, loc, risk):
    bare = len(tc.scope_terms) == 0 and not (loc.explicit or loc.symbols or tc.steps) \
        and tc.words <= VAGUE_BUG_WORDS
    if tc.kind == "bugfix" and (tc.vague or bare):
        why = (f"vagueness {tc.vagueness:.2f}" if tc.vague else "names no file or function")
        return f"vague bug report ({why}): needs exploration"


def _up_sweeping(tc, loc, risk):
    if tc.sweeping and tc.kind in ("refactor", "migration", "feature", "perf"):
        return f"sweeping change ({', '.join(tc.sweeping[:2])}): needs a plan"


def _up_hard_problem(tc, loc, risk):
    if tc.hard and tc.kind in ("perf", "bugfix"):
        return f"hard problem ({', '.join(tc.hard[:2])})"


def _up_code_difficulty(tc, loc, risk):
    kind = tc.kind
    # the codebase decides how hard a bug is, whatever the wording: complex code that is
    # widely depended on or untested needs a stronger model to diagnose safely
    deep = _deep_code(risk) if kind in ("bugfix", "debugging", "perf") else None
    if deep:
        return f"complex code ({deep})"
    if risk.level == "high" and kind in SUBSTANTIVE:
        return f"risk high ({risk.score:.2f})"
    if kind in SUBSTANTIVE and not tc.evidence_terms() and tc.confidence < UNSURE_CLASS \
            and not tc.steps and loc.located and _impactful(risk):
        # a terse prompt with no cue words says nothing about difficulty: let the code decide
        return f"unclassified task on impactful code ({_impactful(risk)})"


def _up_critical_path(tc, loc, risk):
    kind = tc.kind
    if kind in SUBSTANTIVE and risk.critical or kind == "trivial" and risk.critical_located:
        return f"critical path ({'/'.join(risk.critical_groups)})"


def _up_memory_flags(tc, loc, risk):
    if tc.kind in CHANGE and risk.has_memory("critical", "protected", "fragile"):
        kinds = sorted({f.kind for f in risk.memory_flags
                        if f.kind in ("critical", "protected", "fragile")})
        return f"memory flags {'/'.join(kinds)} on {risk.memory_flags[0].path}"


def _down_small_scope(tc, loc, risk):
    if tc.scope == "small" and tc.kind in SUBSTANTIVE - _NO_DOWN:
        return f"small scope ({_scope_label(tc)})"


def _down_explicit_steps(tc, loc, risk):
    if tc.steps and tc.kind in SUBSTANTIVE - _NO_DOWN and not tc.destructive:
        return f"explicit step-by-step instructions ({tc.steps} steps)"


_UP_RULES = (_up_large_scope, _up_sweeping, _up_vague_bug, _up_hard_problem, _up_code_difficulty,
             _up_critical_path, _up_memory_flags)
_DOWN_RULES = (_down_small_scope, _down_explicit_steps)


def _signals(tc: TaskClass, loc: Location, risk: Risk) -> tuple[list[str], list[str]]:
    """The +1 signals (`ups`) and -1 signals (`downs`) that apply to this task."""
    ups = [r for rule in _UP_RULES if (r := rule(tc, loc, risk))]
    downs = [r for rule in _DOWN_RULES if (r := rule(tc, loc, risk))]
    return ups, downs


_HALF = TIERS.index("T2H")


def _climb(tier: int, step: int) -> int:
    """`tier` raised by `step` signals. T2H is a half step between T2 and T3: a single +1 from
    T2 lands on it, but a +2 jumps over it so strong signals still reach the same tier as before."""
    pos = tier + step
    if step > 1 and tier < _HALF <= pos:
        pos += 1
    return pos


def _escalate(tier: int, ups: list[str], max_esc: int, reasons: list[str]) -> int:
    if not ups:
        return tier
    if max_esc <= 0:
        reasons.append("escalation disabled (routing.max_escalation 0): " + "; ".join(ups))
        return tier
    # one tier normally; two when three or more independent signals agree, or when a
    # critical path meets a difficulty signal from the wording (a hard bug in auth code
    # is not a T2 job). Risk and critical path both come from the code, so they don't
    # count as two opinions.
    crit = any(u.startswith(_CRITICAL_UP) for u in ups)
    wording = any(u.startswith(_WORDING_UP) for u in ups)
    sweeping = any(u.startswith(_SWEEPING_UP) for u in ups)
    step = 2 if (len(ups) >= 3 or crit and wording or sweeping) and max_esc >= 2 else 1
    reasons.append(f"+{step}: " + "; ".join(ups)
                   + (" (escalation capped at one tier)" if len(ups) > 1 and step == 1 else ""))
    new = _climb(tier, step)
    return new + 1 if crit and new == _HALF else new      # a critical path never stops at T2H


def _descend(tier: int, downs: list[str], kind: str, critical_up: bool, reasons: list[str]) -> int:
    if not downs:
        return tier
    text = "; ".join(downs)
    if critical_up:
        reasons.append(f"{text}: ignored on a critical path")
        return tier
    floor = 1 if kind in SUBSTANTIVE else 0
    if tier - 1 >= floor:
        reasons.append("-1: " + text)
        return tier - 1
    reasons.append(f"{text}: already at the {TIERS[floor]} floor for {kind}")
    return tier


def _risk_note(kind: str, risk: Risk, ups: list[str], downs: list[str], reasons: list[str]) -> None:
    if not ups and not downs:
        if risk.level == "unknown":
            reasons.append("risk unknown (no files located): no risk adjustment")
        elif kind not in SUBSTANTIVE:
            what = "read-only questions" if kind == "question" else "trivial edits"
            reasons.append(f"risk {risk.level} ({risk.score:.2f}) does not move {what}")
        else:
            reasons.append(f"risk {risk.level} ({risk.score:.2f}): no adjustment")
    elif risk.level in ("low", "medium") and not any(u.startswith("risk") for u in ups):
        reasons.append(f"risk {risk.level} ({risk.score:.2f})")


# T4 rules: each returns a reason when the task must run at T4. Add a rule to _T4_RULES to
# extend the policy. A rule sees the reasons found so far (the production-migration rule
# only applies when no other rule fired).

def _t4_security(tc, risk, cfg, found):
    if tc.kind == "security" and risk.critical:
        return f"security change on a critical path ({'/'.join(risk.critical_groups)})"


def _t4_destructive_migration(tc, risk, cfg, found):
    if tc.kind == "migration" and risk.critical and tc.destructive:
        return (f"destructive migration ({', '.join(tc.destructive[:2])}) on "
                f"{'/'.join(risk.critical_groups)}")


def _t4_memory_marked(tc, risk, cfg, found):
    if tc.kind in SUBSTANTIVE - {"tests"} and risk.critical_located and \
            risk.has_memory("critical", "protected"):
        flag = next(f for f in risk.memory_flags if f.kind in ("critical", "protected"))
        return f"memory marks {flag.path} {flag.kind} ('{flag.term}')"


def _t4_risky_critical_code(tc, risk, cfg, found):
    if tc.kind in SUBSTANTIVE - {"tests"} and risk.critical_located and tc.scope != "small" and \
            risk.code_risk >= cfg["risk"]["t4_code_risk"]:
        return (f"critical path {'/'.join(risk.critical_groups)} with code risk "
                f"{risk.code_risk:.2f}")


_WIPE = ("wipe", "purge", "truncat", "delete all", "delete every", "remove all", "remove every",
         "erase", "clear all", "empty all", "drop")


def _t4_production_migration(tc, risk, cfg, found):
    """Destroying data in production, whatever the task was classed as."""
    prod = [t for t in tc.destructive if "prod" in t or "live" in t]
    wipe = [t for t in tc.destructive if t.startswith(_WIPE)]
    if tc.kind in SUBSTANTIVE - {"tests", "design"} and prod and (wipe or tc.kind == "migration") \
            and not found:
        return f"destructive change against production ({', '.join(tc.destructive[:2])})"


_T4_RULES = (_t4_security, _t4_destructive_migration, _t4_memory_marked,
             _t4_risky_critical_code, _t4_production_migration)


def _t4_rules(tc: TaskClass, risk: Risk, cfg: Config) -> list[str]:
    """Critical-path rules that jump straight to T4."""
    found: list[str] = []
    for rule in _T4_RULES:
        reason = rule(tc, risk, cfg, found)
        if reason:
            found.append(reason)
    return found


def _under_budget(tier: int, t4: list[str], risk: Risk, budget: Budget, reasons: list[str]) -> int:
    if not budget.pressure:
        return tier
    if t4 or tier >= TOP:
        reasons.append(f"budget pressure ({budget.used_pct:.0f}%): T4 is never downgraded")
    elif risk.critical:
        reasons.append(f"budget pressure ({budget.used_pct:.0f}%): critical path, no downgrade")
    elif tier > 0:
        reasons.append(f"-1: budget pressure (weekly {budget.used_pct:.0f}% >= "
                       f"{budget.soft_limit_pct:.0f}%)")
        return tier - 1
    else:
        reasons.append("budget pressure: already at T0")
    return tier


def _unanchored_bug(tc: TaskClass, loc: Location) -> TaskClass:
    """A bug report that names no code we can find says nothing about where or why: treat it
    as a debugging task (exploration first) instead of a routine fix."""
    if tc.kind == "bugfix" and tc.source == "rules" and not loc.located and not tc.steps:
        both = tc.matched.get("bugfix", []) + tc.matched.get("debugging", [])
        return replace(tc, kind="debugging", matched={**tc.matched, "debugging": both})
    return tc


def _route(tc: TaskClass, loc: Location, risk: Risk, budget: Budget, records: list[dict],
           cfg: Config, conf: float) -> Decision:
    reclassed = _unanchored_bug(tc, loc)
    note = ["bug report names no code ccr can find: treated as debugging (explore first)"] \
        if reclassed is not tc else []
    tc = reclassed
    kind = tc.kind
    base_name = cfg["class_base"][kind]
    base = TIERS.index(base_name)
    max_esc = int(cfg["routing"]["max_escalation"])
    ceiling = TIERS.index(cfg["routing"]["ceiling_without_critical"])
    reasons = [f"base {base_name} for {kind}"
               + (f" (matched {tc.evidence()})" if tc.evidence() else "")
               + (" [class from LLM]" if tc.source == "llm" else "")] + note

    ups, downs = _signals(tc, loc, risk)
    critical_up = any(u.startswith(_CRITICAL_UP) for u in ups)
    tier = _escalate(base, ups, max_esc, reasons)
    tier = _descend(tier, downs, kind, critical_up, reasons)
    _risk_note(kind, risk, ups, downs, reasons)

    # history (learning loop, ccr/history/learn.py)
    from ccr.history import learn
    area = loc.top_area()
    on_critical = critical_up or bool(risk.critical)
    low_risk_simple = (not on_critical and tc.scope != "large" and not tc.vague
                       and not tc.hard and not risk.memory_flags
                       and (risk.level == "low"
                            or risk.level == "unknown" and kind not in SUBSTANTIVE))
    learned = learn.adjust(tier, kind=kind, area=area, records=records, cfg=cfg,
                           cap=min(_climb(base, max_esc), ceiling),
                           floor=1 if kind in SUBSTANTIVE else 0, critical=on_critical,
                           low_risk_simple=low_risk_simple)
    tier = learned.tier
    if learned.reason:
        reasons.append(learned.reason)

    if tier > ceiling:
        reasons.append(f"capped at {TIERS[ceiling]} (T4 needs a critical-path rule)")
        tier = ceiling

    t4 = _t4_rules(tc, risk, cfg)
    if t4:
        if tier < TOP:
            reasons.append(f"T4 rule: {'; '.join(t4)} -> T4")
        tier = TOP

    tier = _under_budget(tier, t4, risk, budget, reasons)

    if conf < float(cfg["llm"]["threshold"]) and tier < TOP:
        reasons.append(f"low confidence ({conf:.2f}): starting low; escalate one tier "
                       f"if the result falls short")
    t = cfg.tier(tier)
    reasons.append(f"final {t.name}: {t.model}/{t.effort} via {t.agent}")
    return Decision(task_class=tc, location=loc, risk=risk, budget=budget, tier=t,
                    base=base_name, confidence=conf, reasons=reasons, t4_rules=t4,
                    history=learned.note, area=area,
                    bumps=[u.split(" (")[0] for u in ups], reclassed=bool(note))
