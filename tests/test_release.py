"""The npm launcher installs the PyPI release with the same version, so the two must match."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_npm_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    py = re.search(r'^version = "([^"]+)"', text, re.M).group(1)   # tomllib needs 3.11
    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))["version"]
    assert npm == py


def test_npm_launcher_is_not_mistaken_for_the_python_entry_point():
    # ccr.install.command._is_this_tool looks for these strings; if the launcher matched, `ccr init`
    # would write hooks that start Node on every prompt instead of calling Python directly.
    src = (ROOT / "npm" / "bin" / "ccr.js").read_bytes()
    assert b"ccr.entry" not in src and b"ccr.cli" not in src
