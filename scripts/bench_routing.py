"""Routing benchmark: `uv run python scripts/bench_routing.py [TOKENS_PER_TASK] [--json] [--prompts FILE.json]`.

Replays a labelled prompt set through the router against the shop_py fixture (no API calls).
Each prompt carries the lowest tier that should handle it (`need`) and the highest that is not
a waste (`cap`). Reports the tier mix, under-routed prompts (tier < need), over-routed ones
(tier > cap) and the estimated cost against all-Opus and all-Sonnet baselines, pricing every
task at TOKENS_PER_TASK (default 50000) with the `pricing:` rates. The labels are judgment
calls: edit PROMPTS to match your own work. It measures routing quality, not answer quality.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (prompt, need, cap): tier indexes, T0=0 .. T4=4
PROMPTS = [
    ("where is the pagination helper defined?", 0, 1),
    ("fix the typo in the README where it says 'recieve'", 0, 1),
    ("Please could you have a look at the README, which a few of us have been editing for "
     "weeks, and fix the single typo 'recieve' in the intro paragraph", 0, 1),
    ("add a test for the pagination helper", 1, 2),
    ("fix the off-by-one error in paginate() in app/api/pagination.py", 1, 2),
    ("add a CSV export option to app/reports/export.py", 2, 3),
    ("refactor app/core/engine.py into smaller functions", 2, 3),
    ("fix login sometimes failing", 3, 4),
    ("the sync job fails intermittently, figure out why", 3, 3),
    ("the checkout flow intermittently double charges customers, figure out why", 3, 4),
    ("rework the payment flow", 3, 4),
    ("drop the legacy_id column from the users table in production", 4, 4),
    ("what does the Engine class do?", 0, 1),
    ("rename the helper in app/utils/strings.py", 0, 1),
    ("write unit tests for app/utils/strings.py", 1, 2),
    ("fix the typo in the docstring of paginate()", 0, 1),
    ("add a --dry-run flag to app/main.py", 1, 2),
    ("speed up the report export, it is slow for large datasets", 2, 3),
    ("rotate the signing key used for login tokens", 3, 4),
    ("investigate why refunds are sometimes applied twice", 3, 4),
    ("the export job occasionally hangs, find the root cause", 3, 3),
    ("fix the bug where the page size is ignored in app/api/handlers.py", 1, 2),
    ("migrate the orders table to add a status column", 3, 4),
    ("harden the session token validation", 3, 4),
    ("tidy up app/reports/export.py", 1, 2),
    ("1. add a --verbose flag to app/main.py\n2. pass it to Engine\n3. add a test", 1, 2),
]


def setup() -> Path:
    from ccr.graph import update
    root = Path(tempfile.mkdtemp(prefix="ccr-bench-")) / "shop"
    shutil.copytree(REPO / "tests" / "fixtures" / "route" / "shop_py", root)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=b", "-c", "user.email=b@x", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    update(root, workers=1, offline=True)
    return root


def load_prompts(path: str | None) -> list:
    """PROMPTS, or a JSON file of [prompt, need, cap] triples (tier numbers 0-4)."""
    if not path:
        return PROMPTS
    return [(p, int(a), int(b)) for p, a, b in json.loads(Path(path).read_text())]


def replay(root: Path, tokens: int = 50_000, prompts: list | None = None) -> dict:
    from ccr.config import TIERS, default_config
    from ccr.graph import Graph
    from ccr.history.savings import estimate
    from ccr.memory import collect
    from ccr.route import decide
    cfg = default_config()
    memory = collect(root, nested=False)
    rows = []
    with Graph.open(root) as g:
        for prompt, need, cap in prompts or PROMPTS:
            d = decide(prompt, root, cfg=cfg, graph=g, memory=memory, history_records=[], now=0,
                       nested_memory=False)
            i = TIERS.index(d.tier.name)
            rows.append({"prompt": prompt, "tier": d.tier.name, "need": TIERS[need],
                         "cap": TIERS[cap], "class": d.task_class.kind,
                         "under": i < need, "over": i > cap})
    est = estimate([{"tier": r["tier"], "tokens": tokens} for r in rows], cfg)
    mix = {t: sum(r["tier"] == t for r in rows) for t in TIERS}
    return {"rows": rows, "mix": mix, "under": sum(r["under"] for r in rows),
            "over": sum(r["over"] for r in rows), "total": len(rows), "estimate": est}


def main(argv: list[str]) -> int:
    tokens = next((int(a) for a in argv if a.isdigit()), 50_000)
    argv = list(argv)
    path = argv[argv.index("--prompts") + 1] if "--prompts" in argv else None
    out = replay(setup(), tokens, load_prompts(path))
    if "--json" in argv:
        print(json.dumps(out, indent=2))
        return 0
    for r in out["rows"]:
        flag = "UNDER" if r["under"] else "OVER " if r["over"] else "ok   "
        print(f"{flag} {r['tier']} (want {r['need']}-{r['cap']}) {r['class']:<9} "
              f"{r['prompt'][:70].replace(chr(10), ' / ')}")
    e = out["estimate"]
    print(f"\ntier mix: {out['mix']}; under-routed {out['under']}/{out['total']}, "
          f"over-routed {out['over']}/{out['total']}")
    print(f"est. cost at {tokens} tokens/task: ${e['routed_usd']:.2f} routed vs "
          f"${e['all_opus_usd']:.2f} all-Opus ({100 * (e['saved_vs_opus'] or 0):.0f}% saved), "
          f"${e['all_sonnet_usd']:.2f} all-Sonnet ({100 * (e['saved_vs_sonnet'] or 0):.0f}%)")
    return 1 if out["under"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
