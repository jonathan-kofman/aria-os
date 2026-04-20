"""
aria_os.self_extend — the self-extending engineering codebase.

Loop shape:
    issue/prompt → dispatcher → orchestrator → sub-agents
                                                  ├── Hypothesis
                                                  ├── Implementer(s)
                                                  ├── Contract tester  (guardrail 2)
                                                  ├── Physics judge    (guardrail 3)
                                                  └── Reviewer
                                   → gh pr create
                                   → trust tier    (guardrail 4)

Sub-packages:
    sandbox       — git worktree + scratch dir isolation (guardrail 1)
    contracts     — generator interface schema + fixture suite (guardrail 2)
    physics_judge — FEA / DRC / CAMotics as merge gate (guardrail 3)
    trust         — quarantine → review_required → trusted state machine
    hypothesis    — compose novel primitives from existing building blocks
    orchestrator  — top-level agent dispatcher
    pr_writer     — package survivor as a GitHub PR

Not yet imported eagerly — each submodule lazy-loaded so partial installs
don't break the package.
"""
from __future__ import annotations

__all__ = [
    "orchestrator",
    "sandbox",
    "contracts",
    "physics_judge",
    "trust",
    "hypothesis",
    "pr_writer",
]
