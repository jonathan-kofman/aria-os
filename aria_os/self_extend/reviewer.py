"""
Reviewer — picks the best survivor from the set that passed both contract
and physics gates. Pure Python heuristic today; future versions can
delegate to a Claude Code sub-agent for tie-breaks that need judgement.
"""
from __future__ import annotations

from typing import Any


def pick_best(survivors: list[dict], *, spec: dict,
              dry_run: bool = False) -> dict:
    """Given survivors of (contract + physics) gating, return the best.

    survivor shape:
      {"candidate": {...}, "verdict": {passed, score, metrics, reason, ...},
       "sandbox_worktree": str}

    Heuristic: highest physics score wins; ties broken by lowest mass
    (if reported), then by most specific candidate kind.
    """
    if not survivors:
        raise ValueError("no survivors to pick from")
    if len(survivors) == 1:
        return survivors[0]

    def _key(s: dict) -> tuple:
        v = s.get("verdict", {})
        m = v.get("metrics", {})
        score = float(v.get("score", 0.0))
        mass = float(m.get("mass_g") or m.get("total_mass_g") or 1e9)
        kind_rank = {"sdf": 2, "cadquery": 1, "ecad": 1, "other": 0}
        kind = s.get("candidate", {}).get("kind", "other")
        # Higher score, lower mass, higher kind-rank preferred.
        return (score, -mass, kind_rank.get(kind, 0))

    return max(survivors, key=_key)
