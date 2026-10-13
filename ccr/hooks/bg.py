"""Background graph refresh started by the SessionStart hook: `python -m ccr.hooks.bg ROOT`.

Offline (never downloads grammars), single worker, guarded by .ccr/local/graph.lock so
concurrent sessions do not build at the same time. A lock older than LOCK_STALE_S is
considered abandoned.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

LOCK_STALE_S = 600


def lock_path(root: Path) -> Path:
    return Path(root) / ".ccr" / "local" / "graph.lock"


def lock_held(root: Path) -> bool:
    try:
        return time.time() - lock_path(root).stat().st_mtime < LOCK_STALE_S
    except OSError:
        return False


def acquire(root: Path) -> bool:
    p = lock_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not lock_held(root):
        try:
            p.unlink()
        except OSError:
            return False
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def main(argv: list[str]) -> int:
    if not argv:
        return 0
    root = Path(argv[0]).resolve()
    if not acquire(root):
        return 0
    t0 = time.perf_counter()
    from ccr.hooks.common import log
    try:
        from ccr.graph import update
        r = update(root, offline=True, workers=1)
        log(root, f"bg graph refresh: parsed {r.parsed}, removed {r.removed} in "
                  f"{(time.perf_counter() - t0) * 1000:.0f}ms")
    except Exception as e:  # noqa: BLE001
        log(root, f"bg graph refresh failed: {e.__class__.__name__}: {e}")
    finally:
        try:
            lock_path(root).unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
