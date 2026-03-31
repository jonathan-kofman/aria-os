"""
cam_setup.py — Setup sheet generator for CNC operators.

Parses a generated Fusion 360 CAM script and produces a human-readable
markdown setup sheet (and JSON sidecar). Saves operator time at the machine.

Usage:
    from aria_os.cam_setup import write_setup_sheet
    write_setup_sheet(
        "outputs/cad/step/aria_housing.step",
        "outputs/cam/aria_housing/aria_housing_cam.py",
        material="aluminium_6061",
        out_dir=Path("outputs/cam/aria_housing"),
    )
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── MRR constants (mm³/min) for cycle-time estimation ────────────────────────
_MRR_BY_MATERIAL: dict[str, float] = {
    "aluminium_6061": 5000.0,
    "aluminium_7075": 4500.0,
    "steel_mild":     1200.0,
    "steel_4140":      800.0,
    "stainless_316":   600.0,
    "x1_420i":         700.0,
    "inconel_718":     200.0,
    "titanium_ti6al4v": 350.0,
    "pla":            8000.0,
    "abs":            7000.0,
}
_MRR_DEFAULT = 1000.0

CAM_SETUP_SCHEMA_VERSION = "1.0"

# Minutes assumed per operation type when volume is unknown
_OP_TIME_MIN: dict[str, float] = {
    "adaptive": 3.0,
    "parallel": 2.0,
    "contour":  1.5,
    "drill":    0.5,
    "finish":   2.0,
}


def parse_cam_script(cam_script_path: str) -> dict:
    """
    Read the generated Fusion 360 CAM Python script and extract:
    - Tool entries (number, diameter, type)
    - Material string
    - Operation names and types
    - Feed rates and spindle speeds
    - Stock / bbox dimensions (from header comment)

    Returns a structured dict.
    """
    path = Path(cam_script_path)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": str(exc), "tools": [], "operations": [], "material": "unknown",
                "bbox_mm": None, "feeds": []}

    # ── Material ───────────────────────────────────────────────────────────────
    mat_match = re.search(r'[Mm]aterial[:\s=]+["\']?(\w+)["\']?', text)
    material = mat_match.group(1) if mat_match else "unknown"

    # ── Bbox from header comment ───────────────────────────────────────────────
    # Format produced by cam_generator: "Bbox:     100.0 × 80.0 × 30.0 mm"
    bbox_mm = None
    bbox_match = re.search(
        r'Bbox:\s+([\d.]+)\s*[x×]\s*([\d.]+)\s*[x×]\s*([\d.]+)\s*mm',
        text,
    )
    if bbox_match:
        bbox_mm = {
            "x_mm": float(bbox_match.group(1)),
            "y_mm": float(bbox_match.group(2)),
            "z_mm": float(bbox_match.group(3)),
        }

    # ── Tools — scan make_flat_endmill calls ───────────────────────────────────
    # Pattern: make_flat_endmill("6mm 3-flute carbide", 0.06, 3, 12000, 180.0, 45.0)
    tool_pattern = re.compile(
        r'make_flat_endmill\(\s*"([^"]+)"\s*,\s*([\d.]+)\s*,\s*(\d+)\s*,'
        r'\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)',
    )
    tools: list[dict] = []
    for i, m in enumerate(tool_pattern.finditer(text), start=1):
        dia_cm = float(m.group(2))
        dia_mm = round(dia_cm * 10, 3)
        rpm = float(m.group(4))
        feed_cmpm = float(m.group(5))
        feed_mmpm = round(feed_cmpm * 10, 1)
        tools.append({
            "number": i,
            "name": m.group(1),
            "dia_mm": dia_mm,
            "dia_cm": dia_cm,
            "flutes": int(m.group(3)),
            "rpm": int(rpm),
            "feed_mmpm": feed_mmpm,
        })

    # Also try simpler dia_mm / tool_dia patterns (tolerant fallback)
    if not tools:
        for m in re.finditer(r'(?:dia_mm|tool_dia)\s*[=:]\s*([\d.]+)', text):
            dia = float(m.group(1))
            tools.append({"number": len(tools) + 1, "dia_mm": dia,
                          "name": f"{dia}mm endmill", "flutes": 2,
                          "rpm": None, "feed_mmpm": None})

    # ── Operations ────────────────────────────────────────────────────────────
    # Named by .name = "..." assignments in the script
    op_name_pattern = re.compile(r'(?:adaptive|parallel|contour|drill)_op[_\w]*\.name\s*=\s*"([^"]+)"')
    # Also capture createInput("optype") so we know the op type
    op_input_pattern = re.compile(r'createInput\("(\w+)"\)')

    op_names = op_name_pattern.findall(text)
    op_types = op_input_pattern.findall(text)

    # Deduplicate while preserving order
    seen_types: list[str] = []
    for t in op_types:
        if t not in ("input",) and t not in seen_types:
            seen_types.append(t)

    operations: list[dict] = []
    for idx, op_type in enumerate(seen_types):
        name = op_names[idx] if idx < len(op_names) else op_type.capitalize()
        # Assign a tool: first two ops share the first two tools, drills use subsequent
        tool_idx = min(idx, len(tools) - 1) if tools else None
        tool_info = tools[tool_idx] if tool_idx is not None else None
        operations.append({
            "index": idx + 1,
            "name": name,
            "type": op_type,
            "tool": tool_info,
        })

    # If no operations found at all, synthesise from keywords present in script
    if not operations:
        for kw in ("adaptive", "parallel", "contour", "drill"):
            if kw in text.lower():
                operations.append({
                    "index": len(operations) + 1,
                    "name": kw.capitalize(),
                    "type": kw,
                    "tool": tools[0] if tools else None,
                })

    # ── Feed rates ────────────────────────────────────────────────────────────
    feed_pattern = re.compile(r'feed[_\s]?rate\s*[=:]\s*([\d.]+)', re.IGNORECASE)
    feeds = [float(m.group(1)) for m in feed_pattern.finditer(text)]

    return {
        "material": material,
        "bbox_mm": bbox_mm,
        "tools": tools,
        "operations": operations,
        "feeds": feeds,
        "part_name": path.stem.replace("_cam", ""),
        "script_path": str(path),
    }


def _load_bbox_from_step(step_path: str) -> Optional[dict]:
    """
    Try to load bounding box from a STEP file via CadQuery.
    Returns {"x_mm": ..., "y_mm": ..., "z_mm": ...} or None on failure.
    """
    try:
        import cadquery as cq
        shape = cq.importers.importStep(str(step_path))
        bb = shape.val().BoundingBox()
        return {
            "x_mm": round(bb.xmax - bb.xmin, 3),
            "y_mm": round(bb.ymax - bb.ymin, 3),
            "z_mm": round(bb.zmax - bb.zmin, 3),
        }
    except Exception:
        return None


def estimate_cycle_time(
    operations: list[dict],
    material: str,
    step_path: Optional[str] = None,
    bbox_mm: Optional[dict] = None,
) -> float:
    """
    Estimate total cycle time in minutes.

    Strategy:
    1. If step_path is provided, try loading bbox via CadQuery and compute
       MRR-based estimate: (stock_volume - part_volume) / MRR.
       stock_volume = (x+6) * (y+6) * (z+4) mm³ (3mm overstock per side + 4mm Z).
       part_volume  = bbox_volume * 0.4 (40% density approximation).
    2. If bbox_mm dict is provided and CQ is unavailable, also attempt MRR-based.
    3. Otherwise fall back to per-operation-type heuristic time constants.

    Returns estimated minutes as a float.
    """
    mrr = _MRR_BY_MATERIAL.get(material, _MRR_DEFAULT)

    # ── Attempt MRR-based estimate ─────────────────────────────────────────────
    bb: Optional[dict] = None

    if step_path:
        bb = _load_bbox_from_step(step_path)

    if bb is None:
        bb = bbox_mm  # fall back to CAM-script-parsed bbox

    if bb is not None:
        try:
            x, y, z = bb["x_mm"], bb["y_mm"], bb["z_mm"]
            stock_volume = (x + 6.0) * (y + 6.0) * (z + 4.0)
            part_volume = x * y * z * 0.4
            removal_volume = max(stock_volume - part_volume, 0.0)
            cycle_min = removal_volume / mrr
            return round(cycle_min, 1)
        except (KeyError, ZeroDivisionError, TypeError):
            pass  # fall through to heuristic

    # ── Heuristic fallback ─────────────────────────────────────────────────────
    _mat_scale = {
        "aluminium_6061": 1.0, "aluminium_7075": 1.1,
        "steel_mild": 2.0, "steel_4140": 2.5, "stainless_316": 3.0,
        "x1_420i": 2.5, "inconel_718": 6.0,
        "titanium_ti6al4v": 3.5, "pla": 0.6, "abs": 0.6,
    }
    total_min = 0.0
    for op in operations:
        op_type = op.get("type", "").lower()
        matched_key = next(
            (k for k in _OP_TIME_MIN if k in op_type or op_type in k),
            None,
        )
        total_min += _OP_TIME_MIN.get(matched_key or "", 1.5)

    scale = _mat_scale.get(material, 2.0)
    return round(total_min * scale, 1)


def detect_second_op(step_path: str) -> dict:
    """
    Analyse a STEP file to determine whether a second machining operation
    (part flip) is required to reach bottom features.

    Returns:
        {required: bool, reason: str, flip_axis: str}   # flip_axis only when required=True
    """
    try:
        import cadquery as cq

        shape = cq.importers.importStep(str(step_path))
        solid = shape.val()
        bb = solid.BoundingBox()
        bbox_area = (bb.xmax - bb.xmin) * (bb.ymax - bb.ymin)
        min_area_threshold = bbox_area * 0.05

        bottom_face_area = 0.0
        for face in solid.Faces():
            normal = face.normalAt(face.Center())
            if normal.z < -0.9:
                bottom_face_area += face.Area()

        if bottom_face_area > min_area_threshold:
            return {
                "required": True,
                "reason": "bottom features require flip",
                "flip_axis": "Y",
            }
        return {
            "required": False,
            "reason": "all features accessible from top",
        }
    except Exception:
        return {
            "required": False,
            "reason": "could not analyze",
        }


def suggest_fixturing(bbox_mm: Optional[dict]) -> str:
    """
    Suggest a fixturing method based on part bounding box dimensions.

    Args:
        bbox_mm: dict with x_mm, y_mm, z_mm keys, or None.

    Returns:
        Human-readable fixturing recommendation string.
    """
    if bbox_mm is None:
        return "Verify manually"

    try:
        x = bbox_mm["x_mm"]
        y = bbox_mm["y_mm"]
        z = bbox_mm["z_mm"]
    except (KeyError, TypeError):
        return "Verify manually"

    if max(x, y) > 150.0:
        return "Fixture plate — part too large for standard vise"
    if z > x and z > y:
        return "V-blocks or custom fixture — tall part"
    if min(x, y) < 20.0:
        return "Precision vise with soft jaws — narrow part"
    return '6" vise — standard setup'


def generate_setup_sheet(
    step_path: str,
    cam_script_path: str,
    part_id: Optional[str] = None,
    machine_name: str = "Tormach 1100",
    material: Optional[str] = None,
) -> str:
    """
    Build a markdown setup sheet string from a STEP file and its CAM script.

    Args:
        step_path:        Path to the STEP file.
        cam_script_path:  Path to the generated Fusion 360 CAM script.
        part_id:          Override part identifier (defaults to STEP filename stem).
        machine_name:     CNC machine name shown in the header.
        material:         Material override; falls back to material parsed from
                          the CAM script if None.
    """
    parsed = parse_cam_script(cam_script_path)

    # Resolve part_id
    if part_id is None:
        part_id = Path(step_path).stem
    part_name = part_id  # use part_id as display name

    # Resolve material
    if material is None:
        material = parsed.get("material") or "unknown"

    # Prefer CQ-loaded bbox; fall back to CAM-script-parsed bbox
    bbox = _load_bbox_from_step(step_path) or parsed.get("bbox_mm")

    tools = parsed.get("tools", [])
    operations = parsed.get("operations", [])
    feeds = parsed.get("feeds", [])

    cycle_min = estimate_cycle_time(
        operations,
        material,
        step_path=step_path,
        bbox_mm=parsed.get("bbox_mm"),
    )

    second_op = detect_second_op(step_path)
    fixturing = suggest_fixturing(bbox)

    # Try running machinability check for the Notes section
    cam_warnings: list[str] = []
    cam_violations: list[str] = []
    try:
        from .cam_validator import run_machinability_check
        mac = run_machinability_check(step_path, material)
        cam_violations = mac.get("violations", [])
        cam_warnings = mac.get("warnings", [])
    except Exception:
        pass

    lines: list[str] = []

    lines.append(f"## Setup Sheet: {part_name}")
    lines.append(f"**Machine:** {machine_name}")
    lines.append("")

    # ── Stock ─────────────────────────────────────────────────────────────────
    lines.append("### Stock")
    if bbox:
        # Add exactly 3mm per side (6mm total) + 4mm on Z for facing stock
        sx = round(bbox["x_mm"] + 6.0, 1)
        sy = round(bbox["y_mm"] + 6.0, 1)
        sz = round(bbox["z_mm"] + 4.0, 1)
        lines.append(f"- Part Envelope: {bbox['x_mm']} x {bbox['y_mm']} x {bbox['z_mm']} mm")
        lines.append(f"- Stock Dimensions: {sx} x {sy} x {sz} mm (3mm/side + 4mm top for facing)")
    else:
        lines.append("- Stock Dimensions: unknown (STEP not parsed)")
    lines.append(f"- Material: {material}")
    lines.append("")

    # ── Fixturing ─────────────────────────────────────────────────────────────
    lines.append("### Fixturing")
    lines.append("")
    lines.append(f"- **Recommendation:** {fixturing}")
    lines.append("")

    # ── Second operation ──────────────────────────────────────────────────────
    lines.append("### Second Operation")
    lines.append("")
    if second_op.get("required"):
        lines.append(f"- **Required:** Yes — {second_op['reason']}")
        lines.append(f"- Flip axis: {second_op.get('flip_axis', 'Y')}")
    else:
        lines.append(f"- **Required:** No — {second_op['reason']}")
    lines.append("")

    # ── Tools required ────────────────────────────────────────────────────────
    lines.append("### Tools Required (in order)")
    lines.append("")
    if tools:
        lines.append("| # | Dia (mm) | Flutes | Type | RPM | Feed (mm/min) | Operation |")
        lines.append("|---|----------|--------|------|-----|--------------|-----------|")
        for t in tools:
            # Find which operations use this tool
            ops_using = [
                op["name"]
                for op in operations
                if op.get("tool") and op["tool"].get("number") == t["number"]
            ]
            op_str = ", ".join(ops_using) if ops_using else "-"
            rpm_str = str(t["rpm"]) if t.get("rpm") else "-"
            feed_str = str(t["feed_mmpm"]) if t.get("feed_mmpm") else "-"
            lines.append(
                f"| {t['number']} | {t['dia_mm']} | {t.get('flutes', '-')} "
                f"| Flat Endmill | {rpm_str} | {feed_str} | {op_str} |"
            )
    else:
        lines.append("_No tool data extracted from CAM script._")
    lines.append("")

    # ── Work offsets ──────────────────────────────────────────────────────────
    lines.append("### Work Offsets")
    lines.append("")
    lines.append("- **G54**: Part zero at stock bottom-left-top corner")
    if bbox:
        lines.append(f"  - X0: left edge of stock")
        lines.append(f"  - Y0: front edge of stock")
        lines.append(f"  - Z0: top face of stock (after facing)")
    lines.append("- Probe with edge finder or 3D taster before cutting")
    lines.append("")

    # ── Operations sequence ───────────────────────────────────────────────────
    lines.append("### Operations Sequence")
    lines.append("")
    if operations:
        for op in operations:
            tool_dia = op["tool"]["dia_mm"] if op.get("tool") else "?"
            tool_num = op["tool"]["number"] if op.get("tool") else "?"
            op_cycle = _OP_TIME_MIN.get(
                next((k for k in _OP_TIME_MIN if k in op["type"].lower()), None) or "",
                1.5,
            )
            lines.append(
                f"{op['index']}. **{op['name']}** — T{tool_num} "
                f"({tool_dia}mm) — est. {op_cycle:.0f} min"
            )
    else:
        lines.append("_No operations parsed from CAM script._")
    lines.append("")

    # ── Cycle time ────────────────────────────────────────────────────────────
    lines.append(f"### Estimated Cycle Time: {cycle_min} min")
    lines.append("")
    lines.append(
        "> Estimate based on MRR (material removal rate) for "
        f"{material} on {machine_name}. Actual time will vary with "
        "depth-of-cut and machine condition."
    )
    lines.append("")

    # ── Notes / Flags ─────────────────────────────────────────────────────────
    lines.append("### Notes / Flags")
    lines.append("")
    if cam_violations:
        lines.append("**MACHINABILITY VIOLATIONS (must fix before cutting):**")
        for v in cam_violations:
            lines.append(f"- {v}")
        lines.append("")
    if cam_warnings:
        lines.append("**Warnings:**")
        for w in cam_warnings:
            lines.append(f"- {w}")
        lines.append("")
    if not cam_violations and not cam_warnings:
        lines.append("- No machinability issues detected.")
    lines.append("")
    lines.append("**General reminders:**")
    lines.append("- Verify stock dimensions before loading program")
    lines.append("- Check tool lengths and set offsets in control")
    lines.append("- Dry-run with spindle stopped before first cut")
    lines.append("- Confirm coolant is on for all metal operations")

    return "\n".join(lines)


def write_setup_sheet(
    step_path: str,
    cam_script_path: str,
    material: str,
    out_dir: Path,
    part_id: Optional[str] = None,
    machine_name: str = "Tormach 1100",
) -> str:
    """
    Generate and write the setup sheet to:
      - out_dir/setup_sheet.md   (markdown for the operator)
      - out_dir/setup_sheet.json (structured JSON sidecar)

    Args:
        step_path:        Path to the STEP file.
        cam_script_path:  Path to the generated Fusion 360 CAM script.
        material:         Material identifier (e.g. "aluminium_6061").
        out_dir:          Output directory.
        part_id:          Override part identifier; defaults to STEP filename stem.
        machine_name:     CNC machine name shown in the header.

    Returns the markdown output file path as a string.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve part_id here so both outputs share it
    if part_id is None:
        part_id = Path(step_path).stem

    md = generate_setup_sheet(
        step_path,
        cam_script_path,
        part_id=part_id,
        machine_name=machine_name,
        material=material,
    )

    # ── Write markdown ─────────────────────────────────────────────────────────
    out_path = out_dir / "setup_sheet.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[cam_setup] Setup sheet written: {out_path}")

    # ── Build JSON sidecar ─────────────────────────────────────────────────────
    parsed = parse_cam_script(cam_script_path)
    bbox = _load_bbox_from_step(step_path) or parsed.get("bbox_mm")

    # Stock dims: 3mm per side (6mm total on each axis) + 4mm on Z
    # Fall back to a placeholder when no geometry is available so the JSON
    # output always satisfies the schema (stock_dims must be an object).
    if bbox:
        stock_dims: dict = {
            "x_mm": round(bbox["x_mm"] + 6.0, 1),
            "y_mm": round(bbox["y_mm"] + 6.0, 1),
            "z_mm": round(bbox["z_mm"] + 4.0, 1),
        }
    else:
        stock_dims = {"x_mm": 0.1, "y_mm": 0.1, "z_mm": 0.1}  # unknown — measure from part

    cycle_min = estimate_cycle_time(
        parsed.get("operations", []),
        material,
        step_path=step_path,
        bbox_mm=parsed.get("bbox_mm"),
    )

    second_op = detect_second_op(step_path)
    fixturing = suggest_fixturing(bbox)

    # Serialise tool list (drop non-JSON-safe dia_cm if present)
    tools_json = [
        {k: v for k, v in t.items() if k != "dia_cm"}
        for t in parsed.get("tools", [])
    ]

    json_data = {
        "schema_version": CAM_SETUP_SCHEMA_VERSION,
        "part_id": part_id,
        "machine_name": machine_name,
        "tools": tools_json,
        "stock_dims": stock_dims,
        "cycle_time_min_estimate": cycle_min,
        "second_op_required": bool(second_op.get("required")),
        "work_offset_recommendation": "G54: bottom-left-top corner of stock",
        "fixturing_suggestion": fixturing,
        "generated_at": datetime.utcnow().isoformat(),
    }

    json_path = out_dir / "setup_sheet.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"[cam_setup] Setup sheet JSON written: {json_path}")

    return str(out_path)
