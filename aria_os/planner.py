"""Read goal + context, output operation plan as structured dict with build order (and plain-text for display)."""
import re
from .context_loader import load_context, get_mechanical_constants
from typing import Any
from pathlib import Path
from .cem_context import load_cem_geometry, get_part_dimensions
from . import event_bus

# Template default dimensions per part_id. Used by has_dimensional_overrides.
TEMPLATE_DIMS = {
    "aria_spool": {"diameter": 600.0, "height": 50.0, "bore": 47.2},
    "aria_cam_collar": {"diameter": 55.0, "height": 40.0, "bore": 25.0},
    "aria_housing": {"width": 700.0, "height": 680.0, "depth": 344.0},
    "aria_rope_guide": {"width": 80.0, "height": 40.0, "depth": 10.0},
    "aria_motor_mount": {"width": 120.0, "height": 120.0, "depth": 8.0},
    "aria_ratchet_ring": {"diameter": 240.0, "face_width": 20.0, "bore": 120.0, "n_teeth": 24},
}

# Keywords that indicate a feature not in the template (force LLM)
OVERRIDE_FEATURE_KEYWORDS = {
    "aria_spool": [
        "flange", "keyway", "key way", "bolt circle", "m6", "90mm",
        "pocket", "slot", "relief", "counter-bore", "counterbore",
        "spline", "hub flange", "asymmetric",
    ],
    "aria_cam_collar": [
        "helical", "ramp", "set screw", "m4",
        "keyway", "key way", "spline", "slot", "undercut", "chamfer",
    ],
    "aria_ratchet_ring": [
        "asymmetric", "custom pitch", "modified addendum", "lugs",
        "involute", "pressure angle", "module", "relief", "undercut",
        "dual-direction", "bidirectional",
    ],
    "aria_housing": [
        "pocket", "slot", "boss", "rib", "gusset", "counter-bore",
        "counterbore", "viewport", "window", "cutout",
    ],
    "aria_shaft": [
        "keyway", "key way", "spline", "shoulder", "journal",
        "snap ring", "groove", "stepped", "threaded",
    ],
    "aria_bracket": [
        "gusset", "rib", "pocket", "slot", "counter-bore", "counterbore",
        "chamfer", "fillet", "asymmetric", "offset holes",
    ],
    "aria_brake_drum": [
        "fin", "vane", "rib", "slot", "ventilated", "keyway",
    ],
    "lre_nozzle": [
        "dual-throat", "dual throat", "bell curve", "contoured",
        "truncated", "film cooling", "regenerative",
    ],
}


def has_dimensional_overrides(goal: str, template_dims: dict, part_id: str = "") -> bool:
    """
    Returns True if the goal string contains explicit dimensions that differ from
    the template defaults by >5%, or mentions features not in the template.
    """
    goal_lower = goal.lower()

    # Feature keywords that indicate spec beyond template
    for pid, keywords in OVERRIDE_FEATURE_KEYWORDS.items():
        if part_id == pid and any(kw in goal_lower for kw in keywords):
            return True

    for key, template_val in template_dims.items():
        synonyms = {
            "diameter": ["outer diameter", "outer dia", "flange diameter", "diameter"],
            "height": ["height", "length", "thick", "thickness", "tall"],
            "bore": ["inner bore", "inner diameter", "bore", "bearing fit"],
            "width": ["width", "wide"],
            "depth": ["depth", "deep"],
            "face_width": ["face width", "face_width", "axial width", "width"],
            "n_teeth": ["teeth", "tooth count", "n teeth"],
        }
        patterns = synonyms.get(key, [key])
        for pat in patterns:
            m = re.search(rf"{re.escape(pat)}[^\d]*(\d+(?:\.\d+)?)\s*mm", goal_lower)
            if not m:
                m = re.search(rf"(\d+(?:\.\d+)?)\s*mm[^\d]*{re.escape(pat)}", goal_lower)
            if m:
                n = float(m.group(1))
                if template_val > 0 and abs(n - template_val) / template_val > 0.05:
                    return True
    return False


