"""
Parse arbitrary part descriptions into a structured spec for CadQuery generation.
No API required — regex and heuristics to extract dimensions and features.
"""
import re
from typing import Any, List, Optional


def parse(goal: str) -> dict[str, Any]:
    """
    Extract dimensions and features from goal text.
    Returns spec: base_shape (type, dimensions), center_hole, bolt_circle, wall_mm, slot, expected_bbox.
    """
    g = (goal or "").lower()
    spec: dict[str, Any] = {
        "base_shape": {"type": "box", "width": 100.0, "height": 100.0, "depth": 100.0},
        "center_hole": None,
        "bolt_circle": None,
        "wall_mm": None,
        "slot": None,
        "expected_bbox": None,
    }

    # All numbers in mm: "50mm", "10.5 mm", "50mm x 30mm x 8mm"
    def find_mm(s: str) -> List[float]:
        return [float(m.group(1)) for m in re.finditer(r"(\d+\.?\d*)\s*mm", s, re.I)]

    nums = find_mm(g)

    # Box: "50mm x 30mm x 8mm" or "50 x 30 x 8 mm" or "rectangular ... 50 ... 30 ... 8"
    box_match = re.search(r"(\d+\.?\d*)\s*mm?\s*[x×]\s*(\d+\.?\d*)\s*mm?\s*[x×]\s*(\d+\.?\d*)\s*mm?", g)
    if box_match:
        w, h, d = float(box_match.group(1)), float(box_match.group(2)), float(box_match.group(3))
        spec["base_shape"] = {"type": "box", "width": w, "height": h, "depth": d}
        spec["expected_bbox"] = (w, h, d)
    elif "spacer" in g or "plate" in g or "rectangular" in g and len(nums) >= 3:
        # First three numbers as L, W, H
        spec["base_shape"] = {"type": "box", "width": nums[0], "height": nums[1], "depth": nums[2]}
        spec["expected_bbox"] = (nums[0], nums[1], nums[2])

    # Cylinder: "80mm outer diameter", "20mm tall" — do NOT match "10mm diameter" (hole description)
    cyl_od = re.search(r"(\d+\.?\d*)\s*mm\s*outer\s+(?:diameter|dia)", g) or re.search(r"outer\s+(?:diameter|dia)\s*(\d+\.?\d*)\s*mm", g)
    cyl_h = re.search(r"(\d+\.?\d*)\s*mm\s*(?:tall|high|thick|long)", g) or re.search(r"(?:height|tall|thick)\s*(\d+\.?\d*)\s*mm", g)
    if (cyl_od or "cylindrical" in g or " cylinder " in g) or ("flange" in g and "outer" in g):
        od = float(cyl_od.group(1)) if cyl_od else (nums[0] if nums else 80.0)
        height = float(cyl_h.group(1)) if cyl_h else (nums[1] if len(nums) > 1 else 20.0)
        spec["base_shape"] = {"type": "cylinder", "diameter": od, "height": height}
        spec["expected_bbox"] = (od, od, height)

    # Wall thickness / hollow
    wall = re.search(r"(\d+\.?\d*)\s*mm\s*wall", g) or re.search(r"wall\s*(?:thickness)?\s*(\d+\.?\d*)\s*mm", g)
    if wall:
        spec["wall_mm"] = float(wall.group(1))
    if "hollow" in g and nums:
        spec["wall_mm"] = spec["wall_mm"] or (nums[-1] if nums else 6.0)

    # Center hole: "center hole 10mm diameter through" or "center bore 42mm"
    center_hole = re.search(r"center\s+(?:hole|bore)\s*(\d+\.?\d*)\s*mm", g) or re.search(r"(\d+\.?\d*)\s*mm\s*(?:diameter)?\s*through\s+all", g)
    if center_hole:
        spec["center_hole"] = {"diameter": float(center_hole.group(1)), "through": True}
    if "through all" in g or "through" in g and "hole" in g and nums:
        # Use first hole-sized number if not already set
        for n in nums:
            if n < (spec["base_shape"].get("width", 100) or spec["base_shape"].get("diameter", 100)):
                spec["center_hole"] = spec["center_hole"] or {"diameter": n, "through": True}
                break

    # Bolt circle: "4 bolt holes on 65mm bolt circle", "each 5.5mm diameter"
    bcd = re.search(r"(\d+\.?\d*)\s*mm\s*bolt\s*circle", g) or re.search(r"bolt\s*circle\s*(?:diameter)?\s*(\d+\.?\d*)\s*mm", g)
    hole_dia = re.search(r"each\s*(\d+\.?\d*)\s*mm", g) or re.search(r"(\d+\.?\d*)\s*mm\s*diameter\s*(?:holes?|each)", g)
    if bcd:
        count = 4
        m = re.search(r"(\d+)\s*(?:x|bolt|holes?)", g)
        if m:
            count = int(m.group(1))
        spec["bolt_circle"] = {
            "bolt_circle_diameter": float(bcd.group(1)),
            "count": count,
            "hole_diameter": float(hole_dia.group(1)) if hole_dia else 6.5,
        }

    # Slot: "30mm slot" or "slot ... width ... length"
    slot_w = re.search(r"(\d+\.?\d*)\s*mm\s*(?:wide?\s+)?slot", g) or re.search(r"slot\s*(\d+\.?\d*)\s*mm", g)
    if slot_w:
        w = float(slot_w.group(1))
        spec["slot"] = {"width": w, "length": max(w * 2, 40.0), "depth": 10.0}

    # Center bore (cylinder): "center bore 42mm"
    if not spec["center_hole"] and ("center bore" in g or "centre bore" in g):
        cb = re.search(r"center\s+bore\s*(\d+\.?\d*)\s*mm", g) or re.search(r"(\d+\.?\d*)\s*mm\s*(?:center\s+)?bore", g)
        if cb:
            spec["center_hole"] = {"diameter": float(cb.group(1)), "through": True}

    return spec
