"""
Hypothesis generator — when no existing template covers the request, this
stage proposes N novel candidate modules by composing existing primitives.

In dry-run mode (hackathon unit tests), emits deterministic mock
candidates so the rest of the pipeline can be exercised without LLM
calls. In live mode, spawns a Claude Code sub-agent that reads the full
primitive library and writes each candidate's source in an isolated
worktree.

Each candidate is returned as a dict:
    {
      "name":            str,                 # short human handle
      "kind":            "cadquery" | "sdf" | "ecad",
      "module_relpath":  "aria_os/generators/_cand_<n>.py",
      "code":            str,                 # the new module source
      "rationale":       str,                 # why this composition
      "parent_primitives": [list of building blocks combined],
    }
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import ExtensionRequest


# --------------------------------------------------------------------------- #
# Dry-run stub candidate — emits a valid CadQuery bracket so the
# downstream pipeline (sandbox + contract + physics) can be tested
# without a live LLM.
# --------------------------------------------------------------------------- #

_STUB_CADQUERY_CANDIDATE = '''\
"""Auto-generated dry-run candidate bracket."""
from pathlib import Path

def build(params: dict) -> dict:
    import cadquery as cq  # optional dep
    import os
    w = float(params.get("width_mm", 50))
    h = float(params.get("height_mm", 30))
    t = float(params.get("thickness_mm", 4))
    n_bolts = int(params.get("n_bolts", 2))
    bolt_d = float(params.get("bolt_dia_mm", 4))
    plate = cq.Workplane("XY").box(w, h, t)
    if n_bolts >= 2:
        pts = [(-w/3, 0), (w/3, 0)][:n_bolts]
        plate = (plate.faces(">Z").workplane()
                 .pushPoints(pts).hole(bolt_d))
    out_dir = Path(os.environ.get("ARIA_OUTPUT_DIR", "."))
    out_dir.mkdir(parents=True, exist_ok=True)
    step = out_dir / "candidate.step"
    stl = out_dir / "candidate.stl"
    cq.exporters.export(plate, str(step))
    cq.exporters.export(plate, str(stl), exportType="STL")
    bb = plate.val().BoundingBox()
    return {
        "step_path": str(step),
        "stl_path": str(stl),
        "bbox_mm": (bb.xlen, bb.ylen, bb.zlen),
        "units": "mm",
        "kind": "cadquery",
        "metadata": {"generator": "stub_dry_run"},
    }
'''


def propose_candidates(req: "ExtensionRequest", *, n: int = 4,
                       dry_run: bool = False) -> list[dict]:
    """Return a list of up to n candidate module dicts.

    In dry_run mode, returns one hand-crafted stub candidate so the rest
    of the pipeline can be exercised.
    """
    if dry_run:
        return [{
            "name": "dryrun_bracket",
            "kind": "cadquery",
            "module_relpath": "aria_os/generators/_cand_dryrun_bracket.py",
            "code": _STUB_CADQUERY_CANDIDATE,
            "rationale": "dry-run stub; covers the bracket contract suite",
            "parent_primitives": ["_cq_bracket"],
        }]

    # Live path — spawn a Claude Code sub-agent. TODO: implement via
    # subprocess call to `claude -p` with a carefully crafted prompt that
    # surfaces the full primitive library and asks for N compositions.
    # For now, falls back to the dry-run stub so the pipeline is runnable.
    return propose_candidates(req, n=n, dry_run=True)
