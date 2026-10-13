"""Shared helpers for hooks and the status line: stdlib only, nothing slow at import."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

MAX_STDIN = 4 * 1024 * 1024
LOG_MAX = 512 * 1024
SESSION_TTL_S = 7 * 86400
_SID = re.compile(r"[^A-Za-z0-9_.-]")
MODEL_RANK = (("haiku", 0), ("sonnet", 1), ("opus", 2), ("fable", 3))
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}


def parse_stdin(raw: bytes | str | None) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def find_root(data: dict | None, environ=None) -> Path | None:
    """Nearest directory holding `.ccr/` above CLAUDE_PROJECT_DIR or the hook's cwd."""
    env = os.environ if environ is None else environ
    starts = []
    for v in (env.get("CLAUDE_PROJECT_DIR"),
              (data or {}).get("cwd") if isinstance(data, dict) else None):
        if isinstance(v, str) and v:
            starts.append(v)
    ws = (data or {}).get("workspace") if isinstance(data, dict) else None
    if isinstance(ws, dict):
        for k in ("project_dir", "current_dir"):
            if isinstance(ws.get(k), str) and ws[k]:
                starts.append(ws[k])
    home = Path.home()
    for s in starts:
        p = Path(s)
        for cand in (p, *p.parents):
            if (cand / ".ccr").is_dir():
                return cand
            if cand == home:
                break
    return None


def local_dir(root: Path) -> Path:
    d = root / ".ccr" / "local"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def session_path(root: Path, sid: str) -> Path:
    return root / ".ccr" / "local" / "sessions" / (_SID.sub("_", sid)[:128] + ".json")


def load_session(root: Path, sid: str | None) -> dict:
    return read_json(session_path(root, sid)) if sid else {}


def save_session(root: Path, sid: str | None, data: dict) -> None:
    if sid:
        data["updated"] = time.time()
        write_json_atomic(session_path(root, sid), data)


def log(root: Path | None, line: str) -> None:
    """Append one line to .ccr/local/hooks.log (bounded). Never raises."""
    if root is None:
        return
    try:
        p = local_dir(root) / "hooks.log"
        try:
            if p.stat().st_size > LOG_MAX:
                keep = p.read_bytes()[-LOG_MAX // 2:]
                p.write_bytes(keep[keep.find(b"\n") + 1:])
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line.replace("\n", " ") + "\n")
    except Exception:
        pass


def model_rank(model: str | None) -> int | None:
    if not isinstance(model, str):
        return None
    m = model.lower()
    for name, r in MODEL_RANK:
        if name in m:
            return r
    return None


def model_alias(model: str | None) -> str | None:
    r = model_rank(model)
    return None if r is None else MODEL_RANK[r][0]


def effort_level(v) -> str | None:
    if isinstance(v, dict):
        v = v.get("level")
    return v if isinstance(v, str) and v in EFFORT_RANK else None


def main_model(root: Path, sid: str | None, session: dict) -> tuple[str | None, str | None]:
    """(model id, effort) of the main session: the freshest of the status line relay
    (per session), SessionStart's `model` and the last model seen in the transcript."""
    cands = []
    st = read_json(root / ".ccr" / "local" / "state.json")
    sess = (st.get("sessions") or {}).get(sid) if sid and isinstance(st.get("sessions"),
                                                                    dict) else None
    if isinstance(sess, dict) and sess.get("model"):
        cands.append((float(sess.get("ts") or 0), sess.get("model"), sess.get("effort")))
    elif not sid and isinstance(st.get("model"), dict) and st["model"].get("id"):
        cands.append((float(st.get("ts") or 0), st["model"]["id"], effort_level(st.get("effort"))))
    mm = session.get("main_model")
    if isinstance(mm, dict) and mm.get("id"):
        cands.append((float(mm.get("ts") or 0), mm["id"], mm.get("effort")))
    if not cands:
        return None, None
    cands.sort(key=lambda c: c[0])
    model = cands[-1][1]
    effort = next((c[2] for c in reversed(cands) if c[2]), None)
    return model, effort
