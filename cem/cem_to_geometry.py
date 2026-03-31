"""
cem_to_geometry.py — CEM Scalars → Deterministic CadQuery

Converts physics-derived geometry dicts (from cem_aria / cem_lre) into
executable CadQuery scripts. NO LLM calls in this path — fully deterministic.

Usage:
    from cem_to_geometry import scalars_to_cq_script
    script = scalars_to_cq_script("aria_ratchet_ring", params)
    # Returns a Python string ready to exec() or write to .py
"""
from __future__ import annotations

import textwrap
from typing import Any


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def scalars_to_cq_script(part_id: str, params: dict[str, Any]) -> str:
    """
    Return a CadQuery script string for the given part and physics params.
    Raises ValueError if no template exists for part_id.
    """
    pid = part_id.lower().strip()
    generators = {
        "aria_ratchet_ring": _cq_ratchet_ring,
        "aria_brake_drum":   _cq_brake_drum,
        "aria_spool":        _cq_spool,
        "aria_housing":      _cq_housing,
        "aria_cam_collar":   _cq_cam_collar,
        "aria_rope_guide":   _cq_rope_guide,
        "lre_nozzle":        _cq_lre_nozzle,
        "aria_nozzle":       _cq_lre_nozzle,
    }
    gen = generators.get(pid)
    if gen is None:
        # Attempt partial match
        for key, fn in generators.items():
            if key in pid or pid in key:
                gen = fn
                break
    if gen is None:
        raise ValueError(
            f"cem_to_geometry: no deterministic template for '{part_id}'. "
            f"Available: {sorted(generators)}"
        )
    return gen(params)


# ---------------------------------------------------------------------------
# ARIA part templates
# ---------------------------------------------------------------------------

def _p(params: dict, key: str, default: float) -> float:
    """Get a float param with fallback default."""
    v = params.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cq_ratchet_ring(params: dict) -> str:
    od       = _p(params, "od_mm",          213.0)
    id_      = _p(params, "bore_mm",          40.0)
    thick    = _p(params, "thickness_mm",     21.0)
    n_teeth  = int(_p(params, "ratchet_n_teeth", 24))
    tooth_h  = _p(params, "ratchet_tooth_height_mm", 6.0)
    face_w   = _p(params, "ratchet_face_width_mm",  thick)

    return textwrap.dedent(f"""\
        # CEM-derived ratchet ring — deterministic, no LLM
        import cadquery as cq, math

        od      = {od}
        id_     = {id_}
        thick   = {thick}
        n_teeth = {n_teeth}
        tooth_h = {tooth_h}
        face_w  = min({face_w}, thick)

        # Base annular ring
        ring = (
            cq.Workplane("XY")
            .circle(od / 2).circle(id_ / 2)
            .extrude(thick)
        )

        # Teeth: rectangular extrusions around the OD
        angle_step = 360.0 / n_teeth
        tooth_w    = math.pi * od / n_teeth * 0.45   # 45% of pitch
        for i in range(n_teeth):
            angle = i * angle_step
            tooth = (
                cq.Workplane("XY")
                .transformed(rotate=(0, 0, angle))
                .rect(tooth_w, tooth_h)
                .extrude(face_w)
                .translate((od / 2 + tooth_h / 2, 0, (thick - face_w) / 2))
            )
            ring = ring.union(tooth)

        result = ring
        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/aria_ratchet_ring.step")
        cq.exporters.export(result, "/tmp/aria_ratchet_ring.stl")
    """)


def _cq_brake_drum(params: dict) -> str:
    od      = _p(params, "brake_drum_od_mm",   200.0)
    width   = _p(params, "brake_drum_width_mm",  60.0)
    wall    = _p(params, "brake_drum_wall_mm",    8.0)
    bore    = _p(params, "bore_mm",              40.0)

    return textwrap.dedent(f"""\
        # CEM-derived brake drum — deterministic, no LLM
        import cadquery as cq

        od    = {od}
        width = {width}
        wall  = {wall}
        bore  = {bore}

        drum = (
            cq.Workplane("XY")
            .circle(od / 2).circle(od / 2 - wall)
            .extrude(width)
        )
        # Hub bore through centre
        hub = (
            cq.Workplane("XY")
            .circle(bore / 2)
            .extrude(width)
        )
        result = drum.cut(hub)

        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/aria_brake_drum.step")
        cq.exporters.export(result, "/tmp/aria_brake_drum.stl")
    """)


def _cq_spool(params: dict) -> str:
    hub_od   = _p(params, "spool_hub_od_mm",    120.0)
    flange_od= _p(params, "spool_flange_od_mm", 200.0)
    width    = _p(params, "spool_width_mm",       80.0)
    bore     = _p(params, "bore_mm",              30.0)
    flange_t = max(8.0, width * 0.08)

    return textwrap.dedent(f"""\
        # CEM-derived rope spool — deterministic, no LLM
        import cadquery as cq

        hub_od    = {hub_od}
        flange_od = {flange_od}
        width     = {width}
        bore      = {bore}
        flange_t  = {flange_t}

        # Hub cylinder
        hub = (
            cq.Workplane("XY")
            .circle(hub_od / 2).circle(bore / 2)
            .extrude(width)
        )
        # Flanges (each side)
        flange = (
            cq.Workplane("XY")
            .circle(flange_od / 2).circle(bore / 2)
            .extrude(flange_t)
        )
        flange2 = flange.translate((0, 0, width - flange_t))
        result = hub.union(flange).union(flange2)

        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/aria_spool.step")
        cq.exporters.export(result, "/tmp/aria_spool.stl")
    """)


