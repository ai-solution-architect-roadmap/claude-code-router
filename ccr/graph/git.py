"""Thin git helpers. Every function degrades to None/empty when git is unavailable."""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitInfo:
    top: str
    prefix: str  # root relative to the repo top level, "" or "sub/dir/"
    head: str | None


def _git(root: Path, *args: str, timeout: float = 30) -> str | None:
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-C", str(root), "-c", "core.quotepath=off", *args],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="surrogateescape")


def info(root: Path) -> GitInfo | None:
    out = _git(root, "rev-parse", "--show-toplevel", "--show-prefix")
    if out is None:
        return None
    lines = out.split("\n")
    top = lines[0].strip()
    prefix = lines[1].strip() if len(lines) > 1 else ""
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    return GitInfo(top=top, prefix=prefix, head=head.strip() if head else None)


def ls_files(root: Path) -> list[str] | None:
    """Tracked + untracked-not-ignored files, relative to root."""
    out = _git(root, "ls-files", "-z", "-c", "-o", "--exclude-standard")
    if out is None:
        return None
    return sorted({p for p in out.split("\0") if p})


def changed_since(root: Path, commit: str) -> set[str] | None:
    """Files differing between `commit` and the working tree (committed + staged + unstaged)."""
    out = _git(root, "diff", "--name-only", "-z", "--relative", "--no-renames", commit, "--")
    if out is None:
        return None
    return {p for p in out.split("\0") if p}


def churn(root: Path, gi: GitInfo, days: int = 180,
          max_commits: int = 20000) -> dict[str, tuple[int, int, int]]:
    """path -> (commit count, distinct authors, last commit unix ts) within the window."""
    since = f"{int(time.time()) - days * 86400}"
    out = _git(root, "log", f"--since={since}", f"--max-count={max_commits}", "--no-merges",
               "--no-renames", "--name-only", "--format=%x1e%ae%x1f%ct", "--relative", "--",
               ".", timeout=120)
    if not out:
        return {}
    stats: dict[str, list] = {}
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        head, _, rest = block.partition("\n")
        author, _, ts = head.partition("\x1f")
        try:
            ts_i = int(ts.strip())
        except ValueError:
            ts_i = 0
        for line in rest.split("\n"):
            p = line.strip()
            if not p:
                continue
            s = stats.get(p)
            if s is None:
                stats[p] = [1, {author}, ts_i]
            else:
                s[0] += 1
                s[1].add(author)
                if ts_i > s[2]:
                    s[2] = ts_i
    return {p: (c, len(a), t) for p, (c, a, t) in stats.items()}
