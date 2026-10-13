from pathlib import Path


from ccr.detect import detect


def w(root: Path, rel: str, text: str = "x = 1\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_python(tmp_path):
    w(tmp_path, "app/main.py", "a\nb\n")
    w(tmp_path, "app/util.py")
    w(tmp_path, "pyproject.toml", '[project]\ndependencies=["fastapi","pytest"]\n')
    w(tmp_path, ".venv/lib/x.py")
    w(tmp_path, "uv.lock", "x\n" * 1000)
    p = detect(tmp_path)
    assert p.languages == {"Python": 2}
    assert "FastAPI" in p.frameworks and "pytest" in p.frameworks
    assert p.file_count == 2 and p.total_loc == 3
    assert (p.suggested_model, p.suggested_effort) == ("haiku", "low")


def test_typescript(tmp_path):
    w(tmp_path, "src/a.ts")
    w(tmp_path, "src/b.tsx")
    w(tmp_path, "package.json", '{"dependencies":{"react":"18","next":"14"}}')
    w(tmp_path, "node_modules/dep/index.js")
    w(tmp_path, "dist/out.js")
    p = detect(tmp_path)
    assert p.languages == {"TypeScript": 2}
    assert {"React", "Next.js"} <= set(p.frameworks)


def test_go(tmp_path):
    w(tmp_path, "main.go")
    w(tmp_path, "go.mod", "module x\nrequire github.com/gin-gonic/gin v1\n")
    w(tmp_path, "vendor/v.go")
    p = detect(tmp_path)
    assert p.languages == {"Go": 1} and p.frameworks == ["Gin"]


def test_java(tmp_path):
    w(tmp_path, "src/A.java")
    w(tmp_path, "pom.xml", "<dependency>spring-boot-starter</dependency>")
    p = detect(tmp_path)
    assert p.languages == {"Java": 1} and p.frameworks == ["Spring"]


def test_unsupported_zig_and_unknown(tmp_path):
    w(tmp_path, "src/main.zig")
    w(tmp_path, "weird.xyz", "\x00\x01")
    p = detect(tmp_path)
    assert p.languages == {"Zig": 1}
    assert p.frameworks == []


def test_empty_repo(tmp_path):
    p = detect(tmp_path)
    assert p.languages == {} and p.file_count == 0


def test_large_repo_suggests_sonnet(tmp_path):
    for i in range(60):
        w(tmp_path, f"m{i}.py")
    assert detect(tmp_path).suggested_model == "sonnet"
