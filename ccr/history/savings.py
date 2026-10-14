"""Cost-savings estimate and misroute detection over resolved history (`ccr stats`).

Savings: interactive sessions report tokens, not dollars, so each task's tokens are priced at
the blended USD/Mtok of the model its tier used (`pricing:` in .ccr/config.yaml) and compared
with the same tokens priced at an all-Opus and an all-Sonnet baseline. It is an estimate: it
assumes the stronger model would have used the same tokens, which ignores rework saved or
extra thinking spent.

Misroutes, from decided outcomes (feedback applied):
  under  the task failed or was escalated at the tier it was routed to
  over   the task succeeded at a tier above the class floor while the tier below has a proven
         record (>= `history.min_samples` decided tasks, >= `history.success_rate_down`
         success) for the same class x area, so a cheaper tier would likely have done
Suggestions group the under-routed tasks by the signals that fired (no prompt text is stored),
to show which rule to tune.
"""
from __future__ import annotations

from collections import Counter

from ccr.config import TIERS
from ccr.history.learn import FAILURE, SUCCESS, tier_counts

NO_FLOOR = {"question", "trivial"}


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def price(cfg, model: str) -> float:
    """USD per Mtok for a model alias or id (matched by family name), 0 if unknown."""
    m = str(model).lower()
    for fam, usd in cfg["pricing"].items():
        if fam in m:
            return float(usd)
    return 0.0


def estimate(records: list[dict], cfg) -> dict:
    routed = opus = sonnet = 0.0
    tokens = n = 0
    for r in records:
        t, tok = r.get("tier"), _num(r.get("tokens"))
        if t not in TIERS or tok <= 0:
            continue
        n += 1
        tokens += int(tok)
        routed += tok * price(cfg, cfg.tier(t).model) / 1e6
        opus += tok * price(cfg, "opus") / 1e6
        sonnet += tok * price(cfg, "sonnet") / 1e6

    def saved(base: float):
        return round(1 - routed / base, 3) if base else None
    return {"tasks": n, "tokens": tokens, "routed_usd": round(routed, 4),
            "all_opus_usd": round(opus, 4), "all_sonnet_usd": round(sonnet, 4),
            "saved_vs_opus": saved(opus), "saved_vs_sonnet": saved(sonnet),
            "estimate": True}


def _item(r: dict, kind: str, why: str) -> dict:
    tr = r.get("trace") if isinstance(r.get("trace"), dict) else {}
    return {"id": r.get("id"), "kind": kind, "class": r.get("class"), "area": r.get("area"),
            "tier": r.get("tier"), "outcome": r.get("outcome"), "why": why,
            "rules": tr.get("rules"), "hard": tr.get("hard"), "bumps": tr.get("bumps"),
            "class_conf": tr.get("class_conf"), "has_trace": bool(tr)}


def misroutes(records: list[dict], cfg) -> dict:
    hc = cfg["history"]
    k, down = int(hc["min_samples"]), float(hc["success_rate_down"])
    tasks = [r for r in records if r.get("tier") in TIERS and r.get("class")]
    decided = [r for r in tasks if r.get("outcome") in SUCCESS | FAILURE]
    items: list[dict] = []
    over = under = 0
    for r in decided:
        i = TIERS.index(r["tier"])
        if r["outcome"] in FAILURE:
            under += 1
            items.append(_item(r, "under", f"{r['outcome']} at {r['tier']}"))
            continue
        floor = 0 if r["class"] in NO_FLOOR else 1
        if i <= floor:
            continue
        stats, _ = tier_counts(records, r["class"], r.get("area"))
        lo = stats.get(TIERS[i - 1])
        n = sum(lo) if lo else 0
        if n >= k and lo[0] / n >= down:
            over += 1
            items.append(_item(r, "over", f"{TIERS[i - 1]} succeeded {lo[0]}/{n} for "
                                          f"{r['class']}"))
    n = len(decided)
    return {"decided": n, "over": over, "under": under,
            "over_rate": round(over / n, 3) if n else None,
            "under_rate": round(under / n, 3) if n else None,
            "items": items, "suggestions": suggestions(items)}


def suggestions(items: list[dict]) -> list[str]:
    """Which rule to tune, from the under-routed tasks (grouped by class and tier)."""
    groups: dict[tuple, list[dict]] = {}
    for it in items:
        if it["kind"] == "under":
            groups.setdefault((it["class"], it["tier"]), []).append(it)
    out = []
    for (kind, tier), its in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(its)
        traced = [i for i in its if i["has_trace"]]
        bare = [i for i in traced if not i["rules"]]
        calm = [i for i in traced if not i["hard"] and not i["bumps"]]
        if bare:
            out.append(f"{len(bare)}/{n} {kind} task(s) failed at {tier} with no cue word "
                       f"matched: the class was a guess; raise class_base.{kind} or add "
                       f"cues to the rules")
        if kind in ("bugfix", "perf") and calm:
            out.append(f"{len(calm)}/{n} {kind} task(s) failed at {tier} with no hard or "
                       f"escalation signal: add project words that mean 'hard' to "
                       f"`hard_terms:` in .ccr/config.yaml")
        if not traced:
            out.append(f"{n} {kind} task(s) failed at {tier}; older records have no routing "
                       f"trace to tune from")
    return out


def format_lines(est: dict, mis: dict, detail: bool = False) -> list[str]:
    def pct(v):
        return "-" if v is None else f"{100 * v:.0f}%"
    out = []
    if est["tasks"]:
        out.append(f"Savings:   est. ${est['routed_usd']:.2f} routed vs ${est['all_opus_usd']:.2f}"
                   f" all-Opus ({pct(est['saved_vs_opus'])} saved) and "
                   f"${est['all_sonnet_usd']:.2f} all-Sonnet ({pct(est['saved_vs_sonnet'])}) "
                   f"over {est['tasks']} tasks; estimate from tokens x `pricing:`")
    if mis["decided"]:
        out.append(f"Misroutes: over-routed {mis['over']}/{mis['decided']} "
                   f"({pct(mis['over_rate'])}, a cheaper tier had a proven record), "
                   f"under-routed {mis['under']}/{mis['decided']} ({pct(mis['under_rate'])}, "
                   f"failed or escalated)")
        out += [f"  tune: {s}" for s in mis["suggestions"]]
        if detail:
            for it in mis["items"]:
                tid = str(it["id"] or "?")[:8]
                out.append(f"  [{tid}] {it['kind']:<5} {it['tier']} {it['class']} in "
                           f"{it['area'] or '*'}: {it['why']}; rules={it['rules'] or '-'} "
                           f"hard={it['hard']} bumps={it['bumps'] or '-'}")
    return out