def _extract_cem_floats(context: dict[str, str]) -> dict[str, float]:
    """Parse the CEM geometry block out of context markdown and return key->float dict."""
    result: dict[str, float] = {}
    for text in context.values():
        for line in text.splitlines():
            m = re.match(r"#\s+([\w_]+):\s+([\d.]+)", line.strip())
            if m:
                try:
                    result[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    return result


def _planner_cem_floats(context: dict[str, str], cem_geom: dict) -> dict[str, float]:
    """
    Merge CEM numbers: live geometry from load_cem_geometry (input_/output_ keys)
    over markdown-parsed floats from context.
    """
    out = _extract_cem_floats(context)
    if not cem_geom:
        return out
    skip = {
        "source", "timestamp", "ansi_all_pass", "ansi_arrest_pass", "ansi_force_pass",
    }
    for k, v in cem_geom.items():
        if k in skip or isinstance(v, bool):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if k.startswith("output_"):
            out[k[7:]] = fv
        elif k.startswith("input_"):
            out[k[6:]] = fv
    return out


LATTICE_KEYWORDS = [
    "lattice",
    "weave",
    "mesh",
    "honeycomb",
    "octet",
    "truss",
    "infill",
    "kagome",
    "islamic",
    "geometric",
    "periodic",
    "tiling",
    "strut",
    "cellular",
    "porous",
]


def _extract_dim_mm(goal_lower: str, labels: list[str], default: float) -> float:
    """Best-effort parse for '<label> ... <n> mm' or '<n> mm ... <label>'."""
    for label in labels:
        m = re.search(rf"{re.escape(label)}[^\d]*(\d+(?:\.\d+)?)\s*mm", goal_lower)
        if not m:
            m = re.search(rf"(\d+(?:\.\d+)?)\s*mm[^\d]*{re.escape(label)}", goal_lower)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return float(default)


def plan(goal: str, context: dict[str, str] | None = None, repo_root: Path | None = None) -> dict[str, Any]:
    """
    Produce a structured plan. Returns dict with:
      - text: str (plain-English steps for display)
      - part_id: str (e.g. aria_housing, aria_cam_collar)
      - base_shape: dict (type, dimensions)
      - hollow: bool, wall_mm: float | None
      - features: list of dicts (type, face, dimensions, position)
      - build_order: list of step descriptions
      - material, tolerances, export_formats (optional)
    """
    event_bus.emit("step", f"Planning: {goal[:60]}", {"goal": goal})
    if context is None:
        context = load_context()
    constants = get_mechanical_constants(context)
    goal_lower = goal.lower()
    cem = load_cem_geometry(repo_root)
    cem_nums = _planner_cem_floats(context, cem)

    def _inject_cem_guidance(out: dict[str, Any]) -> dict[str, Any]:
        """Attach CEM-derived routing guidance for downstream tool selection/prompting."""
        out["cem_context"] = cem
        safety_keywords = (
            "ratchet", "pawl", "lever", "trip", "blocker",
            "shaft", "spool", "bearing", "clutch", "cam",
            "bore", "bolt", "mount", "face contact", "engagement",
        )
        if any(k in goal_lower for k in safety_keywords):
            # Route to cadquery (headless, reliable) with CEM constraints injected
            # into the plan. "fusion_cem_authoritative" was a dead route — tool_router
            # had no handler for it so parts silently fell back to heuristic routing.
            out["tool_route"] = "cadquery"
            out["cad_tool_selected"] = "cadquery"
            out["cem_route_reason"] = (
                "CEM-critical component: using CadQuery with physics-derived params injected."
            )
        else:
            out["tool_route"] = "cadquery"
            out["cad_tool_selected"] = "cadquery"
        return out

    # ---------- Lattice generator routing ----------
    if any(kw in goal_lower for kw in LATTICE_KEYWORDS):
        return _inject_cem_guidance({
            "part_id": "lattice",
            "text": goal,
            "route": "lattice_generator",
            "base_shape": "panel",
            "build_order": ["params", "pattern", "form", "export"],
        })

    # ---------- ARIA housing shell ----------
    if "housing" in goal_lower and ("shell" in goal_lower or "box" in goal_lower or "aria housing" in goal_lower):
        housing_dims = get_part_dimensions("aria_housing", cem, repo_root)
        w = float(cem_nums.get("housing_width_mm", housing_dims.get("width_mm", constants.get("housing_width", 200.0))))
        h = float(cem_nums.get("housing_height_mm", housing_dims.get("height_mm", constants.get("housing_height", 150.0))))
        d = float(cem_nums.get("housing_length_mm", housing_dims.get("length_mm", constants.get("housing_depth", 344.0))))
        wall = float(cem_nums.get("housing_wall_mm", housing_dims.get("wall_mm", constants.get("wall_thickness", 10.0))))
        bore = constants.get("bearing_od", 47.2)
        cx = constants.get("spool_center_x", 350.0)
        cy = constants.get("spool_center_y", 330.0)
        ratchet_dia = constants.get("ratchet_pocket_dia", 213.0)
        ratchet_depth = constants.get("ratchet_pocket_depth", 21.0)
        slot_w = constants.get("rope_slot_width", 30.0)
        slot_l = constants.get("rope_slot_length", 80.0)
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA housing shell plan:",
                f"1. Create solid outer box {w} x {h} x {d} mm (CEM-driven where available).",
                f"2. Hollow interior with wall thickness {wall} mm (cut inner void).",
                f"3. Front face: bearing bore Ø{bore} mm at ({cx}, {cy}), depth 12 mm.",
                f"4. Back face: bearing bore Ø{bore} mm at ({cx}, {cy}), depth 12 mm.",
                f"5. Back face: ratchet pocket Ø{ratchet_dia} mm, depth {ratchet_depth} mm.",
                f"6. Top face: rope slot {slot_w} x {slot_l} mm, depth 15 mm.",
            ]),
            "part_id": "aria_housing",
            "base_shape": {"type": "box", "width": w, "height": h, "depth": d},
            "hollow": True,
            "wall_mm": wall,
            "features": [
                {"type": "bore", "face": ">Z", "diameter": bore, "depth": 12, "center_x": cx - w / 2, "center_y": cy - h / 2},
                {"type": "bore", "face": "<Z", "diameter": bore, "depth": 12, "center_x": cx - w / 2, "center_y": cy - h / 2},
                {"type": "pocket", "face": "<Z", "diameter": ratchet_dia, "depth": ratchet_depth, "center_x": cx - w / 2, "center_y": cy - h / 2},
                {"type": "slot", "face": ">Y", "width": slot_w, "length": slot_l, "depth": 15, "center_x": 0, "center_y": 0},
            ],
            "build_order": [
                "Create solid box (no annular profile).",
                "Cut interior void as separate boolean cut.",
                "Bearing bore front face.",
                "Bearing bore back face.",
                "Ratchet pocket back face.",
                "Rope slot top face.",
            ],
            "expected_bbox": (w, h, d),
            "material": "6061 Al",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA spool hub (before rope spool) ----------
    if "spool hub" in goal_lower or (
        re.search(r"\bhub\b", goal_lower) and "spool" in goal_lower and "cam" not in goal_lower
    ):
        hub_d = float(cem_nums.get("spool_hub_d_mm", constants.get("spool_hub_d_mm", 120.0)))
        h = float(cem_nums.get("spool_width_mm", 40.0))
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Spool Hub plan:",
                f"1. Cylindrical hub Ø{hub_d} mm, length {h} mm (CEM spool hub / width).",
                "2. Add keyway / set screw only if specified in goal.",
            ]),
            "part_id": "aria_spool_hub",
            "base_shape": {"type": "cylinder", "diameter": hub_d, "height": h},
            "hollow": False,
            "wall_mm": None,
            "features": [],
            "build_order": ["Create hub cylinder.", "Add bore/features per goal."],
            "expected_bbox": (hub_d, hub_d, h),
            "material": "6061 Al",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA rope spool ----------
    if "spool" in goal_lower:
        spool_dims = get_part_dimensions("aria_spool", cem, repo_root)
        template_dims = {
            "diameter": float(spool_dims.get("flange_diameter_mm", TEMPLATE_DIMS["aria_spool"]["diameter"])),
            "height": float(spool_dims.get("width_mm", TEMPLATE_DIMS["aria_spool"]["height"])),
            "bore": float(spool_dims.get("hub_diameter_mm", TEMPLATE_DIMS["aria_spool"]["bore"])),
        }
        if has_dimensional_overrides(goal, template_dims, "aria_spool"):
            return _inject_cem_guidance(
                _plan_generic(
                    goal, constants,
                    route_reason="Dimensional overrides detected (e.g. 120mm/160mm vs 600mm template) -> LLM route",
                    context=context,
                    cem_nums=cem_nums,
                )
            )
        dia = float(cem_nums.get("spool_flange_d_mm", spool_dims.get("flange_diameter_mm", constants.get("rope_spool_dia", 600.0))))
        bore = float(cem_nums.get("spool_hub_d_mm", spool_dims.get("hub_diameter_mm", constants.get("bearing_od", 47.2))))
        spool_w = float(cem_nums.get("spool_width_mm", spool_dims.get("width_mm", 50.0)))
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA rope spool plan:",
                f"1. Create cylindrical spool outer/flange diameter {dia} mm.",
                "2. Apply 10 mm wall thickness (hollow).",
                f"3. Center bore matching hub interface ({bore} mm, CEM spool_hub_d_mm when available).",
            ]),
            "part_id": "aria_spool",
            "base_shape": {"type": "cylinder", "diameter": dia, "height": spool_w},
            "hollow": True,
            "wall_mm": 10.0,
            "features": [{"type": "bore", "face": ">Z", "diameter": bore, "through": True}],
            "build_order": ["Create outer cylinder.", "Cut inner cylinder (hollow).", "Center bore."],
            "expected_bbox": (dia, dia, spool_w),
            "material": "6061 Al",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Cam Collar ----------
    if "cam collar" in goal_lower or "cam_collar" in goal_lower:
        shoulder_od = constants.get("bearing_shoulder_od", 55.0)
        hub_bore_default = float(cem_nums.get("spool_hub_d_mm", 25.0))
        od = _extract_dim_mm(goal_lower, ["outer diameter", "od", "diameter"], shoulder_od)
        h = _extract_dim_mm(goal_lower, ["height", "length", "thickness"], 40.0)
        bore = _extract_dim_mm(goal_lower, ["bore", "inner diameter", "id"], hub_bore_default)
        has_helical = ("helical" in goal_lower) or ("ramp" in goal_lower)
        has_set_screw = ("set screw" in goal_lower) or ("set-screw" in goal_lower) or ("m4" in goal_lower)
        features = [{"type": "bore", "face": ">Z", "diameter": bore, "through": True}]
        if has_helical:
            features.append({"type": "ramp", "description": "15° helical ramp, 5mm rise over 90°"})
        if has_set_screw:
            features.append({"type": "hole", "description": "M4 radial set screw feature"})
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Cam Collar plan:",
                f"1. Cylindrical part OD {od} mm, length {h} mm.",
                f"2. ID bore {bore} mm (default from CEM spool hub when not specified).",
                "3. Add helical ramp when requested (15° ramp, 5 mm rise over 90°).",
                "4. Add radial set-screw hole when requested (M4).",
            ]),
            "part_id": "aria_cam_collar",
            "base_shape": {"type": "cylinder", "diameter": od, "height": h},
            "hollow": True,
            "wall_mm": None,
            "features": features,
            "build_order": ["Create solid cylinder.", f"Center bore {bore} mm.", "Add requested outer ramp / set-screw features."],
            "expected_bbox": (od, od, h),
            "material": "6061 Al",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Rope Guide ----------
    if "rope guide" in goal_lower or "rope_guide" in goal_lower:
        guide_dims = get_part_dimensions("aria_rope_guide", cem, repo_root)
        slot_w = float(guide_dims.get("rope_slot_width_mm", constants.get("rope_slot_width", 30.0)))
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Rope Guide plan:",
                "1. Base plate 80 x 40 x 10 mm.",
                f"2. Centered slot {slot_w} mm wide (rope slot width from aria_mechanical).",
                "3. 4x M6 mounting holes at corners: 6.5 mm dia, 15 mm from edges.",
            ]),
            "part_id": "aria_rope_guide",
            "base_shape": {"type": "box", "width": 80.0, "height": 40.0, "depth": 10.0},
            "hollow": False,
            "wall_mm": None,
            "features": [
                {"type": "slot", "face": ">Z", "width": slot_w, "length": 40.0, "depth": 10.0, "center_x": 0, "center_y": 0},
                {"type": "holes", "face": ">Z", "diameter": 6.5, "positions": [(80/2 - 15, 40/2 - 15), (-(80/2 - 15), 40/2 - 15), (-(80/2 - 15), -(40/2 - 15)), (80/2 - 15, -(40/2 - 15))]},
            ],
            "build_order": ["Create solid base plate.", "Cut centered slot.", "Add 4x M6 holes at corners."],
            "expected_bbox": (80.0, 40.0, 10.0),
            "material": "6061 Al",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Motor Mount Plate ----------
    if "motor mount" in goal_lower or "motor_mount" in goal_lower:
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Motor Mount Plate plan:",
                "1. Plate 120 x 120 x 8 mm.",
                "2. 4x M5 motor bolt pattern: 98 mm bolt circle diameter.",
                "3. Center bore 22 mm (motor shaft clearance).",
                "4. 4x M6 wall mount holes at corners: 10 mm from edges (6.5 mm dia).",
            ]),
            "part_id": "aria_motor_mount",
            "base_shape": {"type": "box", "width": 120.0, "height": 120.0, "depth": 8.0},
            "hollow": False,
            "wall_mm": None,
            "features": [
                {"type": "bore", "face": ">Z", "diameter": 22.0, "through": True, "center_x": 0, "center_y": 0},
                {"type": "bolt_circle", "face": ">Z", "diameter": 6.5, "bolt_circle_diameter": 98.0, "count": 4},
                {"type": "holes", "face": ">Z", "diameter": 6.5, "positions": [(120/2 - 10, 120/2 - 10), (-(120/2 - 10), 120/2 - 10), (-(120/2 - 10), -(120/2 - 10)), (120/2 - 10, -(120/2 - 10))]},
            ],
            "build_order": ["Create solid plate.", "Center bore 22 mm.", "4x M5 holes on 98 mm BCD.", "4x M6 corner holes."],
            "expected_bbox": (120.0, 120.0, 8.0),
            "material": "6061 Al",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Trip lever (before generic pawl) ----------
    if "trip lever" in goal_lower or "trip_lever" in goal_lower:
        L, W, H = 60.0, 6.0, 22.0
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Trip Lever plan:",
                f"1. Prismatic lever body ~{L} x {W} x {H} mm (tune from goal / CEM).",
                "2. Pivot bore Ø4.2 mm (M4) or per goal.",
                "3. Trip face angle per mechanical context (e.g. 15°).",
            ]),
            "part_id": "aria_trip_lever",
            "base_shape": {"type": "box", "width": L, "height": H, "depth": W},
            "hollow": False,
            "wall_mm": None,
            "features": [
                {"type": "bore", "face": ">Z", "diameter": 4.2, "through": True, "description": "pivot"},
            ],
            "build_order": ["Create lever blank.", "Add pivot bore.", "Add trip face / spring tab per spec."],
            "expected_bbox": (L, H, W),
            "material": "6061-T6",
            "yield_mpa": 276.0,
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Catch pawl ----------
    if (
        "blocker" in goal_lower
        or "catch pawl" in goal_lower
        or "trip pawl" in goal_lower
        or ("pawl" in goal_lower and "collar" not in goal_lower and "cam collar" not in goal_lower)
    ):
        tip_w = float(constants.get("pawl_tip_width_mm", 9.0))
        L, W, H = 55.0, max(tip_w, 6.0), 22.0
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Catch Pawl plan:",
                f"1. Body envelope ~{L} mm long x {W} mm tip width x {H} mm height.",
                "2. Pivot bore Ø6 mm for pivot pin (adjust per goal).",
                "3. Tooth tip chamfer / engagement per ratchet geometry.",
            ]),
            "part_id": "aria_catch_pawl",
            "base_shape": {"type": "box", "width": L, "height": H, "depth": W},
            "hollow": False,
            "wall_mm": None,
            "features": [
                {"type": "bore", "face": ">Z", "diameter": 6.0, "through": True, "description": "pivot pin"},
                {"type": "chamfer", "description": "tooth tip chamfer"},
            ],
            "build_order": ["Create pawl body.", "Pivot bore.", "Tip chamfer and engagement face."],
            "expected_bbox": (L, H, W),
            "material": "A2 tool steel",
            "yield_mpa": 1800.0,
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Flyweight ----------
    if "flyweight" in goal_lower or "fly weight" in goal_lower or "inertia trigger" in goal_lower:
        r = float(cem_nums.get("flyweight_radius_mm", 60.0))
        mass_g = float(cem_nums.get("flyweight_mass_g", 2764.8))
        h = 20.0
        d = 2.0 * r
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Flyweight plan:",
                f"1. Cylindrical flyweight Ø{d:.1f} mm (R={r} mm from CEM), height {h} mm.",
                f"2. Target mass context: ~{mass_g:.1f} g from CEM (adjust density/geometry).",
            ]),
            "part_id": "aria_flyweight",
            "base_shape": {"type": "cylinder", "diameter": d, "height": h},
            "hollow": False,
            "wall_mm": None,
            "features": [],
            "build_order": ["Create solid cylinder.", "Tune mass vs CEM flyweight_mass_g."],
            "expected_bbox": (d, d, h),
            "material": "steel",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Brake drum ----------
    if "brake drum" in goal_lower or (re.search(r"\bbrake\b", goal_lower) and "ratchet" not in goal_lower and "motor" not in goal_lower):
        od = float(cem_nums.get("brake_drum_diameter_mm", constants.get("brake_drum_diameter_mm", 200.0)))
        wall = float(cem_nums.get("brake_drum_wall_mm", 3.0))
        bore = max(od - 2.0 * wall, 1.0)
        h = float(cem_nums.get("spool_width_mm", 50.0))
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Brake Drum plan:",
                f"1. Annular cylinder OD {od} mm, ID {bore:.2f} mm (wall {wall} mm from CEM).",
                f"2. Axial height {h} mm (CEM spool_width_mm default).",
            ]),
            "part_id": "aria_brake_drum",
            "base_shape": {"type": "annular_cylinder", "od": od, "bore": bore, "height": h},
            "hollow": False,
            "wall_mm": wall,
            "features": [],
            "build_order": [
                f"Create outer cylinder OD {od} mm, height {h} mm.",
                f"Cut inner bore {bore:.2f} mm.",
            ],
            "expected_bbox": (od, od, h),
            "material": "4140 steel",
            "export_formats": ["step", "stl"],
        })

    # ---------- ARIA Ratchet ring ----------
    if any(
        kw in goal_lower
        for kw in ("ratchet ring", "ratchet_ring", "catch ring", "ring gear", "ratchet")
    ):
        template_r = TEMPLATE_DIMS["aria_ratchet_ring"]
        if has_dimensional_overrides(goal, template_r, "aria_ratchet_ring"):
            return _inject_cem_guidance(
                _plan_generic(
                    goal,
                    constants,
                    route_reason="Ratchet ring dimensional / feature overrides -> LLM route",
                    context=context,
                    cem_nums=cem_nums,
                )
            )
        n_teeth = int(cem_nums.get("ratchet_n_teeth", constants.get("ratchet_n_teeth", 66)))
        face_w = float(cem_nums.get("ratchet_face_width_mm", constants.get("ratchet_face_w_mm", 187.79)))
        flange_d = float(cem_nums.get("spool_flange_d_mm", constants.get("spool_flange_d_mm", 240.0)))
        hub_d = float(cem_nums.get("spool_hub_d_mm", constants.get("spool_hub_d_mm", 120.0)))
        od = flange_d
        wall = (od - hub_d) / 2.0
        pitch_r = od / 2.0
        return _inject_cem_guidance({
            "text": "\n".join([
                "ARIA Ratchet Ring plan:",
                f"1. Annular ring OD {od} mm, bore {hub_d} mm, face width {face_w} mm.",
                f"2. {n_teeth} asymmetric ratchet teeth, pitch radius {pitch_r:.1f} mm.",
                f"3. Wall thickness {wall:.1f} mm.",
                "4. Material: 4140 QT, yield 1300 MPa.",
            ]),
            "part_id": "aria_ratchet_ring",
            "base_shape": {"type": "annular_cylinder", "od": od, "bore": hub_d, "height": face_w},
            "hollow": False,
            "wall_mm": wall,
            "features": [
                {"type": "teeth", "count": n_teeth, "profile": "asymmetric_ratchet", "pitch_radius_mm": pitch_r},
            ],
            "build_order": [
                f"Create outer cylinder OD {od} mm, height {face_w} mm.",
                f"Cut center bore {hub_d} mm through.",
                f"Pattern {n_teeth} asymmetric ratchet teeth around OD.",
            ],
            "expected_bbox": (od, od, face_w),
            "material": "4140 QT",
            "yield_mpa": 1300.0,
            "export_formats": ["step", "stl"],
        })

    # ---------- Generic / unknown part: break goal into structure ----------
    return _inject_cem_guidance(_plan_generic(goal, constants, context=context, cem_nums=cem_nums))


