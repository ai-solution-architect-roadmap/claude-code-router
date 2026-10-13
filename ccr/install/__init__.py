"""`ccr init` / `ccr profile --apply`: plan every file change first, then either print the
exact unified diff (--dry-run, nothing written) or apply it with backups.

Files managed:
  .claude/settings.json        model/effortLevel (only when absent, or --force), hooks,
                               statusLine relay (wrapping an existing one)
  .claude/settings.local.json  only when needed: --local, an existing local statusLine, or a
                               user-level statusLine to wrap (personal, never committed)
  .claude/agents/*.md          ccr-quick/small/implement/architect/critical + Explore (haiku)
  .ccr/config.yaml             created once, never overwritten
  .ccr/history.jsonl           created empty
  .gitignore                   .ccr/graph.db*, .ccr/local/, .ccr/backups/
  .gitattributes               `.ccr/history.jsonl merge=union`: branches merge by
                               concatenation; duplicates are dropped when history is read
Backups of every modified file go to .ccr/backups/<timestamp>/<path>.
"""
from __future__ import annotations

import copy
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ccr.install import agents as A
from ccr.install import settings as S
from ccr.install.command import HookCommand, detect_command, is_ours, our_subcommand

GITIGNORE_LINES = (".ccr/graph.db*", ".ccr/local/", ".ccr/backups/")
GITIGNORE_HEADER = "# ccr (local caches, never commit)"
GITATTRIBUTES_LINE = ".ccr/history.jsonl merge=union"
MAX_BACKUP_SETS = 20


class InstallError(RuntimeError):
    pass


@dataclass
class Change:
    rel: str
    old: str | None          # None: file does not exist yet
    new: str
    note: str = ""

    @property
    def action(self) -> str:
        return "create" if self.old is None else "update"


@dataclass
class Plan:
    root: Path
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # what was decided and why
    warnings: list[str] = field(default_factory=list)
    command: HookCommand | None = None

    def add(self, rel: str, old: str | None, new: str, note: str = "") -> None:
        if old != new:
            self.changes.append(Change(rel, old, new, note))

    def diff(self) -> str:
        return "".join(unified_diff(c.rel, c.old, c.new) for c in self.changes)


# --------------------------------------------------------------------------- diff
def _lines(text: str) -> list[str]:
    out = text.splitlines(keepends=True)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n\\ No newline at end of file\n"
    return out


def unified_diff(rel: str, old: str | None, new: str) -> str:
    """git-style unified diff (applies with `git apply`)."""
    import difflib
    head = f"diff --git a/{rel} b/{rel}\n"
    if old is None:
        head += "new file mode 100644\n"
        if not new:
            return head
    a = _lines(old or "")
    b = _lines(new)
    body = list(difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else f"a/{rel}",
                                     tofile=f"b/{rel}", n=3))
    if not body:
        return ""
    return head + "".join(body)


# --------------------------------------------------------------------------- helpers
def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def config_template(cfg) -> str:
    t = cfg.tiers
    lines = ["# ccr configuration (commit this file). Keys left out use built-in defaults;",
             "# `ccr doctor` validates it and `ccr explain \"<task>\"` shows its effect.",
             "version: 1", "",
             "# Model + effort per tier. effort: low|medium|high|xhigh. Re-run `ccr init` after",
             "# changing these to regenerate the .claude/agents/ccr-* files.",
             "tiers:"]
    for name, tier in t.items():
        lines.append(f"  {name}: {{model: {tier.model}, effort: {tier.effort}, "
                     f"agent: {tier.agent}}}")
    b, h = cfg["budget"], cfg["hooks"]
    lines += ["", "budget:",
              f"  soft_limit_pct: {b['soft_limit_pct']}   # weekly usage % where tiers start "
              f"dropping one level", "",
              "hooks:",
              f"  enabled: {str(h['enabled']).lower()}          # master switch for ccr hooks",
              f"  route_prompts: {str(h['route_prompts']).lower()}    # inject the routing line "
              f"on each prompt (opt out per prompt with !noroute)",
              f"  model_advice: {str(h['model_advice']).lower()}     # suggest /model for "
              f"iterative work above the main model", "",
              "# Headless `ccr run`: success also requires this command to pass.",
              "run:",
              "  test_command: null      # e.g. \"pytest -q\" or \"npm test --silent\"",
              "  max_escalations: 1      # one tier up after a failed attempt", ""]
    return "\n".join(lines)


def _gitignore(root: Path, plan: Plan, extra: tuple[str, ...] = ()) -> None:
    p = root / ".gitignore"
    old = _read(p)
    have = {ln.strip().lstrip("/") for ln in (old or "").splitlines()}
    missing = [ln for ln in (*GITIGNORE_LINES, *extra) if ln.lstrip("/") not in have]
    if not missing:
        return
    new = old or ""
    if new and not new.endswith("\n"):
        new += "\n"
    if new.strip():
        new += "\n"
    if GITIGNORE_HEADER not in (old or ""):
        new += GITIGNORE_HEADER + "\n"
    new += "\n".join(missing) + "\n"
    plan.add(".gitignore", old, new, "ignore " + ", ".join(missing))


