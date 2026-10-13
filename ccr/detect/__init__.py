"""Repository detection: languages, frameworks, size, suggested defaults."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".venv", "venv", "vendor", "build", "dist", ".git", "target",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".next", ".gradle",
    ".idea", ".ccr", "site-packages",
}
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock",
    "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "Pipfile.lock",
}
EXT_LANG = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".go": "Go",
    ".java": "Java", ".kt": "Kotlin", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".swift": "Swift", ".scala": "Scala", ".zig": "Zig", ".ex": "Elixir",
    ".exs": "Elixir", ".lua": "Lua", ".sh": "Shell", ".dart": "Dart",
}
MAX_LOC_BYTES = 2_000_000
SMALL_FILES, SMALL_LOC = 50, 5000

# (framework name, manifest file, regex searched in lowercase content)
FRAMEWORK_RULES = [
    ("FastAPI", ("pyproject.toml", "requirements.txt"), r"fastapi"),
    ("Django", ("pyproject.toml", "requirements.txt"), r"django"),
    ("Flask", ("pyproject.toml", "requirements.txt"), r"flask"),
    ("Typer", ("pyproject.toml", "requirements.txt"), r"typer"),
    ("pytest", ("pyproject.toml", "requirements.txt"), r"pytest"),
    ("React", ("package.json",), r'"react"'),
    ("Next.js", ("package.json",), r'"next"'),
    ("Vue", ("package.json",), r'"vue"'),
    ("Angular", ("package.json",), r'"@angular/core"'),
    ("Express", ("package.json",), r'"express"'),
    ("NestJS", ("package.json",), r'"@nestjs/core"'),
    ("Gin", ("go.mod",), r"gin-gonic/gin"),
    ("Echo", ("go.mod",), r"labstack/echo"),
    ("Spring", ("pom.xml", "build.gradle", "build.gradle.kts"), r"spring"),
    ("Actix", ("Cargo.toml",), r"actix"),
    ("Axum", ("Cargo.toml",), r"axum"),
    ("Tokio", ("Cargo.toml",), r"tokio"),
    ("Rails", ("Gemfile",), r"rails"),
    ("Laravel", ("composer.json",), r"laravel"),
]


@dataclass
class Profile:
    root: str
    languages: dict[str, int] = field(default_factory=dict)  # name -> file count
    frameworks: list[str] = field(default_factory=list)
    file_count: int = 0
    total_loc: int = 0
    suggested_model: str = "sonnet"
    suggested_effort: str = "low"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _count_lines(path: Path) -> int:
    try:
        if path.stat().st_size > MAX_LOC_BYTES:
            return 0
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return ""


def _detect_frameworks(manifests: dict[str, list[Path]]) -> list[str]:
    found: list[str] = []
    for name, files, pattern in FRAMEWORK_RULES:
        for fname in files:
            if any(re.search(pattern, _read(p)) for p in manifests.get(fname, [])):
                found.append(name)
                break
    return found


def detect(root: str | Path) -> Profile:
    root = Path(root).resolve()
    langs: dict[str, int] = {}
    manifests: dict[str, list[Path]] = {}
    wanted = {f for _, fs, _ in FRAMEWORK_RULES for f in fs}
    file_count = total_loc = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            p = Path(dirpath) / name
            if name in wanted:
                manifests.setdefault(name, []).append(p)
            lang = EXT_LANG.get(p.suffix.lower())
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
                file_count += 1
                total_loc += _count_lines(p)
    langs = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    small = file_count < SMALL_FILES and total_loc < SMALL_LOC
    return Profile(
        root=str(root),
        languages=langs,
        frameworks=_detect_frameworks(manifests),
        file_count=file_count,
        total_loc=total_loc,
        suggested_model="haiku" if small else "sonnet",
        suggested_effort="low",
    )