def _cq_housing(params: dict) -> str:
    od      = _p(params, "housing_od_mm",    260.0)
    wall    = _p(params, "housing_wall_mm",   10.0)
    length  = _p(params, "housing_length_mm",180.0)
    n_bolts = int(_p(params, "n_wall_bolts",   4))
    bc_r    = _p(params, "bolt_circle_mm",   150.0) / 2

    return textwrap.dedent(f"""\
        # CEM-derived housing — deterministic, no LLM
        import cadquery as cq, math

        od      = {od}
        wall    = {wall}
        length  = {length}
        n_bolts = {n_bolts}
        bc_r    = {bc_r}
        bolt_d  = 8.5

        shell = (
            cq.Workplane("XY")
            .circle(od / 2).circle(od / 2 - wall)
            .extrude(length)
        )
        # End plate
        end = (
            cq.Workplane("XY")
            .circle(od / 2)
            .extrude(wall)
        )
        result = shell.union(end)
        # Mounting bolt holes on end plate
        for i in range(n_bolts):
            angle = math.radians(i * 360 / n_bolts)
            cx = bc_r * math.cos(angle)
            cy = bc_r * math.sin(angle)
            result = result.cut(
                cq.Workplane("XY").center(cx, cy).circle(bolt_d / 2).extrude(wall)
            )

        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/aria_housing.step")
        cq.exporters.export(result, "/tmp/aria_housing.stl")
    """)


def _cq_cam_collar(params: dict) -> str:
    od     = _p(params, "od_mm",        80.0)
    bore   = _p(params, "bore_mm",      30.0)
    length = _p(params, "length_mm",    40.0)
    taper  = _p(params, "taper_deg",     5.0)

    return textwrap.dedent(f"""\
        # CEM-derived cam collar — deterministic, no LLM
        import cadquery as cq

        od     = {od}
        bore   = {bore}
        length = {length}

        result = (
            cq.Workplane("XY")
            .circle(od / 2).circle(bore / 2)
            .extrude(length)
        )

        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/aria_cam_collar.step")
        cq.exporters.export(result, "/tmp/aria_cam_collar.stl")
    """)


def _cq_rope_guide(params: dict) -> str:
    width  = _p(params, "width_mm",     60.0)
    height = _p(params, "height_mm",    40.0)
    thick  = _p(params, "thickness_mm", 12.0)
    slot   = _p(params, "diameter_mm",  12.0)

    return textwrap.dedent(f"""\
        # CEM-derived rope guide — deterministic, no LLM
        import cadquery as cq

        width  = {width}
        height = {height}
        thick  = {thick}
        slot   = {slot}

        body = (
            cq.Workplane("XY")
            .rect(width, height)
            .extrude(thick)
        )
        # Rope slot through the body
        cut = (
            cq.Workplane("XZ")
            .center(0, thick / 2)
            .circle(slot / 2)
            .extrude(height)
        )
        result = body.cut(cut)

        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/aria_rope_guide.step")
        cq.exporters.export(result, "/tmp/aria_rope_guide.stl")
    """)


def _cq_lre_nozzle(params: dict) -> str:
    entry_r  = _p(params, "entry_r_mm",     60.0)
    throat_r = _p(params, "throat_r_mm",    25.0)
    exit_r   = _p(params, "exit_r_mm",      80.0)
    conv_len = _p(params, "conv_length_mm",  80.0)
    length   = _p(params, "length_mm",      200.0)
    wall     = _p(params, "wall_mm",          3.0)
    div_len  = length - conv_len

    return textwrap.dedent(f"""\
        # CEM-derived LRE bell nozzle — deterministic, no LLM
        # Revolved convergent+divergent hollow profile in XY plane around Y axis
        import cadquery as cq

        entry_r  = {entry_r}
        throat_r = {throat_r}
        exit_r   = {exit_r}
        conv_len = {conv_len}
        div_len  = {div_len}
        wall     = {wall}

        # Outer profile points (from left = entry, right = exit, along Y axis)
        pts_outer = [
            (entry_r,            0),
            (throat_r,           conv_len),
            (exit_r,             conv_len + div_len),
        ]
        # Inner profile (offset inward by wall)
        pts_inner = [
            (max(entry_r - wall, 1), 0),
            (max(throat_r - wall, 1), conv_len),
            (max(exit_r - wall, 1),   conv_len + div_len),
        ]

        profile = (
            cq.Workplane("XY")
            .polyline(pts_outer)
            .polyline(list(reversed(pts_inner)))
            .close()
        )
        result = profile.revolve(360, (0, 0, 0), (0, 1, 0))

        bb = result.val().BoundingBox()
        print(f"BBOX:{{bb.xmax - bb.xmin:.1f}},{{bb.ymax - bb.ymin:.1f}},{{bb.zmax - bb.zmin:.1f}}")
        cq.exporters.export(result, "/tmp/lre_nozzle.step")
        cq.exporters.export(result, "/tmp/lre_nozzle.stl")
    """)


# ---------------------------------------------------------------------------
# Convenience: write script to file
# ---------------------------------------------------------------------------

def write_cq_script(part_id: str, params: dict[str, Any], path: str) -> str:
    """Generate and write CadQuery script to *path*. Returns the script text."""
    script = scalars_to_cq_script(part_id, params)
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(script, encoding="utf-8")
    return script