def _gitattributes(root: Path, plan: Plan) -> None:
    p = root / ".gitattributes"
    old = _read(p)
    if any(ln.split()[:1] == [".ccr/history.jsonl"] or ln.split()[:1] == ["/.ccr/history.jsonl"]
           for ln in (old or "").splitlines()):
        return                       # the user already decided how this file merges
    new = old or ""
    if new and not new.endswith("\n"):
        new += "\n"
    new += "# ccr: append-only routing history, merged by concatenation\n" + \
        GITATTRIBUTES_LINE + "\n"
    plan.add(".gitattributes", old, new, "history.jsonl merges by union")


# --------------------------------------------------------------------------- settings
def _load_settings(root: Path):
    proj_p = root / ".claude" / "settings.json"
    loc_p = root / ".claude" / "settings.local.json"
    proj, proj_text = S.load(proj_p)
    loc, loc_text = S.load(loc_p)
    return proj, proj_text, loc, loc_text


def _emit_settings(plan: Plan, rel: str, before: dict | None, text: str | None,
                   after: dict, note: str) -> bool:
    if (before or {}) == after and (before is not None or not after):
        return False
    plan.add(rel, text, S.dumps(after, S.indent_of(text)), note)
    return True


def _plan_settings(plan: Plan, root: Path, profile, cmd: HookCommand, *, force: bool,
                   local: bool, statusline: bool, environ: dict | None) -> bool:
    """Returns True when settings.local.json will be written."""
    proj, proj_text, loc, loc_text = _load_settings(root)
    new = {"proj": copy.deepcopy(proj or {}), "loc": copy.deepcopy(loc or {})}
    tgt, other = ("loc", "proj") if local else ("proj", "loc")
    fname = {"proj": ".claude/settings.json", "loc": ".claude/settings.local.json"}

    # model / effortLevel ---------------------------------------------------------------
    for key, val, why in (("model", profile.suggested_model, "main-session default"),
                          ("effortLevel", profile.suggested_effort, "main-session effort")):
        cur = new[tgt].get(key)
        if cur is None:
            new[tgt][key] = val
            plan.notes.append(f"{key}: {val} ({why} from the project profile)")
        elif cur != val and force:
            new[tgt][key] = val
            plan.notes.append(f"{key}: {cur} -> {val} (--force)")
        elif cur != val:
            plan.notes.append(f"{key}: kept your {cur!r} (profile suggests {val!r}; "
                              f"`ccr profile --apply` or `ccr init --force` changes it)")
        if tgt == "proj" and (loc or {}).get(key) not in (None, new["proj"].get(key)):
            plan.warnings.append(f"settings.local.json sets {key}={loc[key]!r}, which "
                                 f"overrides settings.json for you")

    # hooks --------------------------------------------------------------------------------
    for k in ("proj", "loc"):
        S._check_hooks(new[k].get("hooks"), fname[k])
    new[tgt]["hooks"] = S.merge_hooks(new[tgt].get("hooks") or {}, cmd)
    oh = new[other].get("hooks")
    if isinstance(oh, dict) and S.has_ours(oh):
        stripped = S.strip_ours(oh)
        if stripped:
            new[other]["hooks"] = stripped
        else:
            del new[other]["hooks"]
        plan.notes.append(f"moved ccr hooks out of {fname[other]}")
    plan.notes.append("hooks: SessionStart, UserPromptSubmit, SubagentStop, Stop -> "
                      f"{cmd.render('hook')} <name> (user hooks kept)")

    # statusLine ---------------------------------------------------------------------------
    if statusline:
        user = None
        try:
            user, _ = S.load(S.user_settings_path(environ))
        except S.SettingsError as e:
            plan.warnings.append(f"user settings unreadable, ignoring their statusLine: {e}")
        src = next((k for k, d in (("loc", loc), ("proj", proj), ("user", user))
                    if isinstance(d, dict) and "statusLine" in d), None)
        existing = {"loc": loc, "proj": proj, "user": user}[src]["statusLine"] if src else None
        if src == "user" and isinstance(existing, dict) and is_ours(existing.get("command")):
            plan.notes.append("statusLine: your user settings already run ccr statusline")
        else:
            where = "loc" if src in ("loc", "user") else tgt
            sl, note = S.statusline_for(existing, cmd)
            if sl is not None:
                new[where]["statusLine"] = sl
                if src == "user":
                    note += " (from your user settings; written to settings.local.json so " \
                            "the shared settings.json stays personal-command free)"
            plan.notes.append(f"statusLine ({fname[where]}): {note}")
    else:
        plan.notes.append("statusLine: skipped (--no-statusline)")

    _emit_settings(plan, fname["proj"], proj, proj_text, new["proj"], "settings")
    return _emit_settings(plan, fname["loc"], loc, loc_text, new["loc"], "local settings")


