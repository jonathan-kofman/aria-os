"""
aria_os/gh_integration/gh_aria_parts.py

Parametric defaults, CEM SF thresholds, and dual-script generation
(GH Python component + CadQuery fallback) for the 6 known ARIA parts.

Known parts:
    aria_spool, aria_cam_collar, aria_housing,
    aria_ratchet_ring, aria_brake_drum, aria_rope_guide

Per-part GH output layout:
    outputs/cad/grasshopper/<part>/
        params.json                  — parametric inputs
        <part>_gh_component.py       — paste into Grasshopper Python node
        <part>_cq_fallback.py        — headless CadQuery export (no Rhino needed)
        run_rhino_compute.py         — runner script

CEM SF thresholds:
    aria_ratchet_ring  tooth_shear   8.0  (safety-critical)
    aria_spool         radial_load   2.0
    aria_cam_collar    taper_engage  2.0
    aria_housing       wall_bending  2.0
    aria_brake_drum    hoop_stress   2.0
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Parametric defaults
# ---------------------------------------------------------------------------

GH_PART_DEFAULTS: dict[str, dict[str, Any]] = {
    "aria_spool": {
        "hub_od_mm":     120.0,
        "flange_od_mm":  200.0,
        "width_mm":       80.0,
        "bore_mm":        30.0,
        "flange_t_mm":     8.0,
        "material":       "6061-T6",
    },
    "aria_cam_collar": {
        "od_mm":          80.0,
        "bore_mm":        30.0,
        "length_mm":      40.0,
        "taper_deg":       5.0,
        "material":       "6061-T6",
    },
    "aria_housing": {
        "od_mm":         260.0,
        "wall_mm":        10.0,
        "length_mm":     180.0,
        "n_wall_bolts":    4,
        "bolt_circle_mm":150.0,
        "material":       "6061-T6",
    },
    "aria_ratchet_ring": {
        "od_mm":         213.0,
        "bore_mm":        40.0,
        "thickness_mm":   21.0,
        "n_teeth":        24,
        "tooth_height_mm": 6.0,
        "pressure_angle_deg": 26.0,
        "material":       "4140 Steel",
    },
    "aria_brake_drum": {
        "od_mm":         200.0,
        "width_mm":       60.0,
        "wall_mm":         8.0,
        "bore_mm":        40.0,
        "material":       "6061-T6",
    },
    "aria_rope_guide": {
        "width_mm":       60.0,
        "height_mm":      40.0,
        "thickness_mm":   12.0,
        "slot_dia_mm":    12.0,
        "material":       "6061-T6",
    },
}

# ---------------------------------------------------------------------------
# CEM SF thresholds per part
# ---------------------------------------------------------------------------

GH_SF_THRESHOLDS: dict[str, dict[str, float]] = {
    "aria_ratchet_ring": {"tooth_shear":       8.0},  # safety-critical
    "aria_spool":        {"radial_load":        2.0},
    "aria_cam_collar":   {"taper_engagement":   2.0},
    "aria_housing":      {"wall_bending":       2.0},
    "aria_brake_drum":   {"hoop_stress":        2.0},
    "aria_rope_guide":   {"bending":            2.0},
}


# ---------------------------------------------------------------------------
# Script generators
# ---------------------------------------------------------------------------

def generate_gh_component_script(part_id: str, params: dict[str, Any]) -> str:
    """
    Return Python code to paste into a Grasshopper Python node.
    Uses RhinoCommon API to build the geometry.
    """
    pid = part_id.lower()
    defaults = GH_PART_DEFAULTS.get(pid, {})
    merged = {**defaults, **params}

    if "ratchet_ring" in pid:
        return _gh_ratchet_ring(merged)
    if "spool" in pid:
        return _gh_spool(merged)
    if "cam_collar" in pid:
        return _gh_cam_collar(merged)
    if "housing" in pid:
        return _gh_housing(merged)
    if "brake_drum" in pid:
        return _gh_brake_drum(merged)
    if "rope_guide" in pid:
        return _gh_rope_guide(merged)

    # Generic fallback
    return textwrap.dedent(f"""\
        # Grasshopper Python component — {part_id}
        import rhinoscriptsyntax as rs
        import Rhino.Geometry as rg
        # No template for {part_id}: create a placeholder box
        box = rg.Box(rg.Plane.WorldXY, rg.Interval(-50, 50), rg.Interval(-50, 50), rg.Interval(0, 20))
        a = rg.Brep.CreateFromBox(box)
    """)


def generate_cq_fallback_script(part_id: str, params: dict[str, Any]) -> str:
    """
    Return a headless CadQuery script that exports STEP + STL without Rhino.
    Calls cem_to_geometry deterministic templates.
    """
    pid = part_id.lower()
    defaults = GH_PART_DEFAULTS.get(pid, {})
    merged = {**defaults, **params}
    params_repr = json.dumps(merged, indent=4)

    lines = [
        f"# CadQuery fallback for {part_id} — runs without Rhino/Grasshopper",
        "import sys",
        "sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent.parent))",
        "from cem_to_geometry import scalars_to_cq_script",
        "",
        f"params = {params_repr}",
        f'script = scalars_to_cq_script("{pid}", params)',
        'exec(compile(script, "<cem_to_geometry>", "exec"))',
    ]
    return "\n".join(lines) + "\n"


def _gh_ratchet_ring(p: dict) -> str:
    od       = p.get("od_mm", 213.0)
    id_      = p.get("bore_mm", 40.0)
    thick    = p.get("thickness_mm", 21.0)
    n_teeth  = int(p.get("n_teeth", 24))
    tooth_h  = p.get("tooth_height_mm", 6.0)

    return textwrap.dedent(f"""\
        # Grasshopper Python — ARIA Ratchet Ring
        import Rhino.Geometry as rg
        import math

        od, id_, thick = {od}, {id_}, {thick}
        n_teeth, tooth_h = {n_teeth}, {tooth_h}

        # Annular base
        outer = rg.Circle(rg.Plane.WorldXY, od / 2)
        inner = rg.Circle(rg.Plane.WorldXY, id_ / 2)
        outer_crv = rg.ArcCurve(outer)
        inner_crv = rg.ArcCurve(inner)

        vec = rg.Vector3d(0, 0, thick)
        outer_brep = rg.Brep.CreatePipe(outer_crv, (od - id_) / 2, False, rg.PipeCapMode.Flat, True, 1e-3, 1e-3)[0]

        # Teeth (approximate as rectangular extrusions)
        tooth_w = math.pi * od / n_teeth * 0.45
        breps = [outer_brep]
        for i in range(n_teeth):
            angle = math.radians(i * 360 / n_teeth)
            cx = (od / 2) * math.cos(angle)
            cy = (od / 2) * math.sin(angle)
            plane = rg.Plane(rg.Point3d(cx, cy, 0), rg.Vector3d(math.cos(angle), math.sin(angle), 0), rg.Vector3d(0, 0, 1))
            box = rg.Box(plane, rg.Interval(-tooth_w/2, tooth_w/2), rg.Interval(0, tooth_h), rg.Interval(0, thick))
            breps.append(rg.Brep.CreateFromBox(box))

        a = breps  # Output to GH 'a' parameter
    """)


def _gh_spool(p: dict) -> str:
    hub_od    = p.get("hub_od_mm", 120.0)
    flange_od = p.get("flange_od_mm", 200.0)
    width     = p.get("width_mm", 80.0)
    bore      = p.get("bore_mm", 30.0)
    flange_t  = p.get("flange_t_mm", 8.0)

    return textwrap.dedent(f"""\
        # Grasshopper Python — ARIA Rope Spool
        import Rhino.Geometry as rg

        hub_od, flange_od = {hub_od}, {flange_od}
        width, bore, flange_t = {width}, {bore}, {flange_t}

        def annular_cylinder(od, id_, h):
            outer = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, od / 2), h)
            inner = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, id_ / 2), h)
            o_brep = outer.ToBrep(True, True)
            i_brep = inner.ToBrep(True, True)
            result = rg.Brep.CreateBooleanDifference([o_brep], [i_brep], 1e-3)
            return result[0] if result else o_brep

        hub    = annular_cylinder(hub_od, bore, width)
        fl_bot = annular_cylinder(flange_od, bore, flange_t)
        fl_top = annular_cylinder(flange_od, bore, flange_t)
        fl_top.Translate(rg.Vector3d(0, 0, width - flange_t))

        union = rg.Brep.CreateBooleanUnion([hub, fl_bot, fl_top], 1e-3)
        a = union[0] if union else hub
    """)


def _gh_cam_collar(p: dict) -> str:
    od     = p.get("od_mm", 80.0)
    bore   = p.get("bore_mm", 30.0)
    length = p.get("length_mm", 40.0)

    return textwrap.dedent(f"""\
        # Grasshopper Python — ARIA Cam Collar
        import Rhino.Geometry as rg

        od, bore, length = {od}, {bore}, {length}
        outer = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, od / 2), length)
        inner = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, bore / 2), length)
        o_b = outer.ToBrep(True, True)
        i_b = inner.ToBrep(True, True)
        diff = rg.Brep.CreateBooleanDifference([o_b], [i_b], 1e-3)
        a = diff[0] if diff else o_b
    """)


def _gh_housing(p: dict) -> str:
    od      = p.get("od_mm", 260.0)
    wall    = p.get("wall_mm", 10.0)
    length  = p.get("length_mm", 180.0)

    return textwrap.dedent(f"""\
        # Grasshopper Python — ARIA Housing Shell
        import Rhino.Geometry as rg

        od, wall, length = {od}, {wall}, {length}
        id_ = od - 2 * wall
        outer = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, od / 2), length)
        inner = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, id_ / 2), length)
        o_b = outer.ToBrep(True, True)
        i_b = inner.ToBrep(True, True)
        diff = rg.Brep.CreateBooleanDifference([o_b], [i_b], 1e-3)
        a = diff[0] if diff else o_b
    """)


def _gh_brake_drum(p: dict) -> str:
    od    = p.get("od_mm", 200.0)
    width = p.get("width_mm", 60.0)
    wall  = p.get("wall_mm", 8.0)
    bore  = p.get("bore_mm", 40.0)

    return textwrap.dedent(f"""\
        # Grasshopper Python — ARIA Brake Drum
        import Rhino.Geometry as rg

        od, width, wall, bore = {od}, {width}, {wall}, {bore}
        id_ = od - 2 * wall
        outer = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, od / 2), width)
        inner = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, id_ / 2), width)
        hub   = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, bore / 2), width)
        o_b = outer.ToBrep(True, True)
        i_b = inner.ToBrep(True, True)
        h_b = hub.ToBrep(True, True)
        shell = rg.Brep.CreateBooleanDifference([o_b], [i_b], 1e-3)
        s = shell[0] if shell else o_b
        diff2 = rg.Brep.CreateBooleanDifference([s], [h_b], 1e-3)
        a = diff2[0] if diff2 else s
    """)


def _gh_rope_guide(p: dict) -> str:
    width  = p.get("width_mm", 60.0)
    height = p.get("height_mm", 40.0)
    thick  = p.get("thickness_mm", 12.0)
    slot   = p.get("slot_dia_mm", 12.0)

    return textwrap.dedent(f"""\
        # Grasshopper Python — ARIA Rope Guide
        import Rhino.Geometry as rg
        import math

        width, height, thick, slot = {width}, {height}, {thick}, {slot}

        box = rg.Box(rg.Plane.WorldXY,
                     rg.Interval(-width/2, width/2),
                     rg.Interval(-height/2, height/2),
                     rg.Interval(0, thick))
        body = rg.Brep.CreateFromBox(box)

        slot_axis = rg.Line(rg.Point3d(0, -height, thick/2), rg.Point3d(0, height, thick/2))
        slot_cyl  = rg.Cylinder(rg.Circle(rg.Plane(slot_axis.From, rg.Vector3d(0, 1, 0)), slot / 2), 2 * height)
        slot_brep = slot_cyl.ToBrep(True, True)

        diff = rg.Brep.CreateBooleanDifference([body], [slot_brep], 1e-3)
        a = diff[0] if diff else body
    """)


# ---------------------------------------------------------------------------
# Write all artifacts for a part to disk
# ---------------------------------------------------------------------------

def write_gh_artifacts(
    part_id: str,
    params: dict[str, Any],
    repo_root: Path | None = None,
) -> dict[str, Path]:
    """
    Write the full set of GH artifacts for a part.

    Returns a dict of {artifact_name: Path}.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    out_dir = repo_root / "outputs" / "cad" / "grasshopper" / part_id
    out_dir.mkdir(parents=True, exist_ok=True)

    defaults = GH_PART_DEFAULTS.get(part_id.lower(), {})
    merged = {**defaults, **params}

    # params.json
    params_path = out_dir / "params.json"
    params_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    # GH component script
    gh_script = generate_gh_component_script(part_id, merged)
    gh_path = out_dir / f"{part_id}_gh_component.py"
    gh_path.write_text(gh_script, encoding="utf-8")

    # CQ fallback script
    cq_script = generate_cq_fallback_script(part_id, merged)
    cq_path = out_dir / f"{part_id}_cq_fallback.py"
    cq_path.write_text(cq_script, encoding="utf-8")

    # Rhino Compute runner
    runner = _rhino_compute_runner(part_id, merged)
    runner_path = out_dir / "run_rhino_compute.py"
    runner_path.write_text(runner, encoding="utf-8")

    return {
        "params":    params_path,
        "gh_script": gh_path,
        "cq_script": cq_path,
        "runner":    runner_path,
    }


def _rhino_compute_runner(part_id: str, params: dict) -> str:
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        \"\"\"
        Runner: send {part_id} params to Rhino Compute and download STEP.
        Requires RHINO_COMPUTE_URL env var.
        \"\"\"
        import os, json, requests
        from pathlib import Path

        COMPUTE_URL = os.environ.get("RHINO_COMPUTE_URL", "http://localhost:8081")
        params = {json.dumps(params, indent=4)}

        resp = requests.post(
            f"{{COMPUTE_URL}}/grasshopper",
            json={{"algo": (Path(__file__).parent / "{part_id}_gh_component.py").read_text(),
                   "pointer": None,
                   "values": [{{"ParamName": k, "InnerTree": {{"0": [{{"type": "System.Double", "data": v}}]}}}}
                               for k, v in params.items() if isinstance(v, (int, float))]}},
            timeout=120,
        )
        resp.raise_for_status()
        out = resp.json()
        print("Rhino Compute response:", json.dumps(out, indent=2)[:500])
    """)
