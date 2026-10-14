"""`ccr stats`: numbers over resolved history (feedback applied, duplicates dropped).

  tasks        task records: one per routed interactive task (hook) or `ccr run` attempt
  success rate successes / (successes + failures); outcome-unknown tasks are left out
  escalation   share of a tier's tasks that were retried one tier higher (`escalated_to`)
  tokens       input + output + cache-creation tokens (cache reads excluded)
  cost         `total_cost_usd` as reported by `claude -p` (ccr run, LLM assist); interactive
               subscription sessions report no cost, so they show tokens only
"""
from __future__ import annotations

from ccr.config import TIERS
from ccr.history import savings
from ccr.history.learn import FAILURE, SUCCESS, summarize


def _bucket() -> dict:
    return {"tasks": 0, "success": 0, "failure": 0, "unknown": 0, "escalated": 0,
            "tokens": 0, "cost_usd": 0.0, "tiers": {}}


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _add(b: dict, r: dict) -> None:
    b["tasks"] += 1
    o = r.get("outcome")
    if o in SUCCESS:
        b["success"] += 1
    elif o in FAILURE:
        b["failure"] += 1
    else:
        b["unknown"] += 1
    if r.get("escalated_to"):
        b["escalated"] += 1
    b["tokens"] += int(_num(r.get("tokens")))
    b["cost_usd"] += _num(r.get("cost_usd")) + _num(r.get("assist_cost_usd"))
    t = r.get("tier") if r.get("tier") in TIERS else "?"
    b["tiers"][t] = b["tiers"].get(t, 0) + 1


def _finish(b: dict) -> dict:
    decided = b["success"] + b["failure"]
    b["success_rate"] = round(b["success"] / decided, 3) if decided else None
    b["escalation_rate"] = round(b["escalated"] / b["tasks"], 3) if b["tasks"] else None
    b["cost_usd"] = round(b["cost_usd"], 6)
    b["tiers"] = dict(sorted(b["tiers"].items()))
    return b


def compute(records: list[dict], cfg) -> dict:
    tasks = [r for r in records if isinstance(r.get("tier"), str) or r.get("class")]
    total = _bucket()
    by_tier: dict[str, dict] = {}
    by_area: dict[str, dict] = {}
    by_class: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    runs: set[str] = set()
    feedback = 0
    for r in tasks:
        _add(total, r)
        _add(by_tier.setdefault(r.get("tier") if r.get("tier") in TIERS else "?", _bucket()),
             r)
        _add(by_area.setdefault(str(r.get("area") or "*"), _bucket()), r)
        _add(by_class.setdefault(str(r.get("class") or "?"), _bucket()), r)
        src = "run" if r.get("source") == "run" else "hook" if r.get("source") == "hook" \
            else str(r.get("source") or "other")
        _add(by_source.setdefault(src, _bucket()), r)
        if isinstance(r.get("run_id"), str):
            runs.add(r["run_id"])
        if r.get("feedback"):
            feedback += 1
    ts = [r["ts"] for r in tasks if isinstance(r.get("ts"), (int, float))]
    _finish(total)
    n = total["tasks"]
    tiers = {}
    for t in [*TIERS, "?"]:
        if t in by_tier:
            b = _finish(by_tier[t])
            b["share"] = round(b["tasks"] / n, 3) if n else None
            b.pop("tiers")
            tiers[t] = b
    return {
        "tasks": n, "runs": len(runs), "feedback_corrections": feedback,
        "since": min(ts) if ts else None, "until": max(ts) if ts else None,
        "usage": {"tokens": total["tokens"], "cost_usd": total["cost_usd"],
                  "by_source": {k: {"tasks": v["tasks"], "tokens": v["tokens"],
                                    "cost_usd": round(v["cost_usd"], 6)}
                                for k, v in sorted(by_source.items())}},
        "overall": {k: total[k] for k in ("success", "failure", "unknown", "escalated",
                                          "success_rate", "escalation_rate")},
        "tiers": tiers,
        "areas": {k: _finish(v) for k, v in sorted(by_area.items())},
        "classes": {k: _finish(v) for k, v in sorted(by_class.items())},
        "learning": summarize(tasks, cfg),
        "savings": savings.estimate(tasks, cfg),
        "misroutes": savings.misroutes(tasks, cfg),
    }


def _pct(v) -> str:
    return "-" if v is None else f"{100 * v:.0f}%"


def _k(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def _table(title: str, rows: dict, key_w: int, with_tiers: bool) -> list[str]:
    head = (f"  {title:<{key_w}} {'tasks':>5} {'ok':>4} {'fail':>4} {'?':>3} {'success':>7} "
            f"{'escal':>5} {'tokens':>8} {'cost':>9}")
    out = [head + ("  tiers" if with_tiers else "")]
    for k, b in rows.items():
        line = (f"  {k[:key_w]:<{key_w}} {b['tasks']:>5} {b['success']:>4} {b['failure']:>4} "
                f"{b['unknown']:>3} {_pct(b['success_rate']):>7} {_pct(b['escalation_rate']):>5}"
                f" {_k(b['tokens']):>8} {('$%.4f' % b['cost_usd']) if b['cost_usd'] else '-':>9}")
        if with_tiers:
            line += "  " + " ".join(f"{t}:{c}" for t, c in b["tiers"].items())
        out.append(line)
    return out


def format_text(s: dict, root=None, when=None, detail: bool = False) -> list[str]:
    import time
    fmt = (lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))) if when is None \
        else when
    out = ["ccr stats" + (f" ({root})" if root else "")]
    if not s["tasks"]:
        return out + ["  no routed tasks in history yet (.ccr/history.jsonl)"]
    src = s["usage"]["by_source"]
    by_src = ", ".join(f"{k} {v['tasks']}" for k, v in src.items())
    out.append(f"Tasks:     {s['tasks']} ({by_src}; {s['runs']} ccr run(s)) from "
               f"{fmt(s['since'])} to {fmt(s['until'])}"
               + (f"; {s['feedback_corrections']} corrected by feedback"
                  if s["feedback_corrections"] else ""))
    u = s["usage"]
    out.append(f"Usage:     {_k(u['tokens'])} tokens ("
               + ", ".join(f"{k} {_k(v['tokens'])}" for k, v in src.items())
               + f"); est. cost ${u['cost_usd']:.4f} (claude-reported, headless only)")
    o = s["overall"]
    out.append(f"Outcomes:  {o['success']} ok, {o['failure']} failed, {o['unknown']} unknown; "
               f"success {_pct(o['success_rate'])}, escalated {o['escalated']} "
               f"({_pct(o['escalation_rate'])})")
    out += savings.format_lines(s["savings"], s["misroutes"], detail)
    out.append("Tier distribution: " + "  ".join(
        f"{t} {b['tasks']} ({_pct(b['share'])})" for t, b in s["tiers"].items()))
    out.append("Per tier:")
    out += _table("tier", s["tiers"], 4, False)
    out.append("Per area:")
    out += _table("area", s["areas"], max(4, min(28, max(len(k) for k in s["areas"]))), True)
    out.append("Per task class:")
    out += _table("class", s["classes"], 9, True)
    learn = [x for x in s["learning"] if x["action"] != "watch"]
    out.append("Learning (what routing does with this history):")
    if learn:
        out += [f"  {x['action']:<7} {x['note']}" for x in learn]
    else:
        out.append("  no adjustments yet (thresholds: `history:` in .ccr/config.yaml)")
    return out
