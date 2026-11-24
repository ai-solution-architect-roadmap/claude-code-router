"""What is changing in the working tree right now: uncommitted edits and recently committed
files. Used by code-first routing (always on) to find the files a prompt is about
from the real repository state instead of from prompt words alone.

Two or three short git calls, each with a tight timeout. Outside git, or when git is slow or
missing, the snapshot is empty and routing carries on without it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

GIT_TIMEOUT_S = 1.0
MAX_FILES = 200          # ignore a working tree with more changes than this (mass rewrite)


@dataclass
class Workspace:
    # path (relative to root) -> lines added + deleted; untracked files count their lines
    dirty: dict[str, int] = field(default_factory=dict)
    untracked: set[str] = field(default_factory=set)
    recent: list[str] = field(default_factory=list)     # files in the last N commits, newest first
    available: bool = False                             # git answered
    notes: list[str] = field(default_factory=list)

    @property
    def dirty_lines(self) -> int:
        return sum(self.dirty.values())

    def lines(self, paths) -> int:
        return sum(self.dirty.get(p, 0) for p in paths)


def _count_lines(root: Path, rel: str, cap: int = 5000) -> int:
    try:
        with open(root / rel, "rb") as f:
            n = 0
            for _ in f:
                n += 1
                if n >= cap:
                    break
            return n
    except OSError:
        return 0


def snapshot(root: str | Path, recent_commits: int = 5) -> Workspace:
    """Uncommitted changes (staged + unstaged + untracked) and the files of the last
    `recent_commits` commits, relative to `root`."""
    from ccr.graph import git as G
    root = Path(root)
    ws = Workspace()
    numstat = G._git(root, "diff", "HEAD", "--numstat", "-z", "--relative", "--no-renames", "--",
                     timeout=GIT_TIMEOUT_S)
    if numstat is None:      # outside git, no HEAD yet, or git too slow
        numstat = G._git(root, "diff", "--cached", "--numstat", "-z", "--relative",
                         "--no-renames", "--", timeout=GIT_TIMEOUT_S)
        if numstat is None:
            return ws
    ws.available = True
    for rec in numstat.split("\0"):
        parts = rec.split("\t")
        if len(parts) != 3 or not parts[2]:
            continue
        add, rem, path = parts
        n = (int(add) if add.isdigit() else 0) + (int(rem) if rem.isdigit() else 0)
        ws.dirty[path] = max(n, 1)
    untracked = G._git(root, "ls-files", "-z", "-o", "--exclude-standard", "--", ".",
                       timeout=GIT_TIMEOUT_S)
    for p in (untracked or "").split("\0"):
        if p and p not in ws.dirty:
            ws.untracked.add(p)
            ws.dirty[p] = max(_count_lines(root, p), 1)
    if len(ws.dirty) > MAX_FILES:
        ws.notes.append(f"{len(ws.dirty)} files changed: too many to use as a routing signal")
        ws.dirty, ws.untracked = {}, set()
    if recent_commits > 0:
        log = G._git(root, "log", f"-n{int(recent_commits)}", "--no-merges", "--no-renames",
                     "--name-only", "--format=", "--relative", "--", ".", timeout=GIT_TIMEOUT_S)
        seen: set[str] = set()
        for line in (log or "").split("\n"):
            p = line.strip()
            if p and p not in seen:
                seen.add(p)
                ws.recent.append(p)
    return ws
