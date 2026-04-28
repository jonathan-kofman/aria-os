"""autocad_routing.py — Goal-to-AutoCAD detection for auto-router.

Detects whether a natural-language goal is suitable for AutoCAD (2D/3D drawing,
dimensioning, GD&T) vs. other CAD tools (SolidWorks, Rhino, Fusion360, KiCad).

Industries that benefit from AutoCAD:
  - Civil Engineering (site plans, foundation plans)
  - Structural Engineering (building sections, elevation drawings)
  - Architecture (floor plans, detail callouts)
  - MEP (electrical DWG schematics, HVAC layout, P&ID)
  - Mechanical 2D drafting (any technical drawing)

Used by dispatcher._auto_detect_mode() to route to the right CAD bridge.
"""


def is_autocad_goal(goal: str) -> bool:
    """Return True if the goal is better suited for AutoCAD than other CAD tools.

    Args:
        goal: Natural language description of the part/drawing to create.

    Returns:
        True if AutoCAD should be the primary CAD tool; False otherwise.
    """
    goal_lower = goal.lower()

    # Civil/structural drawings — top priority for AutoCAD
    civil_keywords = [
        "site plan",
        "foundation plan",
        "foundation drawing",
        "floor plan",
        "building section",
        "elevation drawing",
        "structural drawing",
        "structural section",
        "cross section",
        "cross-section",
        "survey plan",
        "site survey",
        "soil profile",
        "grade plan",
        "grading plan",
        "contour drawing",
        "contour plan",
    ]

    # Architecture — floor plans, elevations, details
    architecture_keywords = [
        "architectural drawing",
        "arch drawing",
        "arch plan",
        "floor layout",
        "room layout",
        "building layout",
        "detail drawing",
        "construction detail",
        "construction drawing",
        "wall section",
        "door schedule",
        "window schedule",
        "finish schedule",
        "reflected ceiling plan",
        "ceiling plan",
    ]

    # MEP — electrical, HVAC, plumbing schematics
    mep_keywords = [
        "electrical schematic",
        "electrical dwg",
        "electrical drawing",
        "electrical plan",
        "power plan",
        "lighting plan",
        "hvac layout",
        "hvac plan",
        "hvac drawing",
        "duct drawing",
        "piping diagram",
        "p&id",
        "pid diagram",
        "piping schematic",
        "plumbing plan",
        "plumbing layout",
        "riser diagram",
        "riser section",
        "equipment schedule",
        "equipment layout",
    ]

    # 2D drafting — generic technical drawings
    drafting_keywords = [
        "2d drawing",
        "2d technical drawing",
        "technical drawing",
        "2d plan",
        "orthographic drawing",
        "orthographic view",
        "blueprint",
        "dwg",
        "autocad",
        "acad",
        "dimensioned drawing",
        "dimensioned plan",
        "scale drawing",
    ]

    # GD&T and dimensioning specific
    gdt_keywords = [
        "gd&t",
        "gdnt",
        "geometric dimension",
        "tolerance frame",
        "datum reference",
        "position tolerance",
        "runout tolerance",
        "perpendicularity",
        "flatness tolerance",
    ]

    # Combine all keyword lists
    all_keywords = (
        civil_keywords +
        architecture_keywords +
        mep_keywords +
        drafting_keywords +
        gdt_keywords
    )

    # Check for exact keyword matches (case-insensitive substring search)
    for keyword in all_keywords:
        if keyword in goal_lower:
            return True

    # Fallback: if goal mentions "dwg", "drawing", "plan", "layout"
    # and does NOT mention 3D keywords, assume AutoCAD
    generic_drawing_words = ["drawing", "plan", "layout", "schematic", "diagram"]
    is_drawing = any(word in goal_lower for word in generic_drawing_words)

    # Exclude if goal mentions 3D keywords (those go to SolidWorks, Fusion, Rhino)
    exclude_3d_keywords = [
        "3d model",
        "3d solid",
        "extrude",
        "revolve",
        "sweep",
        "loft",
        "weld",
        "assembly",
        "part",
        "mechanism",
        "machine",
        "product",
        "prototype",
        "casting",
        "forging",
        "sheet metal",
        "stamping",
        "cnc",
        "cam",
        "toolpath",
        "render",
        "photorealistic",
    ]

    has_3d_keyword = any(kw in goal_lower for kw in exclude_3d_keywords)

    return is_drawing and not has_3d_keyword


if __name__ == "__main__":
    # Smoke test: print classification for sample goals
    test_goals = [
        "site plan for a 2-acre parcel",
        "foundation plan with soil profile",
        "electrical schematic for an office building",
        "floor plan of a 3-bedroom house",
        "HVAC layout with ductwork",
        "3D model of a turbine impeller",
        "orthographic drawing with GD&T",
        "SolidWorks assembly of a pump",
        "AutoCAD 2D floor layout",
        "Rhino surface modeling",
        "structural cross-section of a building",
        "dimensioned technical drawing",
        "elevation drawing of a facade",
        "MEP plan with equipment schedule",
    ]

    print("AutoCAD Goal Detection (Smoke Test)\n")
    print(f"{'Goal':<50} | {'AutoCAD?':<8}")
    print("-" * 60)
    for goal in test_goals:
        result = is_autocad_goal(goal)
        print(f"{goal:<50} | {str(result):<8}")
