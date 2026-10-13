"""Optional LLM assist for low-confidence classifications.

Uses only the user's own `claude` CLI found on PATH (`claude -p --model haiku --effort low`),
never an API key or SDK. Disabled in hooks; callers opt in (`ccr explain --llm`, `ccr run`).
Everything funnels through `run_claude`, so tests replace that one function.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from ccr.config import CLASSES

SCOPES = ("small", "medium", "large")


@dataclass
class LLMHint:
    kind: str | None = None
    scope: str | None = None
    vague: bool | None = None
    files: list[str] = field(default_factory=list)
    confidence: float | None = None
    reason: str = ""
    cost_usd: float | None = None


class LLMUnavailable(RuntimeError):
    pass


def claude_command(model: str, effort: str) -> list[str]:
    exe = shutil.which("claude")
    if not exe:
        raise LLMUnavailable("`claude` CLI not found on PATH")
    # no tools, no MCP servers, no saved session: a single cheap classification turn
    return [exe, "-p", "--model", model, "--effort", effort, "--output-format", "json",
            "--tools", "", "--strict-mcp-config", "--no-session-persistence"]


def run_claude(prompt: str, *, model: str = "haiku", effort: str = "low",
               timeout: float = 45) -> dict:
    """Run the user's claude CLI headlessly; return its JSON result object. Mock me in tests."""
    cmd = claude_command(model, effort)
    env = {**os.environ, "CCR_INTERNAL": "1"}   # ccr hooks no-op inside this call
    env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired as e:
        raise LLMUnavailable(f"claude timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise LLMUnavailable(f"cannot run claude: {e}") from e
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out.splitlines()[-1] if out else "")
    except (ValueError, IndexError) as e:
        raise LLMUnavailable(f"claude returned non-JSON output (exit {r.returncode}): "
                             f"{(r.stderr or out)[:200]}") from e
    if not isinstance(data, dict):
        raise LLMUnavailable("claude JSON output is not an object")
    if data.get("is_error"):
        raise LLMUnavailable(f"claude reported an error: {str(data.get('result'))[:200]}")
    return data


PROMPT = """You classify a software task for model routing. Reply with ONE JSON object only, no
prose, no code fences:
{{"class": one of {classes}, "scope": "small"|"medium"|"large", "vague": true|false,
 "files": [up to 5 project-relative paths from the summary most likely touched],
 "confidence": 0.0-1.0, "reason": "<= 15 words"}}
Classes: question=explain/lookup only; trivial=typo/rename/format/version bump; tests=write tests;
bugfix=clear bug; feature=new behaviour; refactor=restructure without behaviour change;
perf=speed/memory; migration=framework/language/schema migration; security=vulnerability/auth
hardening; debugging=unclear, intermittent or concurrency bug needing investigation.

Project summary:
{summary}

Rule-based guess: {guess}

Task:
<<<
{task}
>>>"""


def build_prompt(task: str, summary: str, guess: str) -> str:
    return PROMPT.format(classes="|".join(f'"{c}"' for c in CLASSES), summary=summary,
                         guess=guess, task=task.strip()[:4000])


def parse_hint(text: str) -> LLMHint | None:
    """Lenient JSON extraction from the model's reply; None when nothing usable."""
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
    hint = LLMHint()
    k = str(obj.get("class") or "").strip().lower()
    hint.kind = k if k in CLASSES else None
    s = str(obj.get("scope") or "").strip().lower()
    hint.scope = s if s in SCOPES else None
    if isinstance(obj.get("vague"), bool):
        hint.vague = obj["vague"]
    files = obj.get("files")
    if isinstance(files, list):
        hint.files = [_rel(f) for f in files if isinstance(f, str) and f.strip()][:5]
    c = obj.get("confidence")
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        hint.confidence = max(0.0, min(1.0, float(c)))
    hint.reason = str(obj.get("reason") or "")[:200]
    if hint.kind is None and hint.scope is None and not hint.files:
        return None
    return hint


def _rel(p: str) -> str:
    p = p.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def assist(task: str, summary: str, guess: str, *, model: str = "haiku", effort: str = "low",
           timeout: float = 45, runner=None) -> tuple[LLMHint | None, str]:
    """(hint or None, status message). Never raises."""
    runner = runner or run_claude
    try:
        data = runner(build_prompt(task, summary, guess), model=model, effort=effort,
                      timeout=timeout)
    except LLMUnavailable as e:
        return None, f"LLM assist unavailable: {e}"
    except Exception as e:  # a broken runner must not break routing
        return None, f"LLM assist failed: {e}"
    hint = parse_hint(str(data.get("result") or ""))
    if hint is None:
        return None, "LLM assist returned no usable classification"
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        hint.cost_usd = float(cost)
    return hint, f"LLM assist ({model}/{effort})"


def graph_summary(root, graph, location, max_chars: int = 2500) -> str:
    """Compact, token-cheap project summary: languages, top directories, candidate files."""
    lines = []
    if graph is not None:
        try:
            st = graph.conn.execute(
                "SELECT lang, COUNT(*) FROM nodes WHERE kind='file' GROUP BY lang "
                "ORDER BY 2 DESC LIMIT 6").fetchall()
            lines.append("languages: " + ", ".join(f"{lang or '?'} {n}" for lang, n in st))
            dirs: dict[str, int] = {}
            for (p,) in graph.conn.execute("SELECT path FROM nodes WHERE kind='file'"):
                parts = p.split("/")
                key = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1
                                                                  else ".")
                dirs[key] = dirs.get(key, 0) + 1
            top = sorted(dirs.items(), key=lambda kv: -kv[1])[:15]
            lines.append("directories: " + ", ".join(f"{d} ({n})" for d, n in top))
        except Exception:
            pass
    else:
        lines.append("no code graph available")
    if location is not None and location.candidates:
        lines.append("candidate files (rule match confidence):")
        for c in location.candidates[:10]:
            lines.append(f"  {c.path} {c.confidence:.2f} ({'; '.join(c.reasons[:2])})")
    out = "\n".join(lines)
    return out if len(out) <= max_chars else out[: max_chars - 3] + "..."
