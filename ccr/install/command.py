"""How Claude Code should invoke ccr from hooks and the status line.

Preference order:
  1. a `ccr` on PATH that is *this* tool (not e.g. the npm "claude-code-router", which also
     installs a `ccr`) and is not only on PATH because a virtualenv is active -> bare `ccr`,
     portable across machines;
  2. otherwise `<absolute interpreter> -m ccr` (machine-specific; init warns).

Quoting: Claude Code runs commands with `sh -c` on macOS/Linux and with Git Bash on Windows
(PowerShell when Git Bash is missing; hooks accept an explicit `"shell": "powershell"`).
Arguments made only of safe characters are left bare, which reads the same in all three
shells; anything else is single-quoted in the target shell's syntax.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
# Our hook / status line commands: `ccr hook x`, `/p/ccr hook x`, `py -m ccr hook x`,
# `'C:/p/ccr.exe' statusline ...`, `& 'p/ccr' statusline`.
_OURS = re.compile(r"""(?:^|[\s/\\'"&])ccr(?:\.exe)?['"]?\s+(hook|statusline)(?:\s|$)""")
IS_WINDOWS = os.name == "nt"


@dataclass
class HookCommand:
    prefix: list[str]          # argv prefix, e.g. ["ccr"] or [python, "-m", "ccr"]
    flavor: str = "posix"      # posix (sh / Git Bash) | powershell
    portable: bool = True      # False when the command embeds a machine-specific path
    source: str = "path"       # path | interpreter | override

    def render(self, *args: str) -> str:
        return render([*self.prefix, *args], self.flavor)

    @property
    def shell_field(self) -> str | None:
        """Value for a hook entry's `shell` field (only needed for PowerShell)."""
        return "powershell" if self.flavor == "powershell" else None


def quote(arg: str, flavor: str = "posix") -> str:
    if arg and _SAFE.match(arg):
        return arg
    if flavor == "powershell":
        return "'" + arg.replace("'", "''") + "'"
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def render(parts: list[str], flavor: str = "posix") -> str:
    q = [quote(p, flavor) for p in parts]
    if flavor == "powershell" and q and q[0][:1] == "'":
        return "& " + " ".join(q)     # PowerShell needs the call operator for a quoted path
    return " ".join(q)


def split(cmd: str, flavor: str = "posix") -> list[str]:
    """Inverse of `render` (enough for commands ccr wrote itself)."""
    if flavor == "powershell":
        s = cmd.strip()
        if s.startswith("& "):
            s = s[2:]
        out = []
        for m in re.finditer(r"'((?:[^']|'')*)'|(\S+)", s):
            out.append(m.group(1).replace("''", "'") if m.group(1) is not None else m.group(2))
        return out
    import shlex
    return shlex.split(cmd, posix=True)


def is_ours(cmd: object) -> bool:
    return isinstance(cmd, str) and bool(_OURS.search(cmd))


def our_subcommand(cmd: str) -> str | None:
    m = _OURS.search(cmd)
    return m.group(1) if m else None


def _norm(p: str) -> str:
    return p.replace("\\", "/") if IS_WINDOWS else p


def git_bash() -> str | None:
    if not IS_WINDOWS:
        return None
    found = shutil.which("bash")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        cand = Path(base) / "Git" / "bin" / "bash.exe"
        if cand.exists():
            return str(cand)
    return None


def detect_flavor() -> str:
    if not IS_WINDOWS:
        return "posix"
    return "posix" if git_bash() else "powershell"


def _venv_scripts_dir() -> Path | None:
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return None
    return Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin")


def _is_this_tool(exe: str) -> bool:
    try:
        real = Path(os.path.realpath(exe))
        prefix = Path(os.path.realpath(sys.prefix))
        if prefix in real.parents:
            return True
        with open(real, "rb") as f:
            head = f.read(256 * 1024)
        return b"ccr.entry" in head or b"ccr.cli" in head
    except OSError:
        return False


def find_on_path(environ: dict | None = None) -> str | None:
    """A `ccr` on PATH that is this tool, ignoring the active virtualenv's scripts dir."""
    env = os.environ if environ is None else environ
    skip = _venv_scripts_dir()
    dirs = []
    for d in env.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            if skip is not None and Path(d).resolve() == skip.resolve():
                continue
        except OSError:
            pass
        dirs.append(d)
    for d in dirs:   # the first `ccr` on PATH wins in a shell, so only accept it if ours
        found = shutil.which("ccr", path=d)
        if found:
            return found if _is_this_tool(found) else None
    return None


def detect_command(override: str | None = None, environ: dict | None = None,
                   flavor: str | None = None) -> HookCommand:
    flavor = flavor or detect_flavor()
    if override:
        return HookCommand(split(override, "posix") if flavor == "posix" else
                           split(override, "powershell"), flavor, portable=True,
                           source="override")
    if find_on_path(environ):
        return HookCommand(["ccr"], flavor, portable=True, source="path")
    # Keep sys.executable unresolved: resolving a venv's python symlink loses the venv.
    return HookCommand([_norm(sys.executable), "-m", "ccr"], flavor, portable=False,
                       source="interpreter")
