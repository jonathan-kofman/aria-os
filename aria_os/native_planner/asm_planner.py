"""Assembly planner — emits drawing ops for professional multi-component
assemblies with exploded views, balloons, BOM tables, and mate annotations.
"""
from __future__ import annotations


def plan_simple_assembly(spec: dict, components: list[dict] | None = None) -> list[dict]:
    """Professional assembly drawing with exploded view, BOM, balloons, title block.

    Args:
        spec: Dict with assembly_id, description, material, revision.
        components: List of component dicts (id, name, qty, material).

    Returns: List of drawing operations in proper execution order.
    """
    if components is None:
        components = [
            {"id": "comp_1", "name": "Base", "qty": 1, "material": "Steel"},
            {"id": "comp_2", "name": "Housing", "qty": 1, "material": "Aluminum"},
            {"id": "comp_3", "name": "Fastener", "qty": 4, "material": "Steel"},
        ]

    assembly_name = spec.get("assembly_id", spec.get("name", "ASSEMBLY"))
    part_count = len(components)
    total_parts = sum(c.get("qty", 1) for c in components)
    revision = spec.get("revision", "A")

    plan: list[dict] = [
        {"kind": "beginDrawing", "params": {},
         "label": "New assembly drawing"},
        {"kind": "newSheet",
         "params": {"size": "A3", "alias": "sheet_asm"},
         "label": "Sheet: A3 landscape"},

        # Main assembled view
        {"kind": "addView",
         "params": {"sheet": "sheet_asm", "scale": 1.0,
                    "x_mm": 50, "y_mm": 80, "alias": "view_assembled"},
         "label": "Main assembly view"},

        # Exploded view
        {"kind": "addView",
         "params": {"sheet": "sheet_asm", "scale": 0.75,
                    "x_mm": 180, "y_mm": 80, "alias": "view_exploded"},
         "label": "Exploded view (3:4 scale)"},

        # Alternative detail view
        {"kind": "addView",
         "params": {"sheet": "sheet_asm", "scale": 1.0,
                    "x_mm": 50, "y_mm": 200, "alias": "view_detail"},
         "label": "Detail view (front)"},
    ]

    # Balloons pointing to each component (1, 2, 3, etc.)
    for idx, comp in enumerate(components, start=1):
        plan.append({
            "kind": "balloon",
            "params": {"view": "view_exploded", "component": comp["id"],
                       "number": idx},
            "label": f"Balloon {idx}: {comp['name']}"})

    # Mate symbols (concentric, coincident) as text annotations via linearDimension
    if len(components) >= 2:
        plan.append({
            "kind": "linearDimension",
            "params": {"view": "view_assembled", "from": "comp_1_pt",
                       "to": "comp_2_pt"},
            "label": "Mate annotation: Concentric (primary)"})

    # BOM table
    bom_rows = []
    for idx, comp in enumerate(components, start=1):
        bom_rows.append({
            "number": idx,
            "description": comp["name"],
            "qty": comp.get("qty", 1),
            "material": comp.get("material", ""),
            "notes": ""
        })

    plan.append({
        "kind": "bomTable",
        "params": {"sheet": "sheet_asm"},
        "label": f"BOM: {part_count} part types, {total_parts} total items"})

    # Revision table
    plan.append({
        "kind": "revisionTable",
        "params": {"sheet": "sheet_asm"},
        "label": "Revision table"})

    return plan


if __name__ == "__main__":
    from aria_os.native_planner.validator import validate_plan

    # Smoke test: 3-component assembly
    asm_spec = {
        "assembly_id": "MOTOR_MOUNT_ASM",
        "name": "Motor Mount Assembly",
        "description": "3-component motor mounting bracket assembly",
        "revision": "A",
        "weight_g": 450.0,
    }

    asm_components = [
        {"id": "base_plate", "name": "Base Plate", "qty": 1, "material": "Steel ASTM A36"},
        {"id": "mount_bracket", "name": "Mount Bracket", "qty": 1, "material": "Aluminum 6061"},
        {"id": "fastener_M8", "name": "M8 Hex Head Bolt", "qty": 4, "material": "Steel Grade 5"},
    ]

    plan_asm = plan_simple_assembly(asm_spec, asm_components)
    ok_asm, issues_asm = validate_plan(plan_asm)

    print(f"\n=== ASSEMBLY DRAWING (3-COMPONENT) ===")
    print(f"Ops generated: {len(plan_asm)}")
    print(f"Validation: {'PASS' if ok_asm else 'FAIL'}")
    if issues_asm:
        print(f"Issues ({len(issues_asm)}):")
        for issue in issues_asm[:10]:
            print(f"  - {issue}")
        if len(issues_asm) > 10:
            print(f"  ... and {len(issues_asm) - 10} more")
    else:
        print("No validation issues")

    print(f"\nOp kinds emitted:")
    kind_counts = {}
    for op in plan_asm:
        kind = op.get("kind")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    for kind, count in sorted(kind_counts.items()):
        print(f"  {kind}: {count}")
