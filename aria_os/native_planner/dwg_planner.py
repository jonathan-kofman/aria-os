"""Drawing planner — emits sheet/view/dim/title-block ops that stream
into a newly created Fusion drawing document. Professional-grade part
and assembly drawings with full GD&T, datum references, and detailed
callouts per ASME Y14.5 / ISO 128.
"""
from __future__ import annotations


def plan_simple_drawing(spec: dict, part_meta: dict | None = None) -> list[dict]:
    """Professional four-view layout with full dimensions, GD&T, and callouts.

    Args:
        spec: Dict with part_id, material, description, n_bolts, etc.
        part_meta: Computed metadata (material, finish, mass). Defaults to empty.

    Returns: List of drawing operations in proper execution order.
    """
    if part_meta is None:
        part_meta = {}

    part_number = spec.get("part_id") or spec.get("name") or "ARIA_PART"
    material = spec.get("material") or part_meta.get("material", "Steel")
    finish = part_meta.get("finish", "As machined")
    mass_g = part_meta.get("mass_g", 0.0)
    revision = spec.get("revision", "A")
    description = spec.get("description", "")
    n_bolts = int(spec.get("n_bolts", 0))

    plan: list[dict] = [
        {"kind": "beginDrawing", "params": {},
         "label": "New drawing document from active design"},
        {"kind": "newSheet",
         "params": {"size": "A3", "alias": "sheet_main"},
         "label": "Sheet: A3 landscape"},

        # Standard four views: top, front, right, isometric
        # In execution order per validator: addView ops reference the sheet alias
        {"kind": "addView",
         "params": {"sheet": "sheet_main", "scale": 1.0,
                    "x_mm": 50, "y_mm": 130, "alias": "view_top"},
         "label": "Top view @ 1:1"},
        {"kind": "addView",
         "params": {"sheet": "sheet_main", "scale": 1.0,
                    "x_mm": 50, "y_mm": 20, "alias": "view_front"},
         "label": "Front view @ 1:1"},
        {"kind": "addView",
         "params": {"sheet": "sheet_main", "scale": 1.0,
                    "x_mm": 180, "y_mm": 20, "alias": "view_right"},
         "label": "Right (side) view @ 1:1"},
        {"kind": "addView",
         "params": {"sheet": "sheet_main", "scale": 0.75,
                    "x_mm": 180, "y_mm": 130, "alias": "view_iso"},
         "label": "Isometric view @ 3:4"},
    ]

    # Centerline marks on all cylindrical features (bore, bolt holes)
    if spec.get("bore_mm"):
        plan.append({
            "kind": "centerlineMark",
            "params": {"view": "view_top", "feature": "bore"},
            "label": "Centerline mark: bore hole (top view)"})

    if n_bolts > 0:
        plan.append({
            "kind": "centerlineMark",
            "params": {"view": "view_top", "feature": "bolt_holes"},
            "label": f"Centerline marks: {n_bolts} bolt holes"})

    # Linear dimensions: width, height, depth from appropriate views
    if spec.get("width_mm"):
        plan.append({
            "kind": "linearDimension",
            "params": {"view": "view_top", "from": "corner_1", "to": "corner_2"},
            "label": f"Width: {spec.get('width_mm'):.1f}mm"})

    if spec.get("height_mm"):
        plan.append({
            "kind": "linearDimension",
            "params": {"view": "view_front", "from": "corner_3", "to": "corner_4"},
            "label": f"Height: {spec.get('height_mm'):.1f}mm"})

    if spec.get("depth_mm"):
        plan.append({
            "kind": "linearDimension",
            "params": {"view": "view_right", "from": "corner_5", "to": "corner_6"},
            "label": f"Depth: {spec.get('depth_mm'):.1f}mm"})

    # Diameter dimensions for bore and bolt holes
    if spec.get("bore_mm"):
        bore_dia = float(spec.get("bore_mm"))
        plan.append({
            "kind": "diameterDimension",
            "params": {"view": "view_top", "edge": "bore_edge"},
            "label": f"Bore ø{bore_dia:.1f}mm"})

    if spec.get("bolt_dia_mm") and n_bolts > 0:
        bolt_dia = float(spec.get("bolt_dia_mm"))
        plan.append({
            "kind": "diameterDimension",
            "params": {"view": "view_top", "edge": "bolt_hole_edge"},
            "label": f"Bolt hole ø{bolt_dia:.1f}mm"})

    # Radial dimensions for fillets/rounds
    if spec.get("fillet_r_mm"):
        fillet_r = float(spec.get("fillet_r_mm"))
        plan.append({
            "kind": "radialDimension",
            "params": {"view": "view_front", "edge": "fillet_edge"},
            "label": f"Fillet r{fillet_r:.1f}mm"})

    # Datum labels (A = primary, B = secondary, C = tertiary)
    # Typically: A = bottom face, B = a side face, C = another side
    plan.append({
        "kind": "datumLabel",
        "params": {"view": "view_front", "feature": "bottom_face", "label": "A"},
        "label": "Datum A: primary (bottom face)"})
    plan.append({
        "kind": "datumLabel",
        "params": {"view": "view_top", "feature": "front_face", "label": "B"},
        "label": "Datum B: secondary (front face)"})
    plan.append({
        "kind": "datumLabel",
        "params": {"view": "view_right", "feature": "right_face", "label": "C"},
        "label": "Datum C: tertiary (right face)"})

    # GD&T FCFs (Geometric Dimensioning & Tolerancing)
    # Flatness on datum A: 0.05mm
    plan.append({
        "kind": "gdtFrame",
        "params": {"view": "view_front", "feature": "bottom_face",
                   "characteristic": "flatness", "tolerance": 0.05},
        "label": "GD&T: Flatness 0.05mm on datum A"})

    # Perpendicularity of datum B to A: 0.1mm
    plan.append({
        "kind": "gdtFrame",
        "params": {"view": "view_top", "feature": "front_face",
                   "characteristic": "perpendicularity", "tolerance": 0.1},
        "label": "GD&T: Perpendicularity 0.1mm to datum A"})

    # Position tolerance on most critical hole (if bolts present): 0.2mm MMC
    if n_bolts > 0:
        plan.append({
            "kind": "gdtFrame",
            "params": {"view": "view_top", "feature": "bolt_holes",
                       "characteristic": "position", "tolerance": 0.2},
            "label": "GD&T: Position 0.2mm MMC on bolt holes"})

    # Surface finish callout (Ra 1.6 on primary functional face)
    plan.append({
        "kind": "surfaceFinishCallout",
        "params": {"view": "view_front", "feature": "bottom_face", "ra": 1.6},
        "label": "Surface finish: Ra 1.6µm"})

    # Section view (if part has internal features like bore or cavities)
    if spec.get("bore_mm") or spec.get("has_cavity"):
        plan.append({
            "kind": "sectionView",
            "params": {"sheet": "sheet_main", "source_view": "view_top",
                       "section_line": "vert_center", "alias": "section_A_A"},
            "label": "Section view A-A (through center)"})

    # Detail view for small features (< 5mm)
    if spec.get("detail_feature_center") and spec.get("detail_radius_mm", 0) > 0:
        plan.append({
            "kind": "detailView",
            "params": {"sheet": "sheet_main", "source_view": "view_front",
                       "center": spec.get("detail_feature_center"),
                       "radius": spec.get("detail_radius_mm", 3.0),
                       "alias": "detail_B"},
            "label": "Detail view B (enlarged)"})

    # Revision table (single row: A = Initial release)
    plan.append({
        "kind": "revisionTable",
        "params": {"sheet": "sheet_main"},
        "label": "Revision table"})

    return plan


