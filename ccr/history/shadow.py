"""Counterfactual sampling: would a cheaper tier have done a task the router sent to a deep tier?

`ccr shadow` re-runs a few sampled deep-tier tasks (prompts from `hooks.store_prompts`) headless
at a lower tier, inside a throwaway `git worktree` of HEAD so your working tree is never touched,
and records whether that run succeeded in `.ccr/local/shadow.jsonl`. It uses your own `claude`
CLI, so it spends real usage: nothing runs without `--yes` and a total budget cap.
Success needs `--test-command` to mean anything; without one it only means "claude exited cleanly".
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ccr.config import TIERS
from ccr.history.learn import SUCCESS


def path(root) -> Path:
    return Path(root) / ".ccr" / "local" / "shadow.jsonl"


def read(root) -> list[dict]:
    out = []
    try:
        for line in path(root).read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                out.append(e)
    except OSError:
        pass
    return out


def append(root, entry: dict) -> None:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def pick(records: list[dict], logged: dict[str, dict], done: set[str], from_tier: str,
         sample: int, seed: int = 11) -> list[dict]:
    """Successful tasks at `from_tier` or above that have a stored prompt and no shadow run yet."""
    floor = TIERS.index(from_tier)
    pool = [r for r in records if r.get("tier") in TIERS and TIERS.index(r["tier"]) >= floor
            and r.get("outcome") in SUCCESS and r.get("id") not in done
            and (logged.get(str(r.get("id"))) or {}).get("prompt")]
    random.Random(seed).shuffle(pool)
    return pool[:sample]


def shadow_tier(deep: str, lower: str | None) -> str:
    return lower or TIERS[max(0, TIERS.index(deep) - 1)]


def run_one(root: Path, rec: dict, prompt: str, tier: str, cfg, *, budget: float,
            test_command: str | None, runner=None, test_runner=None, worktree=True) -> dict:
    from ccr.headless import run_task
    tmp = None
    work = root
    if worktree:
        tmp = Path(tempfile.mkdtemp(prefix="ccr-shadow-"))
        work = tmp / "wt"
        subprocess.run(["git", "worktree", "add", "--detach", str(work), "HEAD"], cwd=root,
                       check=True, capture_output=True)
    try:
        res = run_task(prompt, work, cfg=cfg, tier=tier, max_escalations=0,
                       max_budget_usd=budget, test_command=test_command,
                       permission_mode="acceptEdits", allow_llm=False, record=False,
                       runner=runner, test_runner=test_runner)
    finally:
        if worktree:
            subprocess.run(["git", "worktree", "remove", "--force", str(work)], cwd=root,
                           capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)
    a = res.attempts[-1]
    return {"id": rec["id"], "ts": int(time.time()), "deep_tier": rec["tier"],
            "shadow_tier": tier, "class": rec.get("class"), "area": rec.get("area"),
            "outcome": a.outcome, "reason": a.reason, "verified": bool(test_command),
            "test": a.test, "cost_usd": a.cost_usd, "tokens": a.tokens}


def summarize(entries: list[dict]) -> dict:
    n = len(entries)
    ok = sum(e.get("outcome") == "success" for e in entries)
    ver = [e for e in entries if e.get("verified")]
    pairs: dict[str, list[int]] = {}
    for e in entries:
        c = pairs.setdefault(f"{e.get('deep_tier')}->{e.get('shadow_tier')}", [0, 0])
        c[0] += e.get("outcome") == "success"
        c[1] += 1
    return {"n": n, "ok": ok, "rate": round(ok / n, 3) if n else None,
            "verified_n": len(ver), "verified_ok": sum(e.get("outcome") == "success" for e in ver),
            "pairs": {k: {"ok": v[0], "n": v[1]} for k, v in sorted(pairs.items())},
            "cost_usd": round(sum(e.get("cost_usd") or 0 for e in entries), 4)}


def format_lines(s: dict) -> list[str]:
    if not s["n"]:
        return []
    out = [f"Shadow:    a cheaper tier succeeded on {s['ok']}/{s['n']} sampled deep-tier task(s) "
           f"({100 * s['rate']:.0f}%); {s['verified_ok']}/{s['verified_n']} verified by tests"]
    out.append("           " + ", ".join(f"{k} {v['ok']}/{v['n']}" for k, v in s["pairs"].items()))
    if s["verified_n"] < s["n"]:
        out.append("           unverified runs only mean claude exited cleanly: use --test-command")
    return out