def _slug_from_goal(goal: str) -> str:
    """Convert a free-text goal into a snake_case part_id slug (max 5 meaningful words).

    Strips filler words, lowercases, replaces spaces with underscores.
    Never returns 'aria_part'; returns 'custom_part' if nothing meaningful remains.

    Examples:
        "Liquid Rocket Engine Nozzle" -> "liquid_rocket_engine_nozzle"
        "a simple bracket with two holes" -> "simple_bracket_two_holes"
        "my ARIA housing 700mm" -> "housing_700mm"
    """
    _FILLER = frozenset({"a", "an", "the", "with", "for", "of", "and", "my", "aria"})
    words = re.sub(r"[^a-z0-9 ]", " ", goal.lower()).split()
    meaningful = [w for w in words if w not in _FILLER][:5]
    return "_".join(meaningful) if meaningful else "custom_part"


def _plan_generic(
    goal: str,
    constants: dict[str, float],
    route_reason: str = "",
    *,
    context: dict[str, str] | None = None,
    cem_nums: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Parse unknown goal into base_shape, hollow, features, build_order; use CEM when possible."""
    goal_lower = goal.lower()
    cem_nums = cem_nums or ( _planner_cem_floats(context, {}) if context else {} )

    PART_ID_MAP: list[tuple[list[str], str]] = [
        (["pawl", "catch pawl", "trip pawl", "blocker"], "aria_catch_pawl"),
        (["trip lever", "trip_lever"], "aria_trip_lever"),
        (["lever"], "aria_trip_lever"),
        (["shaft collar"], "aria_shaft_collar"),
        (["flyweight", "fly weight"], "aria_flyweight"),
        (["brake drum"], "aria_brake_drum"),
        (["bearing plate", "bearing housing"], "aria_bearing_plate"),
        (["spool hub"], "aria_spool_hub"),
        (["ratchet ring", "ratchet_ring", "catch ring", "ring gear"], "aria_ratchet_ring"),
        (["ratchet"], "aria_ratchet_ring"),
    ]

    part_id = None
    for keywords, pid in PART_ID_MAP:
        if any(kw in goal_lower for kw in keywords):
            part_id = pid
            break
    if part_id is None:
        part_id = _slug_from_goal(goal)

    base_shape: dict[str, Any] = {"type": "box", "width": 100.0, "height": 100.0, "depth": 100.0}
    if any(kw in goal_lower for kw in ("cylind", "round", "bore", "spool", "collar", "ring", "drum")):
        base_shape = {"type": "cylinder", "diameter": 50.0, "height": 30.0}

    # CEM envelope override: only for explicitly known ARIA components — never leak into
    # unrelated parts.  (The old "if part_id == 'aria_part'" block has been removed.)

    if part_id == "aria_ratchet_ring" and cem_nums.get("spool_flange_d_mm"):
        base_shape = {
            "type": "annular_cylinder",
            "od": cem_nums["spool_flange_d_mm"],
            "bore": cem_nums.get("spool_hub_d_mm", 120.0),
            "height": cem_nums.get("ratchet_face_width_mm", 20.0),
        }
    elif part_id == "aria_brake_drum" and cem_nums.get("brake_drum_diameter_mm"):
        od = cem_nums["brake_drum_diameter_mm"]
        wall = cem_nums.get("brake_drum_wall_mm", 3.0)
        base_shape = {
            "type": "annular_cylinder",
            "od": od,
            "bore": max(od - 2.0 * wall, 1.0),
            "height": cem_nums.get("spool_width_mm", 50.0),
        }
    elif part_id == "aria_flyweight" and cem_nums.get("flyweight_radius_mm"):
        d = 2.0 * cem_nums["flyweight_radius_mm"]
        base_shape = {"type": "cylinder", "diameter": d, "height": 20.0}

    hollow = False
    wall_mm = None
    if base_shape.get("type") == "annular_cylinder":
        od = float(base_shape["od"])
        bore = float(base_shape["bore"])
        wall_mm = (od - bore) / 2.0

    features: list[dict[str, Any]] = []
    build_order = ["Create main solid from description.", "Apply cuts and bores as specified."]
    expected_bbox: tuple[float, float, float] | None = None
    if base_shape.get("type") == "box":
        expected_bbox = (
            float(base_shape["width"]),
            float(base_shape["height"]),
            float(base_shape["depth"]),
        )
    elif base_shape.get("type") == "cylinder":
        d = float(base_shape["diameter"])
        h = float(base_shape["height"])
        expected_bbox = (d, d, h)
    elif base_shape.get("type") == "annular_cylinder":
        od = float(base_shape["od"])
        h = float(base_shape["height"])
        expected_bbox = (od, od, h)

    out: dict[str, Any] = {
        "text": goal,
        "part_id": part_id,
        "base_shape": base_shape,
        "hollow": hollow,
        "wall_mm": wall_mm,
        "features": features,
        "build_order": build_order,
        "expected_bbox": expected_bbox,
        "material": None,
        "export_formats": ["step", "stl"],
    }
    if route_reason:
        out["route_reason"] = route_reason
    return out
