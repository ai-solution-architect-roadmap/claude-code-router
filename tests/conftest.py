import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"

# Memory notes are written at runtime (never checked in) so they cannot leak into real
# Claude Code sessions opened on this repository.
SHOP_MEMORY = ("# Notes\n\n"
               "- Be careful with `app/payments/ledger.py`: refund and rounding logic is "
               "critical.\n"
               "- `app/core/engine.py` is complex legacy code.\n")

ROUTE_REPOS = {"shop_py": FIX / "route" / "shop_py", "web_ts": FIX / "route" / "web_ts",
               "svc_go": FIX / "route" / "svc_go", "java": FIX / "graph" / "java",
               "rust": FIX / "graph" / "rust"}


def git_commit_all(root: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x",
           "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                       env=env)


@pytest.fixture(scope="session")
def route_env(tmp_path_factory):
    """{name: (root, MemoryReport)} for the routing fixture repos: git history + graph built,
    memory collected with an isolated HOME and no managed policy dirs."""
    from ccr.graph import update
    from ccr.memory import collect
    from ccr.memory import sources as S

    base = tmp_path_factory.mktemp("route")
    home = base / "home"
    (home / ".claude").mkdir(parents=True)
    saved = S.MANAGED_DIRS
    S.MANAGED_DIRS = []
    try:
        out = {}
        for name, src in ROUTE_REPOS.items():
            root = base / name
            shutil.copytree(src, root)
            if name == "shop_py":
                (root / "CLAUDE.md").write_text(SHOP_MEMORY)
            git_commit_all(root)
            update(root, workers=1, offline=True)
            out[name] = (root, collect(root, home=home, environ={"HOME": str(home)}))
        yield out
    finally:
        S.MANAGED_DIRS = saved