# --------------------------------------------------------------------------- agents
def _plan_agents(plan: Plan, root: Path, cfg, force: bool) -> None:
    adir = root / ".claude" / "agents"
    declared = A.names_in(adir)
    for spec in A.SPECS:
        path = adir / spec.file
        rel = f".claude/agents/{spec.file}"
        new = A.render(spec, cfg)
        elsewhere = [p for p in declared.get(spec.name.lower(), []) if not _same_file(p, path)]
        foreign = [p for p in elsewhere
                   if A.state(_read(p) or "") == "foreign"]
        if foreign:
            plan.notes.append(f"agent {spec.name}: kept your own definition in "
                              f"{foreign[0].relative_to(root)} (not generating {rel})")
            continue
        old = _read(path)
        if old is None:
            plan.add(rel, None, new, f"agent {spec.name}")
            continue
        if old == new:
            continue
        st = A.state(old)
        if st == "clean" or force:
            plan.add(rel, old, new, f"agent {spec.name}" + (" (--force)" if st != "clean"
                                                           else ""))
        else:
            what = "edited" if st == "edited" else "hand-written"
            plan.notes.append(f"agent {spec.name}: kept your {what} {rel} (--force "
                              f"regenerates it, with a backup)")


# --------------------------------------------------------------------------- public API
def plan_init(root: str | Path, *, cfg=None, profile=None, force: bool = False,
              local: bool = False, statusline: bool = True, command: str | None = None,
              environ: dict | None = None, hook_command: HookCommand | None = None) -> Plan:
    from ccr.config import config_path, load_config
    root = Path(root).resolve()
    if profile is None:
        from ccr.detect import detect
        profile = detect(root)
    cfg = cfg or load_config(root)
    cmd = hook_command or detect_command(command, environ)
    plan = Plan(root, command=cmd)
    if not cmd.portable:
        plan.warnings.append(
            f"`ccr` is not on PATH, so hooks call {cmd.render()} (an absolute, machine-"
            f"specific path). Install ccr on PATH (e.g. `uv tool install .`) and rerun "
            f"`ccr init`, or use --local to keep these commands out of the shared "
            f"settings.json.")
    wrote_local = _plan_settings(plan, root, profile, cmd, force=force, local=local,
                                 statusline=statusline, environ=environ)
    _plan_agents(plan, root, cfg, force)

    cp = config_path(root)
    if not cp.exists():
        plan.add(".ccr/config.yaml", None, config_template(cfg), "config")
    elif cfg.errors:
        plan.warnings += [f"config: {e}" for e in cfg.errors]
    hp = root / ".ccr" / "history.jsonl"
    if not hp.exists():
        plan.add(".ccr/history.jsonl", None, "", "routing history")
    extra = (".claude/settings.local.json",) if wrote_local else ()
    _gitignore(root, plan, extra)
    _gitattributes(root, plan)
    if os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"):
        plan.warnings.append("CLAUDE_CODE_EFFORT_LEVEL is set: it overrides every subagent's "
                             "effort. Unset it for tier routing to work.")
    return plan


def plan_profile(root: str | Path, profile, *, local: bool = False) -> Plan:
    """Set model/effortLevel from the profile explicitly (overrides existing values)."""
    root = Path(root).resolve()
    plan = Plan(root)
    rel = ".claude/settings.local.json" if local else ".claude/settings.json"
    data, text = S.load(root / rel)
    new = copy.deepcopy(data or {})
    for key, val in (("model", profile.suggested_model), ("effortLevel",
                                                          profile.suggested_effort)):
        if new.get(key) != val:
            plan.notes.append(f"{key}: {new.get(key)!r} -> {val!r}")
        new[key] = val
    _emit_settings(plan, rel, data, text, new, "profile defaults")
    return plan


def apply(plan: Plan, stamp: str | None = None) -> Path | None:
    """Write the plan. Backs up each existing file first; refuses files that changed since
    planning. Returns the backup directory (None when nothing needed a backup)."""
    root = plan.root
    for c in plan.changes:
        if _read(root / c.rel) != c.old:
            raise InstallError(f"{c.rel} changed since the plan was made; rerun")
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    bdir = root / ".ccr" / "backups" / stamp
    n = 1
    while bdir.exists():
        bdir = root / ".ccr" / "backups" / f"{stamp}-{n}"
        n += 1
    used = False
    for c in plan.changes:
        p = root / c.rel
        if c.old is not None:
            dst = bdir / c.rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            used = True
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".ccrtmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(c.new)
        os.replace(tmp, p)
    if used:
        _prune_backups(root / ".ccr" / "backups")
    return bdir if used else None


def _prune_backups(base: Path) -> None:
    try:
        sets = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return
    for p in sets[:-MAX_BACKUP_SETS]:
        shutil.rmtree(p, ignore_errors=True)


__all__ = ["Change", "InstallError", "Plan", "apply", "plan_init", "plan_profile",
           "unified_diff", "our_subcommand"]
