"""SQLite storage for the code graph (.ccr/graph.db). stdlib only, safe to import in hooks."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS nodes(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- file | function | method | class | interface | module | type | macro
    name TEXT NOT NULL,          -- symbol name; basename for files
    path TEXT NOT NULL,          -- project-relative posix path of the (owning) file
    file_id INTEGER,             -- owning file node (NULL for files)
    parent_id INTEGER,           -- enclosing symbol (NULL = top level)
    start_line INTEGER, end_line INTEGER,
    branches INTEGER DEFAULT 0, nesting INTEGER DEFAULT 0,
    -- file-only columns
    lang TEXT, parser TEXT, loc INTEGER, size INTEGER, mtime_ns INTEGER, hash TEXT,
    is_test INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS nodes_file_path ON nodes(path) WHERE kind = 'file';
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS nodes_file ON nodes(file_id);
CREATE TABLE IF NOT EXISTS edges(
    src INTEGER NOT NULL, dst INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- imports | calls | contains
    line INTEGER, ref_id INTEGER -- ref_id: refs.rowid for call edges
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS edges_ref ON edges(ref_id);
CREATE TABLE IF NOT EXISTS imports(
    file_id INTEGER NOT NULL, spec TEXT NOT NULL, line INTEGER, resolved_id INTEGER
);
CREATE INDEX IF NOT EXISTS imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS imports_resolved ON imports(resolved_id);
CREATE TABLE IF NOT EXISTS refs(
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, caller_id INTEGER,
    name TEXT NOT NULL, line INTEGER,
    qual INTEGER DEFAULT 0       -- 1 = qualified call (x.f(), pkg.F()), 0 = bare f()
);
CREATE INDEX IF NOT EXISTS refs_file ON refs(file_id);
CREATE INDEX IF NOT EXISTS refs_name ON refs(name);
CREATE TABLE IF NOT EXISTS churn(
    path TEXT PRIMARY KEY, commits INTEGER, authors INTEGER, last_ts INTEGER
);
"""


def db_path(root: Path) -> Path:
    return Path(root) / ".ccr" / "graph.db"


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))


def ensure_gitignored(root: Path) -> None:
    """Drop a .ccr/.gitignore so the db is never committed, even before `ccr init`."""
    gi = Path(root) / ".ccr" / ".gitignore"
    if not gi.exists():
        try:
            gi.parent.mkdir(parents=True, exist_ok=True)
            gi.write_text("graph.db\ngraph.db-*\nlocal/\n", encoding="utf-8")
        except OSError:
            pass
