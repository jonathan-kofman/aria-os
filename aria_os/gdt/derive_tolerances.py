"""Derive GD&T tolerance values from real part geometry.

Replaces the boilerplate FCF the addin's enrichDrawing was emitting
("⌀ 0.20 Ⓜ A B C, FLATNESS 0.05, PERPENDICULARITY 0.10 A") with
numbers that scale to the actual part:

  * Bbox-derived datum assignment: largest face = A, second = B, third = C
  * Position tolerance: scales with the smallest hole diameter (rule of
    thumb — diameter / 30, clamped to [0.10, 0.50] mm)
  * Flatness: 0.0008 × longest edge length, clamped to [0.02, 0.30] mm
    (rough analogue to typical machining flatness specs)
  * Perpendicularity: 1.5× flatness (datum-A wall must hold tighter than
    free-form face)
  * General tolerance bracket: ISO 2768-mK by default (medium / fine):
      ±0.5 mm linear, ±0.5° angular when no part-level override

Used by `dashboard/aria_server.py` before calling /op enrichDrawing on
the SW addin: orchestrator computes the spec from the STEP geometry +
build_config, passes the derived numbers as params, addin places the
notes verbatim. Means each part gets GD&T sized to its own envelope,
not a copy-paste boilerplate.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class GdtSpec:
    """Per-part GD&T values to inject into enrichDrawing notes."""
    position_tolerance_mm: float = 0.20
    flatness_mm:           float = 0.05
    perpendicularity_mm:   float = 0.10
    general_linear_mm:     float = 0.5
    general_angular_deg:   float = 0.5
    primary_datum:         str   = "A"
    secondary_datum:       str   = "B"
    tertiary_datum:        str   = "C"
    standard:              str   = "ASME Y14.5-2018"
    iso_class:             str   = "ISO 2768-mK"
    material_label:        str   = "AS NOTED"
    finish_label:          str   = "AS NOTED"
    note_lines: list[str]        = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # rendering hint: the addin uses these line strings verbatim so
        # the planner can override formatting without changing the addin.
        if d.get("note_lines") is None:
            d["note_lines"] = [
                f"GENERAL TOL: ±{self.general_linear_mm:g} mm  "
                f"ANGULAR ±{self.general_angular_deg:g}°  ({self.iso_class})",
                f"GD&T PER {self.standard}  RFS UNLESS NOTED",
                f"MATERIAL: {self.material_label}  FINISH: {self.finish_label}",
            ]
        return d


def derive_from_step(step_path: str | Path,
                      build_config_part: dict | None = None) -> GdtSpec:
    """Compute a GdtSpec by inspecting the STEP file's bbox + features.

    Best-effort: if the STEP can't be opened (no cadquery / OCP) we fall
    back to the dataclass defaults. The build_config_part dict (one
    entry from build_config.json's parts[]) overrides material if set.

    Returns a GdtSpec ready for `.as_dict()` -> JSON -> enrichDrawing.
    """
    spec = GdtSpec()

    # 1. material from build_config (drone frame -> 6061 Alloy etc.)
    if build_config_part:
        mat = (build_config_part.get("material")
                or build_config_part.get("material_name"))
        if mat: spec.material_label = mat
        finish = build_config_part.get("finish")
        if finish: spec.finish_label = finish

    # 2. bbox-derived flatness + datum ordering
    try:
        import cadquery as cq  # type: ignore
        shape = cq.importers.importStep(str(step_path))
        bb = shape.val().BoundingBox()
        x = bb.xlen; y = bb.ylen; z = bb.zlen
        edges = sorted([("X", x), ("Y", y), ("Z", z)],
                          key=lambda t: -t[1])
        # datum letter order matches face area order: largest first
        spec.primary_datum   = f"A({edges[0][0]})"
        spec.secondary_datum = f"B({edges[1][0]})"
        spec.tertiary_datum  = f"C({edges[2][0]})"
        # flatness scales with longest edge
        longest = edges[0][1]
        flat = max(0.02, min(0.30, longest * 0.0008))
        spec.flatness_mm = round(flat, 3)
        spec.perpendicularity_mm = round(flat * 1.5, 3)
    except Exception:
        pass  # keep defaults

    # 3. position tolerance from smallest hole (best-effort feature scan)
    try:
        # cadquery's wp.faces() gives us per-face geometry; we look for
        # cylindrical faces whose normal-axis radius is small (holes).
        import cadquery as cq  # type: ignore  # noqa: F811
        shape = cq.importers.importStep(str(step_path))
        smallest_dia = None
        for f in shape.val().Faces():
            try:
                gtype = f.geomType()
                if gtype == "CYLINDER":
                    r = f.radius()
                    if r > 0 and r < 50:  # ignore large outer cylinders
                        d = r * 2
                        if smallest_dia is None or d < smallest_dia:
                            smallest_dia = d
            except Exception:
                continue
        if smallest_dia:
            pt = max(0.10, min(0.50, smallest_dia / 30.0))
            spec.position_tolerance_mm = round(pt, 3)
    except Exception:
        pass

    return spec


def derive_for_bundle(bundle_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Compute a GdtSpec for every fabricated part in a system bundle.

    Reads <bundle>/build_config.json + each part's STEP, returns
    {part_id: spec_dict} keyed by build_config part.id.
    """
    bundle = Path(bundle_dir)
    cfg_path = bundle / "build_config.json"
    if not cfg_path.is_file():
        return {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for part in cfg.get("parts", []):
        if not part.get("fabricated", True):
            continue
        step = part.get("step")
        if not step or not Path(step).is_file():
            continue
        spec = derive_from_step(step, part)
        out[part["id"]] = spec.as_dict()
    return out


if __name__ == "__main__":
    # CLI: derive specs for a bundle, print as JSON
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(derive_for_bundle(target), indent=2))
