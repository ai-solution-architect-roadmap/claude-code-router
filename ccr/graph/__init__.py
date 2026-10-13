"""Code graph: files, symbols, imports, calls, complexity and git signals in .ccr/graph.db.

Query helpers are sqlite-only (safe for hooks); building imports tree-sitter lazily.
"""
from __future__ import annotations

from ccr.graph.query import Dependent, Graph, Symbol, TestLink, freshness

__all__ = ["Dependent", "Graph", "Symbol", "TestLink", "freshness", "update"]


def update(root, **kwargs):
    """Incrementally update (or build) the graph. See ccr.graph.build.update."""
    from ccr.graph.build import update as _update
    return _update(root, **kwargs)
