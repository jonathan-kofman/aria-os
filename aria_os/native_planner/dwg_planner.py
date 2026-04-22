"""Drawing planner — emits sheet/view/dim/title-block ops that stream
into a newly created Fusion drawing document. The planner itself is
geometry-agnostic — it just lays out standard views of whatever is
the active design.
"""
from __future__ import annotations


def plan_simple_drawing(spec: dict) -> list[dict]:
    """Standard four-view + title block layout on an A3 sheet.

    Front / top / right views plus an isometric, at 1:1 scale. Exact
    dimensions are left for a later op set — MVP populates the base
    view geometry that users typically want first.
    """
    part_number = spec.get("part_id") or spec.get("name") or "ARIA_PART"
    material    = spec.get("material") or ""
    revision    = spec.get("revision", "A")
    plan: list[dict] = [
        {"kind": "beginDrawing", "params": {},
         "label": "New drawing document from active design"},
        {"kind": "newSheet",
         "params": {"size": "A3", "name": part_number},
         "label": "Sheet: A3 landscape, mm units"},
        {"kind": "addView",
         "params": {"view_type": "front", "scale": 1.0,
                    "x_mm": 80, "y_mm": 120, "alias": "view_front"},
         "label": "Front view @ 1:1"},
        {"kind": "addView",
         "params": {"view_type": "top", "scale": 1.0,
                    "x_mm": 80, "y_mm": 240, "alias": "view_top"},
         "label": "Top view @ 1:1"},
        {"kind": "addView",
         "params": {"view_type": "right", "scale": 1.0,
                    "x_mm": 220, "y_mm": 120, "alias": "view_right"},
         "label": "Right view @ 1:1"},
        {"kind": "addView",
         "params": {"view_type": "iso", "scale": 0.75,
                    "x_mm": 220, "y_mm": 240, "alias": "view_iso"},
         "label": "Isometric view @ 3:4"},
        {"kind": "addTitleBlock",
         "params": {"part_number": part_number,
                    "description": spec.get("description", ""),
                    "material": material,
                    "revision": revision},
         "label": f"Title block: {part_number} · rev {revision}"},
    ]
    return plan
