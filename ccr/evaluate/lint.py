"""Prompt linter: one short hint when a prompt is too vague to act on well.

It reuses the routing Decision (class, vagueness, located code), so it costs no model call and
almost no time. It never blocks a prompt unless `lint.mode: strict`, and it stays silent when
the prompt is clear. At most one finding per prompt, in this order:

  missing_path  a named file does not exist (`ccr/confg.py`): suggest the closest real file
  no_anchor     a change request names no code ccr can find and is vague ("the app crashes",
                "improve error handling", "fix the bug"): suggest the likely file, or ask for
                the file, command or error message

Questions, design talks, trivial edits, step-by-step prompts and prompts that name code are
never linted: Claude can explore for those, and a hint would only add noise.
"""
from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("off", "hint", "strict")
# kinds a linter hint can help: a change whose target or goal is missing
LINTED_KINDS = {"bugfix", "debugging", "feature", "refactor", "perf", "migration", "security"}
BUG_KINDS = {"bugfix", "debugging"}
SUGGEST_MIN_CONF = 0.2     # weakest located candidate worth offering as "did you mean"
SHORT_BUG_WORDS = 5        # "fix the bug", "it's broken": too short to say where
GOAL_TERMS = {"better", "improve", "nicer", "cleaner", "properly", "make it work"}
EDIT_GUESS = "prompt names no file"    # locate.py's reason for an unanchored workspace edit
_FIXTURE = re.compile(r"(?:^|/)(?:fixtures?|testdata|__snapshots__|vendor|node_modules)/")
_FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


@dataclass
class Lint:
    code: str                         # missing_path | no_anchor
    hint: str                         # one user-facing line
    suggest: list[str] = field(default_factory=list)

    def record(self) -> dict:
        """History fields: no prompt text and no file paths (history may be committed)."""
        return {"code": self.code, "suggested": bool(self.suggest)}


def mode(cfg) -> str:
    m = (cfg["lint"]["mode"] if cfg is not None else "hint") or "off"
    return m if m in MODES else "hint"


def lint(prompt: str, d, *, cfg=None, root: str | Path | None = None,
         files: list[str] | None = None) -> Lint | None:
    """The one finding for this prompt, or None when it is clear enough. `files`: the
    project's files for "did you mean" on a missing path (read from the code graph when
    omitted and needed)."""
    if cfg is not None and mode(cfg) == "off":
        return None
    tc, loc = d.task_class, d.location
    if tc.steps:
        return None
    missing = _missing_paths(loc.unresolved, root)
    if missing:
        if files is None:
            files = _graph_files(root)
        return _missing(missing[0], files)
    if tc.kind not in LINTED_KINDS or loc.located or loc.explicit or loc.symbols:
        return None
    goal = [t for t in tc.vague_terms if t in GOAL_TERMS]
    vague = [t for t in tc.vague_terms if t not in GOAL_TERMS]
    short_bug = (tc.kind in BUG_KINDS and not loc.candidates and tc.words <= SHORT_BUG_WORDS
                 and not tc.hard)
    if not (goal or vague or short_bug):
        return None
    guess, from_edits = _guess(loc)
    if guess and from_edits:
        hint = (f"No file named. Is this about your uncommitted edits in `{guess}`? Naming "
                "the file helps Claude start in the right place.")
    elif guess:
        hint = (f"No file named. Did you mean `{guess}`? Naming it helps Claude start in the "
                "right place.")
    elif tc.kind in BUG_KINDS:
        hint = ("No file, command or error named. Say where it happens or paste the error "
                "message.")
    elif goal:
        hint = (f"'{goal[0]}' does not say what done looks like. Name the file and the "
                "result you want.")
    else:
        hint = "No file or code named. Say which part of the code this is about."
    return Lint("no_anchor", hint, [guess] if guess else [])


def _guess(loc) -> tuple[str | None, bool]:
    """(best weakly-located file, whether it was picked only because it has uncommitted
    edits). Tests and fixtures are skipped: a guess should be code the user would edit."""
    for c in loc.candidates:
        if c.confidence < SUGGEST_MIN_CONF:
            break
        if not c.is_test and not _FIXTURE.search(c.path) and "/tests/" not in "/" + c.path:
            edits_only = bool(c.reasons) and all(EDIT_GUESS in r for r in c.reasons)
            return c.path, edits_only
    return None, False


def _missing_paths(unresolved: list[str], root) -> list[str]:
    """Named files that are really missing: file-shaped (has an extension) and absent on
    disk. Directories and gitignored paths that exist are not findings."""
    out = []
    for p in unresolved:
        p = p.strip().rstrip("/")
        if not p or not _FILE_LIKE.search(posixpath.basename(p)):
            continue
        if root is not None:
            try:
                if (Path(root) / p).exists():
                    continue
            except OSError:
                continue
        out.append(p)
    return out


def _missing(path: str, files: list[str]) -> Lint:
    best = closest(path, files)
    if best:
        return Lint("missing_path", f"`{path}` does not exist. Did you mean `{best}`?", [best])
    return Lint("missing_path", f"`{path}` does not exist in this project. Check the path.")


def closest(path: str, files: list[str]) -> str | None:
    """The project file most like `path`: same basename first, then a close spelling."""
    if not files:
        return None
    path = path.lstrip("./")
    base = posixpath.basename(path).lower()
    same = [f for f in files if posixpath.basename(f).lower() == base]
    if same:
        return max(same, key=lambda f: difflib.SequenceMatcher(None, path, f).ratio())
    hit = difflib.get_close_matches(path, files, n=1, cutoff=0.75)
    if hit:
        return hit[0]
    bases = {}
    for f in files:
        bases.setdefault(posixpath.basename(f).lower(), f)
    hit = difflib.get_close_matches(base, list(bases), n=1, cutoff=0.8)
    return bases[hit[0]] if hit else None


def _graph_files(root) -> list[str]:
    if root is None:
        return []
    try:
        from ccr.graph.query import Graph
        g = Graph.open(root)
    except Exception:  # noqa: BLE001
        return []
    if g is None:
        return []
    try:
        return [r[0] for r in g.conn.execute("SELECT path FROM nodes WHERE kind = 'file'")]
    except Exception:  # noqa: BLE001
        return []
    finally:
        g.close()
