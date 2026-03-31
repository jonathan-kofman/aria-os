"""
aria_os/cem/cem_generator.py — CEM resolve + compute entry point for the orchestrator.

Called by orchestrator.py:
    from .cem_generator import resolve_and_compute
    result = resolve_and_compute(goal, part_id, params, repo_root)

Routing priority:
  1. cem_registry → maps goal/part_id to a module name ("cem_aria" | "cem_lre" | None)
  2. Module's compute_for_goal(goal, params) → flat dict of physics scalars
  3. None → return {} (orchestrator skips CEM injection silently)

The returned dict is merged into plan["params"] by the orchestrator, with user-explicit
values never overwritten.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional


def resolve_and_compute(
    goal: str,
    part_id: str,
    params: dict[str, Any],
    repo_root: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Resolve the CEM module for this goal/part_id and compute physics-derived geometry.

    Returns a flat dict of scalars (e.g. {"od_mm": 213.0, "ratchet_n_teeth": 24, ...})
    or {} if no CEM module matches or computation fails.

    Never raises — all errors are caught and printed as [CEM] warnings.
    """
    if repo_root is not None:
        _ensure_path(repo_root)

    # 1. Resolve module name via registry
    try:
        from cem_registry import resolve_cem_module
        module_name = resolve_cem_module(goal, part_id)
    except Exception as exc:
        print(f"[CEM] registry lookup failed: {exc}")
        return {}

    if module_name is None:
        return {}  # No domain match — skip CEM injection

    # 2. Load and call the module's compute_for_goal()
    try:
        mod = _import_cem_module(module_name, repo_root)
        if mod is None:
            return {}
        result: dict[str, Any] = mod.compute_for_goal(goal, params)
        return result or {}
    except Exception as exc:
        print(f"[CEM] {module_name}.compute_for_goal failed: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_path(repo_root: Path) -> None:
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _import_cem_module(module_name: str, repo_root: Optional[Path]):
    """Import a CEM module by name, searching repo_root first."""
    import importlib

    # Try direct import (works if repo_root is on sys.path already)
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pass

    # Try loading from repo_root as a file
    if repo_root is not None:
        module_path = repo_root / f"{module_name}.py"
        if module_path.exists():
            try:
                spec = importlib.util.spec_from_file_location(module_name, module_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception as exc:
                print(f"[CEM] failed to load {module_path}: {exc}")

    print(f"[CEM] module '{module_name}' not found")
    return None
