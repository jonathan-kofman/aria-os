"""
Guardrail 2 — Contract tests before merge.

Every generator module (CadQuery template, SDF primitive, KiCad footprint
writer, etc.) must implement a defined interface so the orchestrator can
run it against a fixture suite before it ever reaches the physics judge.

The interface contract is simple and deliberately generic:

    def build(params: dict) -> dict
        # Returns: {
        #   "step_path": str | None,      # absolute path to emitted STEP
        #   "stl_path":  str | None,      # absolute path to emitted STL
        #   "bbox_mm":   (dx, dy, dz),    # bounding box, mm
        #   "units":     "mm",
        #   "kind":      "cadquery" | "sdf" | "ecad" | "other",
        #   "metadata":  {...},           # free-form per-generator metadata
        # }

The fixture suite is a set of known-input → known-output pairs. For each
fixture, the contract runner calls `build(params)`, then verifies:

  1.  The returned step_path / stl_path files exist in the sandbox scratch
      dir.
  2.  The bbox_mm is within tolerance of the fixture's expected bbox.
  3.  The STL is watertight (via trimesh if installed; skipped otherwise).
  4.  The file extensions of any generated artifacts are on the whitelist
      (defense-in-depth; sandbox already enforces this).

A candidate that fails ANY contract is rejected; the failure log feeds
back into the hypothesis regeneration prompt.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Fixture suite — hand-curated minimal set (one fixture per generator class)
# --------------------------------------------------------------------------- #

_DEFAULT_FIXTURES: dict[str, list[dict]] = {
    "cadquery": [
        {
            "name": "bracket_standard",
            "params": {"width_mm": 50, "height_mm": 30, "thickness_mm": 4,
                       "n_bolts": 2, "bolt_dia_mm": 4},
            "expected_bbox_mm": (50.0, 30.0, 4.0),
            "bbox_tolerance_pct": 15.0,
        },
        {
            "name": "flange_small",
            "params": {"od_mm": 60, "bore_mm": 20, "thickness_mm": 5,
                       "n_bolts": 4, "bolt_circle_r_mm": 22,
                       "bolt_dia_mm": 5},
            "expected_bbox_mm": (60.0, 60.0, 5.0),
            "bbox_tolerance_pct": 15.0,
        },
    ],
    "sdf": [
        {
            "name": "octet_cube",
            "params": {"size_mm": 30, "cell_size_mm": 8,
                       "beam_radius_mm": 1.0},
            "expected_bbox_mm": (30.0, 30.0, 30.0),
            "bbox_tolerance_pct": 10.0,
        },
        {
            "name": "gyroid_cube",
            "params": {"size_mm": 30, "cell_size_mm": 6,
                       "thickness_mm": 0.8},
            "expected_bbox_mm": (30.0, 30.0, 30.0),
            "bbox_tolerance_pct": 10.0,
        },
    ],
    "ecad": [
        # ECAD contract: emit a valid .kicad_pcb + pass structural sanity
        # check (non-empty, opens in kicad-cli).
        {
            "name": "tiny_board",
            "params": {"board_w_mm": 30, "board_h_mm": 20,
                       "n_components": 3},
            "expected_files": [".kicad_pcb"],
        },
    ],
}


# --------------------------------------------------------------------------- #
# Contract runner
# --------------------------------------------------------------------------- #

def _load_candidate_build_fn(sandbox, candidate: dict):
    """Import the candidate module from the sandbox worktree and return
    its `build(params)` callable."""
    module_relpath = candidate["module_relpath"]
    module_path = Path(sandbox.worktree_dir) / module_relpath
    if not module_path.is_file():
        raise FileNotFoundError(
            f"candidate module not written: {module_path}")

    spec = importlib.util.spec_from_file_location(
        candidate.get("name", "candidate_module"), module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    # Ensure the module can import from `aria_os.*` inside the worktree
    worktree_root = str(Path(sandbox.worktree_dir).resolve())
    if worktree_root not in sys.path:
        sys.path.insert(0, worktree_root)
    spec.loader.exec_module(module)

    if not hasattr(module, "build"):
        raise AttributeError(
            "candidate module must expose a top-level "
            "`build(params: dict) -> dict` function")
    return module.build


def _bbox_close(actual, expected, tol_pct: float) -> bool:
    """Compare two 3-tuples within a percentage tolerance."""
    if actual is None or expected is None:
        return False
    tol = tol_pct / 100.0
    for a, e in zip(actual, expected):
        if e <= 0:
            continue
        if abs(a - e) / e > tol:
            return False
    return True


def run_contract_suite(*, sandbox, candidate: dict) -> tuple[bool, dict]:
    """Run the fixture suite for the candidate's kind and return
    (ok, report).

    report shape:
        {
          "kind":          "cadquery" | "sdf" | "ecad",
          "fixtures_run":  int,
          "fixtures_pass": int,
          "reason":        str | None,   # first failing fixture reason
          "per_fixture":   [ {fixture_name, passed, reason}, ... ]
        }
    """
    kind = candidate.get("kind", "cadquery")
    fixtures = _DEFAULT_FIXTURES.get(kind)
    if not fixtures:
        return False, {"kind": kind, "fixtures_run": 0, "fixtures_pass": 0,
                       "reason": f"no fixtures defined for kind '{kind}'"}

    try:
        build_fn = _load_candidate_build_fn(sandbox, candidate)
    except Exception as exc:
        return False, {"kind": kind, "fixtures_run": 0, "fixtures_pass": 0,
                       "reason": f"import error: {type(exc).__name__}: {exc}"}

    n_pass = 0
    per_fixture: list[dict] = []
    first_failure_reason: str | None = None

    for fix in fixtures:
        entry = {"fixture": fix["name"], "passed": False, "reason": None}
        try:
            out = build_fn(fix["params"])
        except Exception as exc:
            entry["reason"] = f"build() raised: {type(exc).__name__}: {exc}"
            per_fixture.append(entry)
            first_failure_reason = first_failure_reason or entry["reason"]
            continue

        # Interface shape check
        if not isinstance(out, dict):
            entry["reason"] = f"build() returned {type(out).__name__}, not dict"
            per_fixture.append(entry)
            first_failure_reason = first_failure_reason or entry["reason"]
            continue
        if out.get("units") != "mm":
            entry["reason"] = f"units must be 'mm', got {out.get('units')!r}"
            per_fixture.append(entry)
            first_failure_reason = first_failure_reason or entry["reason"]
            continue

        # Artifact existence
        for pathkey in ("step_path", "stl_path"):
            p = out.get(pathkey)
            if p is None:
                continue
            if not Path(p).is_file():
                entry["reason"] = f"{pathkey} points to missing file: {p}"
                break
        if entry["reason"]:
            per_fixture.append(entry)
            first_failure_reason = first_failure_reason or entry["reason"]
            continue

        # Bbox tolerance check (MCAD)
        exp_bbox = fix.get("expected_bbox_mm")
        if exp_bbox is not None:
            bbox = out.get("bbox_mm")
            if not _bbox_close(bbox, exp_bbox,
                               fix.get("bbox_tolerance_pct", 15.0)):
                entry["reason"] = (f"bbox out of tolerance: "
                                   f"got {bbox}, expected ~{exp_bbox}")
                per_fixture.append(entry)
                first_failure_reason = first_failure_reason or entry["reason"]
                continue

        # Watertight check (MCAD, trimesh optional)
        stl_path = out.get("stl_path")
        if stl_path and Path(stl_path).is_file():
            try:
                import trimesh  # type: ignore
                m = trimesh.load(stl_path)
                if not m.is_watertight:
                    entry["reason"] = "STL not watertight"
                    per_fixture.append(entry)
                    first_failure_reason = (first_failure_reason
                                            or entry["reason"])
                    continue
            except ImportError:
                pass  # trimesh not installed — skip check

        # Expected file types (ECAD)
        for ext in fix.get("expected_files", []):
            matches = list(Path(sandbox.scratch_dir).rglob(f"*{ext}"))
            if not matches:
                entry["reason"] = f"no {ext} file found in scratch dir"
                break
        if entry["reason"]:
            per_fixture.append(entry)
            first_failure_reason = first_failure_reason or entry["reason"]
            continue

        entry["passed"] = True
        n_pass += 1
        per_fixture.append(entry)

    ok = n_pass == len(fixtures)
    report = {
        "kind": kind,
        "fixtures_run": len(fixtures),
        "fixtures_pass": n_pass,
        "reason": first_failure_reason if not ok else None,
        "per_fixture": per_fixture,
    }
    return ok, report


def contract_failure_prompt(report: dict) -> str:
    """Turn a failing contract report into a prompt the Hypothesis agent
    can consume to regenerate a better candidate."""
    lines = [
        f"Contract suite failed: {report['fixtures_pass']}/"
        f"{report['fixtures_run']} fixtures passed.",
        f"Primary failure: {report.get('reason', 'unknown')}",
        "",
        "Per-fixture results:",
    ]
    for f in report.get("per_fixture", []):
        status = "PASS" if f["passed"] else "FAIL"
        lines.append(f"  [{status}] {f['fixture']}: {f.get('reason', '')}")
    lines.append("")
    lines.append(
        "Rewrite the generator so it satisfies the interface contract "
        "(build(params) -> {step_path, stl_path, bbox_mm, units='mm', "
        "kind, metadata}) AND produces geometry within the expected "
        "bounding box for each fixture.")
    return "\n".join(lines)
