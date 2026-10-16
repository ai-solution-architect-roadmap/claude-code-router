"""Offline calibration from real outcomes (reports only: it never changes routing).

  export_benchmark  turn decided tasks that have a stored prompt into a labelled replay set
                    for `scripts/bench_routing.py --prompts FILE`
  fit               logistic regression (stdlib) of P(task fails) on the routing signals kept
                    in each record's `trace` plus the tier it got, then a shadow comparison:
                    which tier would the model have picked, against the rule router

Benchmark labels, from one decided task at tier T:
  failure   need = the tier above T, cap = top         (T was not enough)
  success   need = lowest tier that has succeeded for this class x area, cap = the tier above T
A success proves T was sufficient, not necessary, so these labels catch under-routing well and
over-routing only where a cheaper tier has its own record.
"""
from __future__ import annotations

import math
import random

from ccr.config import TIERS, TOP
from ccr.history.learn import FAILURE, SUCCESS, tier_counts

MIN_TASKS = 30
MIN_FAILURES = 5
FOLDS = 5
RISK_LEVEL = {"low": 0.0, "medium": 0.5, "high": 1.0}
FEATURES = ("tier", "base", "scope_large", "scope_small", "vagueness", "steps", "class_conf",
            "hard", "destructive", "located", "loc_conf", "code_risk", "risk_level",
            "critical", "memory_flags", "bumps", "n_rules", "t4")


def export_benchmark(records: list[dict], logged: dict[str, dict]) -> list[list]:
    decided = [r for r in records if r.get("tier") in TIERS and r.get("class")
               and r.get("outcome") in SUCCESS | FAILURE]
    out = []
    for r in decided:
        p = (logged.get(str(r.get("id"))) or {}).get("prompt")
        if not p:
            continue
        i = TIERS.index(r["tier"])
        if r["outcome"] in FAILURE:
            need, cap = min(i + 1, TOP), TOP
        else:
            st, _ = tier_counts(decided, r["class"], r.get("area"))
            ok = [TIERS.index(t) for t, (s, _f) in st.items() if s and t in TIERS]
            need, cap = min([i, *ok]), min(i + 1, TOP)
        out.append([p, TIERS[need], TIERS[cap]])
    return out


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def features(r: dict, tier: str | None = None) -> list[float]:
    t = r.get("trace") if isinstance(r.get("trace"), dict) else {}
    base = t.get("base") if t.get("base") in TIERS else r.get("tier")
    return [TIERS.index(tier or r["tier"]) / TOP, TIERS.index(base) / TOP if base in TIERS else 0.0,
            float(t.get("scope") == "large"), float(t.get("scope") == "small"),
            _num(t.get("vagueness")), _num(t.get("steps")), _num(t.get("class_conf")),
            float(bool(t.get("hard"))), float(bool(t.get("destructive"))),
            float(bool(t.get("located"))), _num(t.get("loc_conf")), _num(t.get("code_risk")),
            RISK_LEVEL.get(t.get("risk_level"), 0.0), float(bool(t.get("critical"))),
            float(len(t.get("memory_flags") or [])), float(len(t.get("bumps") or [])),
            float(len(t.get("rules") or [])), float(bool(t.get("t4")))]


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def _train(X: list[list[float]], y: list[int], l2: float = 0.05, steps: int = 600,
           lr: float = 0.3) -> list[float]:
    """Batch gradient descent; w[0] is the bias. Features are used as given (already 0-1ish)."""
    n, m = len(X), len(X[0])
    w = [0.0] * (m + 1)
    for _ in range(steps):
        g = [0.0] * (m + 1)
        for xi, yi in zip(X, y, strict=True):
            e = _sigmoid(w[0] + sum(a * b for a, b in zip(w[1:], xi, strict=True))) - yi
            g[0] += e
            for j in range(m):
                g[j + 1] += e * xi[j]
        for j in range(m + 1):
            w[j] -= lr * (g[j] / n + (l2 * w[j] if j else 0.0))
    return w


