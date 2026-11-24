# claude-code-tier-router (npm)

npm launcher for [claude-code-router](https://github.com/ai-solution-architect-roadmap/claude-code-router),
which routes Claude Code tasks to the cheapest sufficient model and effort. It installs the `ccr`
command.

```sh
npm install -g claude-code-tier-router
ccr --help
```

ccr is written in Python. On first run this launcher creates a private environment in
`~/.ccr-npm` and installs the matching `claude-code-router` release from PyPI into it, using
[uv](https://docs.astral.sh/uv/) if it is installed, otherwise Python 3.10+ and pip. Later runs
reuse it. The Claude Code hooks that `ccr init` writes call that Python directly, so hooks never
start Node.

Settings: `CCR_NPM_HOME` moves the environment; `CCR_PYTHON` picks the Python used when uv is
not installed.

If you already use Python tooling, installing the Python package directly is simpler:
`uv tool install claude-code-router`.

> Name clash: the unrelated npm package *claude-code-router* also installs a `ccr` command.
> Keep only one of them installed globally. `ccr doctor` checks which one is on your PATH.

Full documentation: https://github.com/ai-solution-architect-roadmap/claude-code-router
