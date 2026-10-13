"""Token-free task evaluation: classification, code location and risk scoring.

`ccr.evaluate.llm` holds the optional (opt-in) LLM assist; nothing here imports typer or
tree-sitter, so it is safe to use from hooks.
"""
from __future__ import annotations

from ccr.evaluate.classify import TaskClass, classify
from ccr.evaluate.locate import Candidate, Location, locate
from ccr.evaluate.risk import Component, Risk, assess_risk

__all__ = ["Candidate", "Component", "Location", "Risk", "TaskClass", "assess_risk",
           "classify", "locate"]