def _predict(w: list[float], x: list[float]) -> float:
    return _sigmoid(w[0] + sum(a * b for a, b in zip(w[1:], x, strict=True)))


def _auc(scores: list[float], y: list[int]) -> float | None:
    pos = [s for s, v in zip(scores, y, strict=True) if v]
    neg = [s for s, v in zip(scores, y, strict=True) if not v]
    if not pos or not neg:
        return None
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def fit(records: list[dict], max_fail_prob: float = 0.2, seed: int = 7) -> dict:
    rows = [r for r in records if r.get("tier") in TIERS and isinstance(r.get("trace"), dict)
            and r.get("outcome") in SUCCESS | FAILURE]
    y = [int(r["outcome"] in FAILURE) for r in rows]
    n, fails = len(rows), sum(y)
    res = {"tasks": n, "failures": fails, "ready": False, "min_tasks": MIN_TASKS,
           "min_failures": MIN_FAILURES}
    if n < MIN_TASKS or fails < MIN_FAILURES or n - fails < MIN_FAILURES:
        res["note"] = (f"need at least {MIN_TASKS} decided tasks with a routing trace and "
                       f"{MIN_FAILURES} each of successes and failures (have {n}, {fails} failed); "
                       f"keep using ccr feedback good|bad and try again")
        return res
    X = [features(r) for r in rows]
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    cv = [0.0] * n
    for k in range(FOLDS):
        test = set(idx[k::FOLDS])
        tr = [i for i in idx if i not in test]
        if not any(y[i] for i in tr) or all(y[i] for i in tr):
            continue
        w = _train([X[i] for i in tr], [y[i] for i in tr])
        for i in test:
            cv[i] = _predict(w, X[i])
    w = _train(X, y)
    base = fails / n
    brier = sum((c - v) ** 2 for c, v in zip(cv, y, strict=True)) / n
    brier0 = sum((base - v) ** 2 for v in y) / n
    shadow = {"same": 0, "cheaper": 0, "stronger": 0}
    for r in rows:
        picked = next((t for t in TIERS if _predict(w, features(r, t)) <= max_fail_prob),
                      TIERS[TOP])
        d = TIERS.index(picked) - TIERS.index(r["tier"])
        shadow["same" if d == 0 else "cheaper" if d < 0 else "stronger"] += 1
    res.update(ready=True, fail_rate=round(base, 3), cv_auc=_auc(cv, y), cv_brier=round(brier, 4),
               baseline_brier=round(brier0, 4), max_fail_prob=max_fail_prob, shadow=shadow,
               weights=sorted(({"feature": f, "weight": round(wt, 3)}
                               for f, wt in zip(FEATURES, w[1:], strict=True)),
                              key=lambda d: -abs(d["weight"])))
    res["cv_auc"] = None if res["cv_auc"] is None else round(res["cv_auc"], 3)
    res["useful"] = bool(res["cv_auc"] and res["cv_auc"] >= 0.65 and brier < brier0)
    return res


def format_text(res: dict) -> list[str]:
    out = [f"ccr calibrate: {res['tasks']} decided task(s) with a trace, {res['failures']} failed"]
    if not res["ready"]:
        return out + [f"  {res['note']}"]
    out.append(f"  cross-validated AUC {res['cv_auc']} (0.5 is chance); Brier {res['cv_brier']} vs "
               f"{res['baseline_brier']} for always guessing the base rate {res['fail_rate']:.0%}")
    out.append("  verdict: " + ("the signals predict failures better than chance; shadow picks below "
                                "are worth reviewing" if res["useful"] else
                                "no better than guessing yet; do not change rules from this"))
    s = res["shadow"]
    out.append(f"  shadow router (tier with predicted failure <= {res['max_fail_prob']:.0%}): "
               f"same tier {s['same']}, cheaper {s['cheaper']}, stronger {s['stronger']}")
    out.append("  strongest signals (+ means more failures):")
    out += [f"    {w['weight']:+.2f}  {w['feature']}" for w in res["weights"][:8]]
    out.append("  report only: routing is unchanged")
    return out
