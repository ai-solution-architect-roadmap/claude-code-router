"""Hook latency benchmark: `uv run python scripts/bench_hooks.py [PROJECT]`.

Without PROJECT, copies tests/fixtures/route/shop_py to a temp dir, commits it and builds
the graph. Reports warm in-process time per hook and the end-to-end time of the real
process (`python -m ccr hook <name>` / `python -m ccr statusline`), which is what Claude
Code waits for.
"""
from __future__ import annotations

import io
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPTS = ["fix the refund rounding bug in app/payments/ledger.py",
           "add a test for the pagination helper",
           "the checkout flow intermittently double charges customers, figure out why",
           "where is the pagination helper defined?"]


def setup() -> Path:
    from ccr.graph import update
    root = Path(tempfile.mkdtemp(prefix="ccr-bench-")) / "shop"
    shutil.copytree(REPO / "tests" / "fixtures" / "route" / "shop_py", root)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=b", "-c", "user.email=b@x", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    update(root, workers=1, offline=True)
    return root


def payloads(root: Path) -> dict[str, list[bytes]]:
    base = {"session_id": "bench", "cwd": str(root), "transcript_path": str(root / "t.jsonl")}
    return {
        "prompt": [json.dumps({**base, "prompt": p}).encode() for p in PROMPTS],
        "session-start": [json.dumps({**base, "source": "startup"}).encode()],
        "stop": [json.dumps({**base, "hook_event_name": "Stop"}).encode()],
        "statusline": [json.dumps({**base, "model": {"id": "claude-sonnet-5-5"},
                                   "rate_limits": {"seven_day": {"used_percentage": 40}}}
                                  ).encode()],
    }


def in_process(name: str, data: bytes) -> float:
    from ccr.hooks import main as hook_main
    from ccr.statusline import main as sl_main
    t0 = time.perf_counter()
    if name == "statusline":
        sl_main([], stdin=io.BytesIO(data), stdout=io.StringIO(), environ={})
    else:
        hook_main([name], stdin=io.BytesIO(data), stdout=io.StringIO(), environ={},
                  hard_exit=False)
    return (time.perf_counter() - t0) * 1000


def process(name: str, data: bytes, cwd: Path) -> float:
    args = ["statusline"] if name == "statusline" else ["hook", name]
    t0 = time.perf_counter()
    subprocess.run([sys.executable, "-m", "ccr", *args], input=data, capture_output=True,
                   cwd=cwd, check=True)
    return (time.perf_counter() - t0) * 1000


TOUCHED = (".ccr/history.jsonl", ".ccr/local/state.json", ".ccr/local/sessions/bench.json")


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else setup()
    print(f"project: {root}")
    saved = {rel: ((root / rel).read_bytes() if (root / rel).exists() else None)
             for rel in TOUCHED}
    try:
        run(root)
    finally:   # leave a real project exactly as it was
        for rel, data in saved.items():
            p = root / rel
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)


def run(root: Path) -> None:
    for name, items in payloads(root).items():
        in_process(name, items[0])                       # warm up
        warm = [in_process(name, d) for _ in range(5) for d in items]
        proc = [process(name, d, root) for _ in range(3) for d in items]
        print(f"{name:<14} in-process warm median {statistics.median(warm):6.1f} ms "
              f"(max {max(warm):6.1f}) | real process median {statistics.median(proc):6.1f} ms "
              f"(max {max(proc):6.1f})")


if __name__ == "__main__":
    main()
