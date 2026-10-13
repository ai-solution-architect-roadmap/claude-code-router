"""Benchmark graph build: python scripts/bench_graph.py [N_FILES] [--git]"""
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ccr.graph import update

PY = '''from pkg{a}.m{b} import f{b}
import os


class C{i}:
    def method(self, x):
        for i in range(x):
            if i % 2 and x > 3:
                f{b}(i)
            elif i > 10:
                return self.other(i)
        return 0

    def other(self, y):
        return y


def f{i}(x):
    try:
        return C{i}().method(x)
    except ValueError:
        return f{b}(x)
'''
TS = '''import {{ g{b} }} from '../ts{a}/t{b}';

export class K{i} {{
  run(n: number): number {{
    if (n > 1 && n < 9) {{ return g{b}(n); }}
    for (const v of [1, 2]) {{ this.run(v); }}
    return 0;
  }}
}}

export function g{i}(n: number): number {{ return n > 0 ? new K{i}().run(n) : 0; }}
'''


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10000
    use_git = "--git" in sys.argv
    random.seed(1)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        root = Path(d)
        for i in range(n):
            b = random.randrange(n)
            if i % 4 == 3:
                p = root / f"ts{i % 50}" / f"t{i}.ts"
                txt = TS.format(i=i, a=b % 50, b=b)
            else:
                p = root / f"pkg{i % 50}" / f"m{i}.py"
                txt = PY.format(i=i, a=b % 50, b=b)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt)
        for k in range(50):
            (root / f"pkg{k}" / "__init__.py").write_text("")
        if use_git:
            env = {**os.environ, "GIT_AUTHOR_NAME": "b", "GIT_AUTHOR_EMAIL": "b@x",
                   "GIT_COMMITTER_NAME": "b", "GIT_COMMITTER_EMAIL": "b@x"}
            for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
                subprocess.run(["git", "-C", d, *cmd], check=True, env=env)
        t = time.perf_counter()
        r = update(root)
        print(f"first build: {r.parsed} files in {time.perf_counter() - t:.2f}s "
              f"(mode {r.mode}, parsers {r.parsers})")
        t = time.perf_counter()
        r = update(root)
        print(f"no-op update: parsed {r.parsed} in {time.perf_counter() - t:.2f}s")
        target = root / "pkg8" / "m8.py"
        target.write_text(target.read_text() + "\n\ndef extra():\n    return f8(1)\n")
        t = time.perf_counter()
        r = update(root)
        print(f"one-file update: parsed {r.parsed} in {time.perf_counter() - t:.2f}s")
        db = root / ".ccr" / "graph.db"
        print(f"db size: {db.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
