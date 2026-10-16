"""Learning loop: count/threshold rules over resolved history, per (task class x area).

Every number comes from `.ccr/config.yaml` `history:` and every adjustment cites its counts.

  up        the starting tier failed >= `fail_rate_up` of >= `min_samples` decided tasks
            -> start one tier higher (never above the escalation cap / T3 ceiling)
  down      the tier below succeeded >= `success_rate_down` of >= `min_samples`
            -> start one tier lower
  explore   the starting tier itself succeeded >= `success_rate_down` of >= `min_samples`,
            the task is low-risk and simple (risk low, scope not large, not vague, not a hard
            problem, no critical path or memory flag) and the tier below has no failure
            record -> try one tier lower (`explore_down: false` disables). If the lower tier
            then fails, the `up` rule brings it back, so the loop settles.
Downward moves never apply on a critical path and never go below the class floor (T1 for
code changes, T0 for questions and trivial edits). Counts are per class x area when that
area has decided samples, else class-wide. Outcomes `unknown`/`abandoned`/`error` are not
counted; `ccr feedback` corrections are applied before counting (history.resolve).
"""
from __future__ import annotations

from dataclasses import dataclass

from ccr.config import TIERS, TOP

SUCCESS = {"success"}
FAILURE = {"failure", "escalated"}


def tier_counts(records: list[dict], kind: str, area: str | None) -> tuple[dict, str]:
    """({tier: [successes, failures]}, scope) for class x area, else class-wide."""
    def collect(match_area: bool) -> dict:
        st: dict[str, list[int]] = {}
        for r in records:
            if r.get("class") != kind or not isinstance(r.get("tier"), str):
                continue
            if match_area and r.get("area") != area:
                continue
            o = r.get("outcome")
            s = st.setdefault(r["tier"], [0, 0])
            if o in SUCCESS:
                s[0] += 1
            elif o in FAILURE:
                s[1] += 1
        return st
    if area and area != "*":
        st = collect(True)
        if sum(sum(v) for v in st.values()):
            return st, "area"
    return collect(False), "class"


@dataclass
class Learned:
    tier: int                   # tier index after learning
    delta: int                  # -1, 0, +1
    note: str                   # one line for Decision.history / `ccr explain`
    reason: str | None = None   # line for Decision.reasons (None: no change)


STRUGGLE_SIGNALS = {"self_reported_failure", "subagent_reported_failure", "manual_model_up"}
REWORK_REPEATS = 2      # possible_rework counts only when it keeps happening for a class x area x tier


def struggled(r: dict, hc: dict) -> bool:
    """A recorded success that looks like it was hard won: many tool errors or turns, or the
    model said it could not finish. Learning treats these as failures; stats still show the
    real outcome."""
    if r.get("outcome") not in SUCCESS:
        return False
    errs, turns = int(hc.get("struggle_tool_errors") or 0), int(hc.get("struggle_turns") or 0)
    return bool((errs and (r.get("tool_errors") or 0) >= errs)
                or (turns and (r.get("turns") or 0) >= turns)
                or STRUGGLE_SIGNALS & set(r.get("signals") or ()))


def mark_struggle(records: list[dict], cfg) -> list[dict]:
    hc = cfg["history"]
    rework: dict[tuple, int] = {}
    for r in records:
        if r.get("outcome") in SUCCESS and "possible_rework" in (r.get("signals") or ()):
            k = (r.get("class"), r.get("area"), r.get("tier"))
            rework[k] = rework.get(k, 0) + 1
    out = []
    for r in records:
        again = "possible_rework" in (r.get("signals") or ()) and r.get("outcome") in SUCCESS \
            and rework.get((r.get("class"), r.get("area"), r.get("tier")), 0) >= REWORK_REPEATS
        out.append({**r, "outcome": "failure", "struggled": True} if struggled(r, hc) or again
                   else r)
    return out


def _rate(c: list[int] | None, i: int) -> float:
    return c[i] / sum(c) if c and sum(c) else 0.0


