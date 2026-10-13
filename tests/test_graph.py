import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ccr.cli import app
from ccr.graph import Graph, freshness, update
from ccr.graph import build as gbuild
from ccr.graph import parse as gparse
from ccr.graph.files import is_test_path, walk_files

FIX = Path(__file__).parent / "fixtures" / "graph"
runner = CliRunner()


def project(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copytree(FIX / name, dst)
    return dst


def git(root: Path, *args: str, author: str = "A") -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author}@x",
           "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": f"{author}@x"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def git_init(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def bump(root: Path, rel: str, text: str) -> None:
    """Write and guarantee a different mtime (coarse filesystem clocks)."""
    p = root / rel
    old = p.stat().st_mtime_ns if p.exists() else 0
    p.write_text(text)
    if p.stat().st_mtime_ns == old:
        os.utime(p, ns=(old + 1_000_000, old + 1_000_000))


def has_grammar(lang: str) -> bool:
    try:
        return gparse._ts_for(lang) is not None
    except Exception:
        return False


# ----------------------------------------------------------------------------- python
@pytest.fixture
def py(tmp_path):
    root = project(tmp_path, "py")
    write(root, "generated/out.py", "def gen():\n    pass\n")
    write(root, "app/schema.gen.py", "def g2():\n    pass\n")
    return root


def test_python_symbols_imports_calls(py):
    r = update(py, workers=1)
    assert r.full and r.mode == "fs"
    g = Graph.open(py)
    files = {f for (f,) in g.conn.execute("SELECT path FROM nodes WHERE kind='file'")}
    # .gitignore'd, vendored and lock files are skipped
    assert "generated/out.py" not in files and "app/schema.gen.py" not in files
    assert not any(f.startswith("node_modules") for f in files)
    assert "app/core/engine.py" in files and "tests/test_engine.py" in files

    (run,) = g.lookup("Engine.run")
    assert run.kind == "method" and run.parent == "Engine" and run.path == "app/core/engine.py"
    assert run.branches == 4 and run.nesting == 3
    assert [s.kind for s in g.lookup("engine")] == ["class"]  # case-insensitive fallback
    assert {s.name for s in g.lookup("fin")} == {"finish"}    # prefix fallback

    assert g.dependencies("app/main.py") == ["app/__init__.py", "app/core/__init__.py",
                                             "app/util.py"]
    assert g.dependencies("app/core/__init__.py") == ["app/core/engine.py"]
    (clamp,) = g.lookup("clamp")
    callers = {c[0] for c in g.callers(clamp.id)}
    assert callers == {"run", "main"}

    deps = {d.path: d for d in g.dependents("app/util.py")}
    assert deps["app/core/engine.py"].depth == 1
    assert deps["app/main.py"].depth == 1
    assert "tests/test_engine.py" in deps  # transitive via engine.py
    tests = g.tests_for("app/core/engine.py")
    assert tests[0].path == "tests/test_engine.py" and tests[0].reason == "imports"
    names = g.tests_for("app/util.py")
    assert any(t.path == "tests/test_util.py" and t.reason == "name" for t in names)
    prox = g.test_proximity("app/core/engine.py")
    assert prox["has_tests"] and prox["direct"]

    cx = g.complexity("app/core/engine.py")
    assert cx["symbols"] == 3 and cx["nesting"] == 3 and cx["hotspots"][0]["name"] == "run"
    assert g.churn("app/util.py") is None  # not a git repo
    st = g.stats()
    assert st["parsers"] == {"tree-sitter": 7} and st["tests"] == 2
    g.close()


def test_typescript(tmp_path):
    if not has_grammar("typescript"):
        pytest.skip("typescript grammar unavailable offline")
    root = project(tmp_path, "ts")
    update(root, workers=1)
    with Graph.open(root) as g:
        files = {f for (f,) in g.conn.execute("SELECT path FROM nodes WHERE kind='file'")}
        assert "src/app.min.js" not in files
        assert g.dependencies("src/components/Button.tsx") == ["src/format.ts", "src/lib/index.ts"]
        assert g.dependencies("src/lib/index.ts") == ["src/lib/math.ts"]
        (render,) = g.lookup("Button.render")
        assert render.kind == "method"
        callees = {c[0] for c in g.callees(render.id)}
        assert callees == {"add", "format"}   # add resolved through the lib/index.ts barrel
        (add,) = g.lookup("add")
        assert add.branches == 2 and add.nesting == 1  # if + &&
        blast = {d.path for d in g.dependents("src/lib/math.ts")}
        assert {"src/lib/index.ts", "src/components/Button.tsx", "src/app.ts"} <= blast


def test_go(tmp_path):
    if not has_grammar("go"):
        pytest.skip("go grammar unavailable offline")
    root = project(tmp_path, "go")
    update(root, workers=1)
    with Graph.open(root) as g:
        assert g.dependencies("main.go") == ["internal/store/store.go"]
        (helper,) = g.lookup("helper")
        assert {c[1] for c in g.callers(helper.id)} == {"main.go"}  # same-package call
        (get,) = g.lookup("Get")
        assert get.kind == "method" and get.branches >= 1
        assert [t.path for t in g.tests_for("internal/store/store.go")] == \
            ["internal/store/store_test.go"]


def test_java(tmp_path):
    if not has_grammar("java"):
        pytest.skip("java grammar unavailable offline")
    root = project(tmp_path, "java")
    update(root, workers=1)
    base = "src/main/java/com/acme/"
    with Graph.open(root) as g:
        assert g.dependencies(base + "app/App.java") == [base + "util/Strings.java"]
        (assist,) = g.lookup("assist")
        assert {c[1] for c in g.callers(assist.id)} == {base + "app/App.java"}  # same package
        (trim,) = g.lookup("Strings.trim")
        assert trim.branches == 1  # ternary


def test_rust(tmp_path):
    if not has_grammar("rust"):
        pytest.skip("rust grammar unavailable offline")
    root = project(tmp_path, "rust")
    update(root, workers=1)
    with Graph.open(root) as g:
        assert g.dependencies("src/main.rs") == ["src/net/client.rs", "src/net/mod.rs"]
        assert g.dependencies("src/net/mod.rs") == ["src/net/client.rs"]


def test_unsupported_language_regex_fallback(tmp_path, monkeypatch):
    root = project(tmp_path, "zig")
    # simulate "no grammar available" (offline / unsupported) for every language
    monkeypatch.setattr(gbuild, "_ts_languages",
                        lambda rels, offline: ({r: None for r in rels}, frozenset()))
    r = update(root, workers=1)
    assert r.parsers == {"regex": 2}
    with Graph.open(root) as g:
        assert g.dependencies("src/main.zig") == ["src/util.zig"]
        kinds = {s.name: s.kind for s in g.lookup("o") + g.lookup("Point") + g.lookup("len")}
        assert kinds["ok"] == "function" and kinds["Point"] == "class"
        assert kinds["len"] == "method"
        cx = g.complexity("src/main.zig")
        assert cx["branches"] >= 1 and cx["loc"] == 7


def test_zig_default_path_never_crashes(tmp_path):
    root = project(tmp_path, "zig")
    r = update(root, workers=1)
    assert r.parsed == 2 and set(r.parsers) <= {"tree-sitter", "regex"}


def test_bad_inputs_never_crash(tmp_path, monkeypatch):
    write(tmp_path, "a.py", "def ok():\n    pass\n")
    (tmp_path / "bin.py").write_bytes(b"\x00\x01\x02" * 100)
    (tmp_path / "bad.py").write_bytes(b"def \xff\xfe(:\n  ((((\n")
    write(tmp_path, "big.py", "x = 1\n" * 200_000)
    write(tmp_path, "weird.zig", "}}}{{{ @import(\n")
    r = update(tmp_path, workers=1)
    assert r.scanned == 5
    with Graph.open(tmp_path) as g:
        parsers = dict(g.conn.execute("SELECT path, parser FROM nodes WHERE kind='file'"))
        assert parsers["big.py"] == "size-only" and parsers["bin.py"] == "none"

    # tree-sitter blowing up mid-parse falls back to regex
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(gparse, "ts_extract", boom)
    r = update(tmp_path, rebuild=True, workers=1)
    with Graph.open(tmp_path) as g:
        assert g.lookup("ok")[0].kind == "function"
        assert g.file_row("a.py")["parser"] == "regex"


# ----------------------------------------------------------------------------- incremental
def test_incremental_one_file_change_fs(py):
    first = update(py, workers=1)
    assert first.parsed == 7
    again = update(py, workers=1)
    assert (again.parsed, again.checked) == (0, 0)

    bump(py, "app/util.py", "def clamp(x, lo=0, hi=100):\n    return x\n\n\ndef extra():\n"
                            "    return clamp(1)\n")
    r = update(py, workers=1)
    assert r.parsed == 1 and r.parsed_files == ["app/util.py"]
    with Graph.open(py) as g:
        assert {s.name for s in g.lookup("extra")} == {"extra"}
        assert not g.lookup("unused")
        (clamp,) = g.lookup("clamp")
        assert {c[0] for c in g.callers(clamp.id)} == {"run", "main", "extra"}

    # touched but identical content: hashed, not re-parsed
    p = py / "app" / "util.py"
    os.utime(p, ns=(p.stat().st_mtime_ns + 5_000_000,) * 2)
    r = update(py, workers=1)
    assert (r.parsed, r.unchanged) == (0, 1)


def test_incremental_add_remove_and_rename(py):
    update(py, workers=1)
    write(py, "app/extra.py", "from app.util import clamp\n\ndef go():\n    return clamp(3)\n")
    r = update(py, workers=1)
    assert (r.parsed, r.added) == (1, 1)
    with Graph.open(py) as g:
        assert "app/extra.py" in {d.path for d in g.dependents("app/util.py")}

    # rename clamp -> clip: callers in untouched files must lose their edges
    bump(py, "app/util.py", "def clip(x):\n    return x\n")
    update(py, workers=1)
    with Graph.open(py) as g:
        assert not g.lookup("clamp")
        (clip,) = g.lookup("clip")
        assert g.callers(clip.id) == []

    # a file appearing makes a previously unresolved import resolve
    write(py, "app/late.py", "def late():\n    pass\n")
    write(py, "app/uses_late.py", "from app.late import late\n")
    update(py, workers=1)
    (py / "app" / "late.py").unlink()
    r = update(py, workers=1)
    assert (r.removed, r.parsed) == (1, 0)
    with Graph.open(py) as g:
        assert g.dependencies("app/uses_late.py") == []
    write(py, "app/late.py", "def late():\n    pass\n")
    update(py, workers=1)
    with Graph.open(py) as g:
        assert g.dependencies("app/uses_late.py") == ["app/late.py"]


def test_incremental_git_mode_and_churn(py):
    git_init(py)
    r = update(py, workers=1)
    assert r.mode == "git" and r.churn_updated and r.commit
    files = {f for (f,) in Graph.open(py).conn.execute("SELECT path FROM nodes WHERE kind='file'")}
    assert "generated/out.py" not in files  # git's own ignore handling

    bump(py, "app/main.py", "from app.core import Engine\n\ndef main():\n    return Engine()\n")
    git(py, "commit", "-qam", "change main")
    bump(py, "app/main.py", "def main():\n    return 2\n")
    git(py, "commit", "-qam", "again", author="B")
    r = update(py, workers=1)
    assert r.parsed_files == ["app/main.py"] and r.churn_updated
    with Graph.open(py) as g:
        ch = g.churn("app/main.py")
        assert ch["commits"] == 3 and ch["authors"] == 2
        assert g.churn("app/util.py")["commits"] == 1

    # working-tree-only change (uncommitted) is picked up too
    bump(py, "app/util.py", "def clamp(x):\n    return x\n")
    r = update(py, workers=1)
    assert r.parsed_files == ["app/util.py"] and not r.churn_updated
    assert freshness(py)["status"] == "fresh"


def test_parallel_matches_sequential(py, monkeypatch):
    update(py, workers=1)
    with Graph.open(py) as g:
        seq = g.stats()
    monkeypatch.setattr(gbuild, "PARALLEL_MIN", 2)
    update(py, rebuild=True, workers=2)
    with Graph.open(py) as g:
        par = g.stats()
    for k in ("files", "symbols", "edges", "parsers"):
        assert seq[k] == par[k]


def test_incremental_perf_small_project(tmp_path):
    for i in range(300):
        write(tmp_path, f"pkg/m{i}.py",
              f"from pkg.m{max(0, i - 1)} import f{max(0, i - 1)}\n\n"
              f"def f{i}(x):\n    if x:\n        return f{max(0, i - 1)}(x - 1)\n    return 0\n")
    update(tmp_path)
    bump(tmp_path, "pkg/m150.py", "def f150(x):\n    return 1\n")
    t0 = time.perf_counter()
    r = update(tmp_path)
    assert r.parsed == 1
    assert time.perf_counter() - t0 < 2.0


# ----------------------------------------------------------------------------- misc
def test_freshness_states(py):
    assert freshness(py)["status"] == "missing"
    update(py, workers=1)
    assert freshness(py)["status"] == "fresh"
    bump(py, "app/util.py", "x = 2\n")
    f = freshness(py)
    assert f["status"] == "stale" and "1 indexed file" in f["reasons"][0]


def test_gitignore_matcher(tmp_path):
    write(tmp_path, ".gitignore", "*.log\n/gen/\ndocs/**/*.tmp\n!keep.log\nsecret\n")
    write(tmp_path, "a.log", "")
    write(tmp_path, "keep.log", "")
    write(tmp_path, "gen/x.py", "")
    write(tmp_path, "src/gen/y.py", "")      # /gen/ is anchored to the root
    write(tmp_path, "build/z.py", "")        # build dirs are always skipped
    write(tmp_path, "docs/a/b/c.tmp", "")
    write(tmp_path, "src/secret/z.py", "")
    write(tmp_path, "src/sub/.gitignore", "local_*.py\n")
    write(tmp_path, "src/sub/local_x.py", "")
    write(tmp_path, "src/sub/ok.py", "")
    files = set(walk_files(tmp_path))
    assert files == {".gitignore", "keep.log", "src/gen/y.py", "src/sub/.gitignore",
                     "src/sub/ok.py"}


def test_is_test_path():
    for p in ["tests/a.py", "pkg/test_a.py", "a_test.go", "src/a.spec.ts", "A.test.js",
              "src/test/java/FooTest.java", "spec/foo_spec.rb", "src/__tests__/x.js"]:
        assert is_test_path(p), p
    for p in ["src/contest.py", "latest.py", "src/attestation.go", "manifest.json"]:
        assert not is_test_path(p), p


def test_cli_graph(py):
    r = runner.invoke(app, ["graph", "--stats", "--path", str(py)])
    assert r.exit_code == 1 and "not built" in r.output
    r = runner.invoke(app, ["graph", "--path", str(py)])
    assert r.exit_code == 0 and "graph built (fs): 7 files, parsed 7" in r.output
    r = runner.invoke(app, ["graph", "--path", str(py)])
    assert "parsed 0" in r.output
    r = runner.invoke(app, ["graph", "--stats", "--path", str(py)])
    assert r.exit_code == 0 and "Files:    7 (2 tests)" in r.output
    r = runner.invoke(app, ["graph", "--query", "clamp", "--path", str(py)])
    assert r.exit_code == 0 and "app/util.py:1" in r.output and "<- run" in r.output
    r = runner.invoke(app, ["graph", "--query", "nope_nothing", "--path", str(py)])
    assert r.exit_code == 1
    r = runner.invoke(app, ["graph", "--rebuild", "--path", str(py)])
    assert "graph built" in r.output
    assert (py / ".ccr" / ".gitignore").read_text().startswith("graph.db")


def test_doctor_reports_graph(py):
    r = runner.invoke(app, ["doctor", "--path", str(py)])
    assert "graph not built yet" in r.output
    update(py, workers=1)
    r = runner.invoke(app, ["doctor", "--path", str(py)])
    assert "graph: 7 files" in r.output and "fresh" in r.output
    bump(py, "app/main.py", "x = 1\n")
    r = runner.invoke(app, ["doctor", "--path", str(py)])
    assert "stale" in r.output
