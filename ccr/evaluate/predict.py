"""Step 2 of code-first routing: ask Haiku to predict the change a task will make.

Opt in with `predict.enabled`. Only asked when routing is unsure (low confidence, a code score
near a band edge, or keywords and code disagreeing by two tiers or more), with a hard
timeout, and answers are cached per prompt and repository state. Goes through the user's own
`claude` CLI like `ccr explain --llm` (ccr.evaluate.llm.run_claude), so tests mock one runner.

The model sees the prompt, the top located files with their symbol outlines from the code
graph (names and line spans, no source), and the uncommitted diff stat. It answers with one
JSON object:

  {"files": [{"path": "...", "confidence": 0.0-1.0}], "lines": int,
   "reach": "one_function" | "one_file" | "few_files" | "cross_cutting",
   "interface_change": bool, "schema_change": bool, "confidence": 0.0-1.0, "reason": "..."}

Predicted files that do not exist (and are not plausible new files next to located ones) are
dropped. The router turns the answer into a size (small / medium / large) and moves the
tier by one at most; see `ccr.route._predict_adjust`.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

REACHES = ("one_function", "one_file", "few_files", "cross_cutting")
CACHE_MAX = 300
MAX_FILES_IN_PROMPT = 5
MAX_SYMBOLS_PER_FILE = 25


@dataclass
class Prediction:
    files: list[tuple[str, float]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)      # predicted paths that do not exist
    lines: int | None = None
    reach: str | None = None
    interface_change: bool = False
    schema_change: bool = False
    confidence: float = 0.0
    reason: str = ""
    cached: bool = False
    elapsed_ms: float = 0.0
    cost_usd: float | None = None

    @property
    def size(self) -> str:
        """small | medium | large, from the predicted reach, lines and contract changes."""
        n = len([p for p, c in self.files if c >= 0.5])
        lines = self.lines if self.lines is not None else 0
        if self.schema_change or self.reach == "cross_cutting" or n >= 4 or lines >= 300:
            return "large"
        if self.reach in ("one_function", "one_file") and not self.interface_change \
                and n <= 1 and lines <= 30:
            return "small"
        return "medium"

    def summary(self) -> str:
        bits = [self.reach.replace("_", " ") if self.reach else "reach ?"]
        if self.lines is not None:
            bits.append(f"~{self.lines} lines")
        if self.files:
            bits.append(f"{len(self.files)} file(s)")
        if self.interface_change:
            bits.append("interface change")
        if self.schema_change:
            bits.append("schema change")
        return ", ".join(bits) + f", confidence {self.confidence:.2f}"

    def to_dict(self) -> dict:
        return {"files": [{"path": p, "confidence": c} for p, c in self.files],
                "dropped": self.dropped, "lines": self.lines, "reach": self.reach,
                "interface_change": self.interface_change, "schema_change": self.schema_change,
                "confidence": self.confidence, "reason": self.reason, "size": self.size,
                "cached": self.cached, "elapsed_ms": round(self.elapsed_ms, 1),
                "cost_usd": self.cost_usd}


PROMPT = """You predict the code change a software task will make, for model routing. Reply
with ONE JSON object only, no prose, no code fences:
{{"files": [{{"path": "<project-relative path>", "confidence": 0.0-1.0}}],
 "lines": <estimated lines added+deleted>,
 "reach": "one_function"|"one_file"|"few_files"|"cross_cutting",
 "interface_change": <true if a public function signature, CLI option, API or config contract
                      changes>,
 "schema_change": <true if a database schema, migration or stored data format changes>,
 "confidence": 0.0-1.0, "reason": "<= 15 words"}}
List only files that will be edited or created (at most 6). Judge the edit, not the file:
a typo in a large file is one_function and a few lines. Investigation-only work that ends in
a small fix counts as the fix.

Located files (symbol outlines: kind name lines):
{outline}

Uncommitted changes: {diffstat}

Task:
<<<
{task}
>>>"""


def outline(graph, paths: list[str]) -> str:
    out = []
    for p in paths[:MAX_FILES_IN_PROMPT]:
        out.append(f"- {p}")
        if graph is None:
            continue
        try:
            rows = graph.conn.execute(
                "SELECT kind, name, start_line, end_line FROM nodes WHERE path = ? AND "
                "kind != 'file' ORDER BY start_line LIMIT ?",
                (p, MAX_SYMBOLS_PER_FILE)).fetchall()
        except Exception:
            rows = []
        for kind, name, a, b in rows:
            out.append(f"    {kind} {name} {a}-{b}")
    return "\n".join(out) or "(none located)"


def build_prompt(task: str, graph, paths: list[str], workspace=None) -> str:
    if workspace is not None and getattr(workspace, "dirty", None):
        items = sorted(workspace.dirty.items(), key=lambda kv: -kv[1])[:8]
        diffstat = ", ".join(f"{p} ({n} lines)" for p, n in items)
    else:
        diffstat = "none"
    return PROMPT.format(outline=outline(graph, paths), diffstat=diffstat,
                         task=task.strip()[:3000])


def _rel(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def parse(text: str) -> Prediction | None:
    """Lenient JSON extraction; None when the answer has nothing usable."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    pr = Prediction()
    for f in obj.get("files") or []:
        if isinstance(f, str):
            path, conf = f, 0.7
        elif isinstance(f, dict) and isinstance(f.get("path"), str):
            path, c = f["path"], f.get("confidence", 0.7)
            conf = float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else 0.7
        else:
            continue
        if path.strip():
            pr.files.append((_rel(path), round(max(0.0, min(1.0, conf)), 2)))
    pr.files = pr.files[:6]
    n = obj.get("lines")
    if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0:
        pr.lines = int(n)
    r = str(obj.get("reach") or "").strip().lower()
    pr.reach = r if r in REACHES else None
    pr.interface_change = obj.get("interface_change") is True
    pr.schema_change = obj.get("schema_change") is True
    c = obj.get("confidence")
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        pr.confidence = max(0.0, min(1.0, float(c)))
    pr.reason = str(obj.get("reason") or "")[:200]
    if pr.reach is None and pr.lines is None and not pr.files:
        return None
    return pr