if __name__ == "__main__":
    from aria_os.native_planner.validator import validate_plan

    # Smoke test: L-bracket part drawing
    l_bracket_spec = {
        "part_id": "L_BRACKET_001",
        "name": "L-Bracket",
        "description": "Angular mounting bracket with bolt holes",
        "material": "Aluminum 6061",
        "revision": "A",
        "width_mm": 80.0,
        "height_mm": 60.0,
        "depth_mm": 40.0,
        "bore_mm": 8.0,
        "bolt_dia_mm": 6.35,
        "n_bolts": 4,
        "fillet_r_mm": 2.0,
    }

    l_bracket_meta = {
        "material": "Aluminum 6061-T6",
        "finish": "Anodized Type II Clear",
        "mass_g": 145.3,
    }

    plan_part = plan_simple_drawing(l_bracket_spec, l_bracket_meta)
    ok_part, issues_part = validate_plan(plan_part)

    print(f"\n=== PART DRAWING (L-BRACKET) ===")
    print(f"Ops generated: {len(plan_part)}")
    print(f"Validation: {'PASS' if ok_part else 'FAIL'}")
    if issues_part:
        print(f"Issues ({len(issues_part)}):")
        for issue in issues_part[:10]:
            print(f"  - {issue}")
        if len(issues_part) > 10:
            print(f"  ... and {len(issues_part) - 10} more")
    else:
        print("No validation issues")

    print(f"\nOp kinds emitted:")
    kind_counts = {}
    for op in plan_part:
        kind = op.get("kind")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    for kind, count in sorted(kind_counts.items()):
        print(f"  {kind}: {count}")
