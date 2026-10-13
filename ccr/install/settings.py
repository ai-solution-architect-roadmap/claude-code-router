"""Merge-safe edits of Claude Code settings files.

Only ccr's own pieces are touched:
  * hook entries whose command runs `ccr hook ...` (any path / interpreter prefix) are
    replaced in place, so re-init never duplicates them and user hooks survive untouched;
  * `statusLine`: ours is refreshed; someone else's is wrapped (`ccr statusline --wrap
    "<their command>"`) instead of being replaced;
  * `model` / `effortLevel` are only added when absent (or with force).
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from ccr.install.command import HookCommand, is_ours, our_subcommand, split

# event -> (ccr hook name, timeout seconds). Claude Code timeouts are in seconds.
HOOKS = {
    "SessionStart": ("session-start", 5),
    "UserPromptSubmit": ("prompt", 5),
    "SubagentStop": ("stop", 5),
    "Stop": ("stop", 5),
}
STATUSLINE_KEYS_KEPT = ("padding", "refreshInterval")


class SettingsError(ValueError):
    pass


def load(path: Path) -> tuple[dict | None, str | None]:
    """(parsed object or None when missing, raw text). Raises SettingsError when invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        raise SettingsError(f"{path}: cannot read ({e})") from e
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SettingsError(f"{path}: not valid JSON ({e}); fix or move it aside, then rerun"
                            ) from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: top level must be a JSON object")
    return data, text


def indent_of(text: str | None) -> int | str:
    if text:
        m = re.search(r"^([ \t]+)\"", text, re.M)
        if m:
            ws = m.group(1)
            return "\t" if ws.startswith("\t") else len(ws)
    return 2


def dumps(data: dict, indent: int | str = 2) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


def user_settings_path(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("CLAUDE_CONFIG_DIR") or str(Path(env.get("HOME") or Path.home()) / ".claude")
    return Path(base) / "settings.json"


# --------------------------------------------------------------------------- hooks
def hook_entry(cmd: HookCommand, name: str, timeout: int) -> dict:
    e = {"type": "command", "command": cmd.render("hook", name), "timeout": timeout}
    if cmd.shell_field:
        e["shell"] = cmd.shell_field
    return e


def _check_hooks(hooks: object, where: str) -> dict:
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise SettingsError(f"{where}: `hooks` must be an object")
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            raise SettingsError(f"{where}: hooks.{ev} must be a list")
    return hooks


def _is_our_hook(h: object) -> bool:
    return isinstance(h, dict) and our_subcommand(str(h.get("command", ""))) == "hook"


def _strip_group(g: object):
    """Group without ccr entries; None when only ccr entries were in it."""
    if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
        return g
    kept = [h for h in g["hooks"] if not _is_our_hook(h)]
    if not kept and g["hooks"]:
        return None
    return {**g, "hooks": kept} if len(kept) != len(g["hooks"]) else g


def strip_ours(hooks: dict) -> dict:
    """Copy of `hooks` without ccr hook entries (emptied groups / events removed)."""
    out = {}
    for ev, groups in hooks.items():
        kept = [s for s in (_strip_group(g) for g in groups) if s is not None]
        if kept or not groups:
            out[ev] = kept
    return copy.deepcopy(out)


def has_ours(hooks: dict) -> bool:
    return strip_ours(hooks) != hooks


def merge_hooks(hooks: dict, cmd: HookCommand) -> dict:
    """Install ccr's hooks. The first matcher-less group holding only a ccr hook is updated
    in place (stable order, so re-init is a no-op); other ccr entries are removed; user
    groups and entries are kept as they are."""
    out = copy.deepcopy(hooks)
    for ev, (name, timeout) in HOOKS.items():
        want = hook_entry(cmd, name, timeout)
        new_groups, placed = [], False
        for g in out.get(ev, []):
            pure = (isinstance(g, dict) and isinstance(g.get("hooks"), list)
                    and len(g["hooks"]) == 1 and _is_our_hook(g["hooks"][0])
                    and not g.get("matcher"))
            if pure and not placed:
                new_groups.append({**g, "hooks": [want]})
                placed = True
                continue
            s = _strip_group(g)
            if s is not None:
                new_groups.append(s)
        if not placed:
            new_groups.append({"hooks": [want]})
        out[ev] = new_groups
    return out


# --------------------------------------------------------------------------- statusLine
def wrapped_command(sl: dict, flavor: str) -> str | None:
    """The user command inside our `ccr statusline --wrap <cmd>`, if any."""
    try:
        parts = split(str(sl.get("command", "")), flavor)
    except ValueError:
        return None
    if "--wrap" in parts:
        i = parts.index("--wrap")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def statusline_for(existing: object, cmd: HookCommand) -> tuple[dict | None, str]:
    """(new statusLine value or None to leave as is, note)."""
    if existing is None:
        return {"type": "command", "command": cmd.render("statusline")}, "status line relay"
    if not isinstance(existing, dict) or not isinstance(existing.get("command"), str) or \
            not existing["command"].strip():
        return None, "statusLine left unchanged (not a command status line)"
    keep = {k: existing[k] for k in STATUSLINE_KEYS_KEPT if k in existing}
    if is_ours(existing["command"]) and our_subcommand(existing["command"]) == "statusline":
        inner = wrapped_command(existing, cmd.flavor)
        args = ["statusline"] + (["--wrap", inner] if inner else [])
        new = {**existing, "type": "command", "command": cmd.render(*args)}
        return new, ("status line relay (still wrapping your command)" if inner
                     else "status line relay")
    new = {"type": "command", "command": cmd.render("statusline", "--wrap", existing["command"]),
           **keep}
    return new, "wrapped your existing status line (its output is shown unchanged)"
