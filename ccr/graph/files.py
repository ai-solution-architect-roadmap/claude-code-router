"""File discovery for the code graph: .gitignore aware, skips vendored/build/lock files."""
from __future__ import annotations

import os
import re
from pathlib import Path

from ccr.detect import SKIP_DIRS, SKIP_FILES

# Extension -> import "family" (drives regex import extraction and resolution).
EXT_FAMILY = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".mts": "js", ".cts": "js", ".vue": "js", ".svelte": "js",
    ".go": "go",
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".scala": "jvm", ".groovy": "jvm",
    ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c",
    ".hxx": "c", ".m": "c", ".mm": "c",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".zig": "zig", ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir", ".dart": "dart", ".swift": "swift",
    ".sh": "shell", ".bash": "shell",
}
CODE_EXTS = frozenset(EXT_FAMILY)
# Non-code files the resolver needs (module roots).
AUX_NAMES = frozenset({"go.mod", "Cargo.toml"})
EXTRA_SKIP_DIRS = frozenset({"bower_components", "Pods", "third_party", "coverage", "out",
                             ".svn", ".hg", ".cache", ".parcel-cache", ".turbo", "elm-stuff"})
ALL_SKIP_DIRS = frozenset(SKIP_DIRS) | EXTRA_SKIP_DIRS
MAX_PARSE_BYTES = 1_000_000

_TEST_DIR = {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e"}
_TEST_NAME = re.compile(
    r"(^test_.+|.+_test|.+[._-](test|spec)|.+Tests?|.+Spec|^Test[A-Z_].*|.+_spec)$")


def is_skipped_name(name: str) -> bool:
    return name in SKIP_FILES or ".min." in name or name.endswith((".map", ".lock"))


def is_code(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return os.path.splitext(name)[1].lower() in CODE_EXTS and not is_skipped_name(name)


def family_of(rel: str) -> str:
    return EXT_FAMILY.get(os.path.splitext(rel)[1].lower(), "")


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    if any(p.lower() in _TEST_DIR for p in parts[:-1]):
        return True
    stem = parts[-1]
    while "." in stem:
        stem, ext = stem.rsplit(".", 1)
        if ext.lower() in ("test", "spec"):
            return True
    return bool(_TEST_NAME.match(stem))


def in_skipped_dir(rel: str) -> bool:
    return any(p in ALL_SKIP_DIRS for p in rel.split("/")[:-1])


# --------------------------------------------------------------------------- gitignore
class GitIgnore:
    """Minimal .gitignore matcher (used only outside git repos; git does it natively)."""

    def __init__(self) -> None:
        self.rules: list[tuple[str, re.Pattern, bool, bool]] = []  # (base, rx, negate, dir_only)

    @staticmethod
    def _compile(pat: str) -> re.Pattern:
        anchored = "/" in pat.rstrip("/")
        pat = pat.lstrip("/")
        out, i = [], 0
        while i < len(pat):
            c = pat[i]
            if pat.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            if c == "*":
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            elif c == "[":
                j = pat.find("]", i)
                if j == -1:
                    out.append(re.escape(c))
                else:
                    out.append(pat[i:j + 1].replace("[!", "[^"))
                    i = j
            else:
                out.append(re.escape(c))
            i += 1
        body = "".join(out)
        prefix = "" if anchored else "(?:.*/)?"
        return re.compile(f"^{prefix}{body}(?:/.*)?$")

    def add_file(self, path: Path, base: str) -> None:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return
        for raw in lines:
            line = raw.rstrip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            if neg:
                line = line[1:]
            dir_only = line.endswith("/")
            try:
                self.rules.append((base, self._compile(line.rstrip("/")), neg, dir_only))
            except re.error:
                continue

    def ignored(self, rel: str, is_dir: bool) -> bool:
        result = False
        for base, rx, neg, dir_only in self.rules:
            if base:
                if not rel.startswith(base + "/"):
                    continue
                sub = rel[len(base) + 1:]
            else:
                sub = rel
            if dir_only and not is_dir:
                # "build/" must still match files below build/
                if "/" not in sub:
                    continue
                if not rx.match(sub.rsplit("/", 1)[0]):
                    continue
                result = not neg
                continue
            if rx.match(sub):
                result = not neg
        return result


def walk_files(root: Path) -> list[str]:
    """All non-ignored files under root (relative posix paths), without git."""
    gi = GitIgnore()
    out: list[str] = []
    root_s = str(root)
    for dirpath, dirnames, filenames in os.walk(root_s):
        rel_dir = os.path.relpath(dirpath, root_s).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if ".gitignore" in filenames:
            gi.add_file(Path(dirpath) / ".gitignore", rel_dir)
        keep = []
        for d in dirnames:
            if d in ALL_SKIP_DIRS or d.startswith(".") and d not in (".github",):
                continue
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if os.path.islink(os.path.join(dirpath, d)) or gi.ignored(rel, True):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not gi.ignored(rel, False):
                out.append(rel)
    return out