def verify_files(pr: Prediction, root: Path, graph, located: list[str]) -> None:
    """Drop predicted files that do not exist, unless a new file sits in a located directory."""
    dirs = {posixpath.dirname(p) for p in located}
    kept = []
    for path, conf in pr.files:
        exists = (graph is not None and graph.file_row(path) is not None) \
            or (root / path).is_file()
        if exists or posixpath.dirname(path) in dirs:
            kept.append((path, conf))
        else:
            pr.dropped.append(path)
    pr.files = kept
    if pr.dropped and not kept:
        pr.confidence = round(pr.confidence * 0.5, 3)   # it guessed at files it cannot see


# --------------------------------------------------------------------------- cache

def _cache_path(root: Path) -> Path:
    return root / ".ccr" / "local" / "predict-cache.json"


def cache_key(task: str, paths: list[str], workspace=None, head: str | None = None) -> str:
    dirty = sorted((workspace.dirty or {}).items()) if workspace is not None else []
    blob = json.dumps([task.strip(), paths[:MAX_FILES_IN_PROMPT], dirty, head])
    return hashlib.sha256(blob.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def cache_get(root: Path, key: str) -> Prediction | None:
    try:
        data = json.loads(_cache_path(root).read_text())
        hit = data.get(key)
        if not isinstance(hit, dict):
            return None
        pr = parse(json.dumps(hit["p"]))
        if pr is not None:
            pr.files = [(p, c) for p, c in hit.get("files", [])]
            pr.cached = True
        return pr
    except Exception:
        return None


def cache_put(root: Path, key: str, pr: Prediction) -> None:
    """Best effort; keeps the newest CACHE_MAX entries. Stores the answer only, no prompt."""
    try:
        p = _cache_path(root)
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data[key] = {"t": time.time(), "files": [[a, c] for a, c in pr.files],
                     "p": {"lines": pr.lines, "reach": pr.reach,
                           "interface_change": pr.interface_change,
                           "schema_change": pr.schema_change, "confidence": pr.confidence,
                           "reason": pr.reason}}
        if len(data) > CACHE_MAX:
            keep = sorted(data.items(), key=lambda kv: -float(kv[1].get("t", 0)))[:CACHE_MAX]
            data = dict(keep)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)
    except Exception:
        pass


# --------------------------------------------------------------------------- entry

def predict(task: str, root, graph, located: list[str], *, workspace=None, cfg=None,
            runner=None) -> tuple[Prediction | None, str]:
    """(prediction or None, status). Never raises."""
    from ccr.evaluate import llm as L
    root = Path(root)
    pc = cfg["predict"] if cfg is not None else {"model": "haiku", "effort": "low",
                                                 "timeout_s": 10.0}
    head = None
    try:
        from ccr.graph import git as G
        out = G._git(root, "rev-parse", "-q", "--verify", "HEAD", timeout=0.5)
        head = out.strip() if out else None
    except Exception:
        pass
    key = cache_key(task, located, workspace, head)
    hit = cache_get(root, key)
    if hit is not None:
        return hit, "cached"
    t0 = time.perf_counter()
    try:
        prompt = build_prompt(task, graph, located, workspace)
        if runner is None:
            data = L.run_claude(prompt, model=pc["model"], effort=pc["effort"],
                                timeout=float(pc["timeout_s"]), lean=True)
        else:
            data = runner(prompt, model=pc["model"], effort=pc["effort"],
                          timeout=float(pc["timeout_s"]))
    except L.LLMUnavailable as e:
        return None, f"unavailable: {e}"
    except Exception as e:  # a broken runner must not break routing
        return None, f"failed: {e.__class__.__name__}: {e}"
    pr = parse(str(data.get("result") or ""))
    if pr is None:
        return None, "no usable prediction"
    pr.elapsed_ms = (time.perf_counter() - t0) * 1000
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        pr.cost_usd = float(cost)
    verify_files(pr, root, graph, located)
    cache_put(root, key, pr)
    return pr, f"{pc['model']} in {pr.elapsed_ms:.0f} ms"
