"""Shared blackboard state for the multi-agent refinement loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DesignState:
    """
    Shared mutable state passed between all agents in the refinement loop.

    Agents read from and write to this object. The refinement loop snapshots
    it after each iteration for history tracking and best-of-N selection.
    """

    # ── Input (set by orchestrator before loop starts) ────────────────────
    goal: str = ""
    repo_root: Path = field(default_factory=lambda: Path("."))
    domain: str = ""              # "cad", "cam", "ecad", "civil", "drawing", "assembly"
    part_id: str = ""

    # ── SpecAgent output ─────────────────────────────────────────────────
    spec: dict[str, Any] = field(default_factory=dict)
    cem_params: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    material: str = ""

    # ── DesignerAgent output ─────────────────────────────────────────────
    code: str = ""                 # generated source (CQ / CAM / pcbnew / ezdxf)
    output_path: str = ""          # primary output file (STEP / DXF / .py script)
    artifacts: dict[str, str] = field(default_factory=dict)
    bbox: dict[str, float] = field(default_factory=dict)
    generation_error: str = ""

    # ── EvalAgent output ─────────────────────────────────────────────────
    eval_passed: bool = False
    failures: list[str] = field(default_factory=list)
    domain_results: dict[str, Any] = field(default_factory=dict)  # per-validator results

    # ── RefinerAgent output ──────────────────────────────────────────────
    refinement_instructions: str = ""
    parameter_overrides: dict[str, Any] = field(default_factory=dict)

    # ── Loop tracking ────────────────────────────────────────────────────
    iteration: int = 0
    max_iterations: int = 15
    history: list[dict[str, Any]] = field(default_factory=list)
    best_iteration: int = -1
    best_failure_count: int = 999
    stall_counter: int = 0         # incremented when no improvement
    converged: bool = False
    budget_exhausted: bool = False

    def snapshot(self) -> dict[str, Any]:
        """Capture current state for history."""
        return {
            "iteration": self.iteration,
            "eval_passed": self.eval_passed,
            "failure_count": len(self.failures),
            "failures": list(self.failures),
            "bbox": dict(self.bbox),
            "generation_error": self.generation_error,
            "refinement_instructions": self.refinement_instructions,
        }

    def record_iteration(self) -> None:
        """Snapshot current state and update best/stall tracking."""
        snap = self.snapshot()
        self.history.append(snap)

        n_failures = len(self.failures)
        if n_failures < self.best_failure_count:
            self.best_failure_count = n_failures
            self.best_iteration = self.iteration
            self.stall_counter = 0
        else:
            self.stall_counter += 1

        if self.eval_passed:
            self.converged = True
        if self.iteration >= self.max_iterations:
            self.budget_exhausted = True