def adjust(tier: int, *, kind: str, area: str, records: list[dict], cfg, cap: int,
           floor: int, critical: bool = False, low_risk_simple: bool = False) -> Learned:
    """Apply the learning rules to a routed tier index."""
    hc = cfg["history"]
    k, up, down = int(hc["min_samples"]), float(hc["fail_rate_up"]), \
        float(hc["success_rate_down"])
    stats, scope = tier_counts(mark_struggle(records, cfg), kind, area)
    where = f"{kind} in {area}" if scope == "area" else f"{kind} (all areas)"
    if not stats:
        return Learned(tier, 0, f"no history for {kind}")
    cur = stats.get(TIERS[tier])
    lower = stats.get(TIERS[tier - 1]) if tier > 0 else None
    cur_n, low_n = sum(cur or ()), sum(lower or ())

    if cur_n >= k and _rate(cur, 1) >= up:
        counts = f"{TIERS[tier]} failed {cur[1]}/{cur_n} for {where}"
        if tier < cap:
            return Learned(tier + 1, 1, f"{counts}: +1", f"+1: history: {counts}")
        return Learned(tier, 0, f"{counts}, but escalation cap reached",
                       f"history: {counts}, but escalation cap reached")
    lower_bad = bool(lower) and low_n >= k and _rate(lower, 1) >= up
    if tier > floor and not critical:
        if low_n >= k and _rate(lower, 0) >= down:
            counts = f"{TIERS[tier - 1]} succeeded {lower[0]}/{low_n} for {where}"
            return Learned(tier - 1, -1, f"{counts}: -1", f"-1: history: {counts}")
        if hc.get("explore_down", True) and low_risk_simple and cur_n >= k and \
                _rate(cur, 0) >= down and not (lower and lower[1]):
            counts = f"{TIERS[tier]} succeeded {cur[0]}/{cur_n} for {where}"
            return Learned(tier - 1, -1,
                           f"{counts}; low-risk simple task: trying {TIERS[tier - 1]} (-1)",
                           f"-1: history: {counts}, low-risk simple task: trying "
                           f"{TIERS[tier - 1]}")
    summary = ", ".join(f"{t} {s}/{s + f} ok" for t, (s, f) in sorted(stats.items()))
    why = ""
    if lower_bad:
        why = f"; {TIERS[tier - 1]} failed {lower[1]}/{low_n}, staying"
    elif critical and (low_n >= k or cur_n >= k):
        why = "; critical path: never lowered by history"
    return Learned(tier, 0, f"{where}: {summary} (no change{why})")


def summarize(records: list[dict], cfg) -> list[dict]:
    """What the rules currently say for every class x area seen in history (for `ccr stats`).
    Each item: {class, area, tier, action: up|down|explore|watch, note}."""
    hc = cfg["history"]
    k, up, down = int(hc["min_samples"]), float(hc["fail_rate_up"]), \
        float(hc["success_rate_down"])
    groups: dict[tuple[str, str], dict[str, list[int]]] = {}
    for r in mark_struggle(records, cfg):
        kind, tier = r.get("class"), r.get("tier")
        if not isinstance(kind, str) or tier not in TIERS:
            continue
        area = r.get("area") if isinstance(r.get("area"), str) else "*"
        st = groups.setdefault((kind, area), {}).setdefault(tier, [0, 0])
        if r.get("outcome") in SUCCESS:
            st[0] += 1
        elif r.get("outcome") in FAILURE:
            st[1] += 1
    out = []
    for (kind, area), stats in sorted(groups.items()):
        where = f"{kind} in {area}" if area != "*" else f"{kind} (no area)"
        for t in sorted(stats):
            s, f = stats[t]
            n = s + f
            i = TIERS.index(t)
            if n < k:
                if n:
                    out.append({"class": kind, "area": area, "tier": t, "action": "watch",
                                "note": f"{where}: {t} {s}/{n} ok (needs {k} samples)"})
                continue
            if f / n >= up:
                nxt = TIERS[min(i + 1, TOP)]
                out.append({"class": kind, "area": area, "tier": t, "action": "up",
                            "note": f"{where}: {t} failed {f}/{n} -> start at {nxt}"})
            elif s / n >= down and i > 0:
                lo = stats.get(TIERS[i - 1])
                if lo and sum(lo) >= k and lo[1] / sum(lo) >= up:
                    note = (f"{where}: {t} ok {s}/{n}, but {TIERS[i - 1]} failed "
                            f"{lo[1]}/{sum(lo)} -> stays {t}")
                    out.append({"class": kind, "area": area, "tier": t, "action": "watch",
                                "note": note})
                else:
                    out.append({"class": kind, "area": area, "tier": t, "action": "explore",
                                "note": f"{where}: {t} ok {s}/{n} -> low-risk simple tasks "
                                        f"try {TIERS[i - 1]}"})
            else:
                out.append({"class": kind, "area": area, "tier": t, "action": "watch",
                            "note": f"{where}: {t} {s}/{n} ok (within thresholds)"})
    return out
