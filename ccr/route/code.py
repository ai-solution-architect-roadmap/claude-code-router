"""Code-first tier: a score of the code a task touches decides the tier, the prompt's
keywords only break ties. Enabled with `code_first.enabled` (see docs/CODE_FIRST_ROUTING.md).

Score components, each in [0, 1] (noisy-or, so one strong factor is enough and several
moderate ones add up):

  files          how many distinct non-test files the task touches (1 -> 0, 2 -> .33, 8+ -> 1)
  change_size    lines already changed in those files (uncommitted edits)
  blast_radius   \\
  complexity      |  from ccr.evaluate.risk, already scaled by how sure we are a file is
  test_proximity  |  touched
  churn           |
  critical_path   |
  memory         /

Blast radius is scaled by how far an edit of the task's kind reaches (REACH). The tier never
drops more than one below the class's usual start, so a debugging hunt in a small file
still gets room to think.

Bands (`code_first.bands`, three edges) map the score to T1 | T2 | T2H | T3; a trivial edit in
the lowest band goes to T0. T4 still needs a T4 rule; history and budget still apply after.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ccr.config import TIERS

CODE_KINDS = {"trivial", "tests", "bugfix", "feature", "refactor", "perf", "migration",
              "security", "debugging"}
WEIGHTS = {"files": 0.5, "change_size": 0.4, "blast_radius": 0.6, "complexity": 0.5,
           "test_proximity": 0.25, "churn": 0.2, "critical_path": 0.75, "memory": 0.6}
CHANGE_FULL_LINES = 400
STRONG = 0.4             # candidate confidence that counts a file as touched
BAND_TIERS = ("T1", "T2", "T2H", "T3")
# How far an edit of this kind reaches past the lines it changes, until step 2 (a Haiku
# prediction of the change) can say it from the code: a typo fix does not break the 30 files
# that import its module, a refactor may. Scales blast radius; `tests` work is not riskier
# because tests are missing (that is the task), and a trivial edit cares less.
REACH = {"trivial": 0.3, "tests": 0.3, "bugfix": 0.6, "perf": 0.6, "debugging": 0.6,
         "feature": 1.0, "refactor": 1.0, "migration": 1.0, "security": 1.0}
TEST_GAP = {"tests": 0.0, "trivial": 0.5}


@dataclass
class CodeTier:
    applies: bool
    tier: str | None = None
    score: float | None = None
    parts: dict[str, float] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    lines: int = 0
    reasons: list[str] = field(default_factory=list)
    fallback: str | None = None      # why keyword-first routing was used instead

    def to_dict(self) -> dict:
        return {"applies": self.applies, "tier": self.tier, "score": self.score,
                "parts": self.parts, "files": self.files, "lines": self.lines,
                "fallback": self.fallback}


def _files_value(n: int) -> float:
    return min(1.0, math.log2(n) / 3) if n > 1 else 0.0


def _size_value(lines: int) -> float:
    return min(1.0, math.log2(1 + lines) / math.log2(1 + CHANGE_FULL_LINES)) if lines else 0.0


def score(loc, risk, workspace=None, kind: str = "feature", sweeping: bool = False
          ) -> tuple[float, dict[str, float], list[str], int]:
    """(score, components, touched files, changed lines)."""
    touched = [c.path for c in loc.candidates if c.confidence >= STRONG and not c.is_test]
    if not touched:
        touched = [c.path for c in loc.primary() if not c.is_test][:3]
    lines = workspace.lines(touched) if workspace is not None else 0
    parts = {"files": round(_files_value(len(touched)), 3),
             "change_size": round(_size_value(lines), 3)}
    for name in ("blast_radius", "complexity", "test_proximity", "churn", "critical_path",
                 "memory"):
        comp = risk.components.get(name)
        parts[name] = round(comp.value, 3) if comp is not None else 0.0
    reach = 1.0 if sweeping else REACH.get(kind, 1.0)    # "everywhere": the edit spreads
    parts["blast_radius"] = round(parts["blast_radius"] * reach, 3)
    parts["test_proximity"] = round(parts["test_proximity"] * TEST_GAP.get(kind, 1.0), 3)
    miss = 1.0
    for name, w in WEIGHTS.items():
        miss *= 1.0 - w * max(0.0, min(1.0, parts[name]))
    return round(1.0 - miss, 3), parts, touched, lines


def _band(s: float, bands: list[float]) -> int:
    """Index into BAND_TIERS."""
    return sum(1 for edge in bands if s >= edge)


def code_tier(tc, loc, risk, cfg, workspace=None) -> CodeTier:
    cf = cfg["code_first"]
    if tc.kind not in CODE_KINDS:
        return CodeTier(False, fallback=f"{tc.kind} does not edit code")
    if not loc.graph:
        return CodeTier(False, fallback="no code graph (run `ccr graph`)")
    if loc.confidence < cf["min_confidence"]:
        return CodeTier(False, fallback=f"files not known (location confidence "
                                        f"{loc.confidence:.2f} < {cf['min_confidence']:.2f})")
    s, parts, touched, lines = score(loc, risk, workspace, tc.kind, bool(tc.sweeping))
    bands = list(cf["bands"])
    idx = _band(s, bands)
    reasons = []
    top = sorted(((v, n) for n, v in parts.items() if v > 0), reverse=True)[:3]
    what = ", ".join(f"{n.replace('_', ' ')} {v:.2f}" for v, n in top) or "simple code"
    reasons.append(f"code score {s:.2f} over {len(touched)} file(s)"
                   + (f", {lines} lines already changed" if lines else "") + f" ({what})")
    # keywords break a tie: near an edge, the class's own starting tier picks the side
    margin = float(cf["tie_margin"])
    base_idx = TIERS.index(cfg["class_base"][tc.kind])
    for i, edge in enumerate(bands):
        if abs(s - edge) < margin:
            lower, upper = BAND_TIERS[i], BAND_TIERS[i + 1]
            side = upper if base_idx >= TIERS.index(upper) else lower
            if BAND_TIERS[idx] != side:
                reasons.append(f"near the {lower}/{upper} edge ({edge:.2f}): the class base "
                               f"{TIERS[base_idx]} for {tc.kind} breaks the tie -> {side}")
                idx = BAND_TIERS.index(side)
            break
    tier = BAND_TIERS[idx]
    if tc.kind == "trivial" and idx == 0:
        tier = "T0"      # a trivial edit on simple code
    # the task type still says how much thinking the work needs before any edit (a debugging
    # hunt, a migration): the code can lower it by one tier at most
    floor = max(0, base_idx - 1)
    if TIERS.index(tier) < floor:
        reasons.append(f"floor {TIERS[floor]}: one below the {TIERS[base_idx]} start for "
                       f"{tc.kind}")
        tier = TIERS[floor]
    reasons.append(f"code tier {tier}")
    return CodeTier(True, tier=tier, score=s, parts=parts, files=touched[:8], lines=lines,
                    reasons=reasons)
