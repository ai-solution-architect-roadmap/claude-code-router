"""Read-only memory readers (CLAUDE.md, auto memory, agent memory, MCP knowledge graph) and
keyword-based risk signals extracted from them. Missing sources are silently skipped."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ccr.memory.signals import MemorySignal, dedupe, matches, signals_from_kg, signals_from_text
from ccr.memory import sources as S

log = logging.getLogger("ccr.memory")

__all__ = ["MemorySignal", "MemorySource", "MemoryReport", "collect", "matches"]


@dataclass
class MemorySource:
    kind: str          # claude_md | claude_local | rules | import | auto_memory | agent_memory | mcp_memory
    scope: str         # managed | user | ancestor | project | local | nested
    path: str
    entries: int = 0   # non-empty lines, or entities + relations for MCP memory
    agent: str | None = None


@dataclass
class MemoryReport:
    sources: list[MemorySource] = field(default_factory=list)
    signals: list[MemorySignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def signals_for(self, rel_path: str) -> list[MemorySignal]:
        return [s for s in self.signals if matches(s, rel_path)]

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.signals:
            out[s.kind] = out.get(s.kind, 0) + 1
        return out


def _lines(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip())


def collect(root: str | Path, home: str | Path | None = None,
            environ: Mapping[str, str] | None = None, nested: bool = True) -> MemoryReport:
    """Read every memory source for a project. Never raises."""
    rep = MemoryReport()
    try:
        ctx = S.make_context(root, home, environ)
    except Exception as e:  # pragma: no cover - unusable root
        rep.notes.append(f"memory: cannot resolve project root: {e}")
        return rep
    try:
        amd = S.auto_memory_dir(ctx)
        if amd is not None and not S._md_dir(amd):
            rep.notes.append(f"auto memory directory is empty: {amd}")
    except Exception:
        pass
    readers = [("CLAUDE.md", lambda: S.claude_md_docs(ctx, nested=nested)),
               ("auto memory", lambda: S.auto_memory_docs(ctx)),
               ("agent memory", lambda: S.agent_memory_docs(ctx))]
    for label, fn in readers:
        try:
            docs = fn()
        except Exception as e:
            log.debug("memory reader %s failed: %s", label, e)
            continue
        for d in docs:
            rep.sources.append(MemorySource(d.kind, d.scope, str(d.path), _lines(d.text), d.agent))
            try:
                rep.signals += signals_from_text(d.text, str(d.path))
            except Exception as e:
                log.debug("signal extraction failed for %s: %s", d.path, e)
    try:
        graphs, notes = S.mcp_memory(ctx)
        rep.notes += notes
        for g in graphs:
            rep.sources.append(MemorySource("mcp_memory", "project", str(g.path),
                                            len(g.entities) + len(g.relations)))
            rep.signals += signals_from_kg(g.entities, g.relations, f"mcp:{g.server}")
    except Exception as e:
        log.debug("MCP memory reader failed: %s", e)
    rep.signals = dedupe(rep.signals)
    return rep
