#!/usr/bin/env node
// npm launcher for the claude-code-router Python package.
//
// On first run it creates a private Python environment and installs the matching
// claude-code-router release from PyPI into it, then runs `python -m ccr` there. Later runs
// reuse that environment. Because `ccr init` is then run by that Python, the Claude Code
// hooks it writes call that Python directly and never start Node, which keeps hooks fast.
//
// Environment variables:
//   CCR_NPM_HOME  where the environment lives (default: ~/.ccr-npm)
//   CCR_PYTHON    Python 3.10+ to build it with when uv is not installed
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PKG = require("../package.json");
const PYPI_NAME = "claude-code-router";
const VERSION = PKG.version;
const IS_WIN = process.platform === "win32";

const home = process.env.CCR_NPM_HOME || path.join(os.homedir(), ".ccr-npm");
const venv = path.join(home, "venv");
const venvPython = path.join(venv, IS_WIN ? "Scripts" : "bin", IS_WIN ? "python.exe" : "python");
const marker = path.join(home, "installed-version");

function log(msg) {
  process.stderr.write(`ccr (npm): ${msg}\n`);
}

function run(cmd, args, quiet) {
  const r = spawnSync(cmd, args, { stdio: quiet ? "ignore" : ["ignore", "inherit", "inherit"] });
  return !r.error && r.status === 0;
}

function has(cmd, args) {
  return run(cmd, args || ["--version"], true);
}

function findPython() {
  const candidates = [process.env.CCR_PYTHON, IS_WIN ? "py" : null, "python3", "python"].filter(Boolean);
  const check = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"];
  for (const c of candidates) {
    const cmd = c === "py" ? ["py", "-3"] : [c];
    if (has(cmd[0], cmd.slice(1).concat(check))) {
      return cmd;
    }
  }
  return null;
}

function installedVersion() {
  try {
    return fs.existsSync(venvPython) ? fs.readFileSync(marker, "utf8").trim() : null;
  } catch (_) {
    return null;
  }
}

function install() {
  fs.mkdirSync(home, { recursive: true });
  const spec = `${PYPI_NAME}==${VERSION}`;
  log(`installing ${spec} into ${venv} (one time)`);
  let ok;
  if (has("uv")) {
    ok = (fs.existsSync(venvPython) || run("uv", ["venv", "--quiet", "--python", ">=3.10", venv])) &&
      run("uv", ["pip", "install", "--quiet", "--python", venvPython, spec]);
  } else {
    const py = findPython();
    if (!py) {
      log("needs Python 3.10+ (or uv). Install one, or set CCR_PYTHON to a Python 3.10+ path.");
      return false;
    }
    ok = (fs.existsSync(venvPython) || run(py[0], py.slice(1).concat(["-m", "venv", venv]))) &&
      run(venvPython, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", spec]);
  }
  if (!ok) {
    log(`install failed. You can install the Python package directly: uv tool install ${PYPI_NAME}`);
    return false;
  }
  fs.writeFileSync(marker, VERSION + "\n");
  return true;
}

function main() {
  if (installedVersion() !== VERSION && !install()) {
    process.exit(1);
  }
  const r = spawnSync(venvPython, ["-m", "ccr"].concat(process.argv.slice(2)), { stdio: "inherit" });
  if (r.error) {
    log(`could not start ${venvPython}: ${r.error.message}`);
    process.exit(1);
  }
  process.exit(r.status === null ? 1 : r.status);
}

main();
