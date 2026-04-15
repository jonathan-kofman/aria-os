"""
aria_os.visual_qa.dxf_verify — deterministic checks for sheet-metal DXFs.

Verifies that a flat-pattern DXF from ``aria_os.sheet_metal_unfold``
has the expected layers, bounding box, and hole count. Runs independent
of any vision LLM — pure geometry + ezdxf. Returns a structured
pass/fail result with a confidence score computed from the ratio of
checks that passed.

Part of the reusable ``aria_os.visual_qa`` visual verification
framework. Never raises — on failure returns a dict with
``passed=False`` and an ``error`` entry in ``checks``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def verify_sheet_metal_dxf(
    dxf_path: str | Path,
    expected_bbox_mm: Optional[tuple[float, float]] = None,
    expected_holes: int = 0,
    bbox_tolerance: float = 0.05,
) -> dict[str, Any]:
    """Check a sheet-metal flat-pattern DXF against expected dimensions.

    Checks performed:
        1. File exists and is readable
        2. OUTLINE layer exists and has at least one entity
        3. BEND layer entity count is reported (informational)
        4. HOLES layer entity count matches ``expected_holes``
        5. OUTLINE bounding box matches ``expected_bbox_mm`` within
           ``bbox_tolerance`` (default ±5%)

    Args:
        dxf_path: path to the DXF.
        expected_bbox_mm: (width, height) in mm, or None to skip.
        expected_holes: number of hole entities on HOLES layer.
        bbox_tolerance: fractional tolerance on bbox checks (0.05 = 5%).

    Returns:
        {
          "passed": bool,
          "confidence": float (0..1),
          "checks": [{"name","ok","detail"}, ...],
          "bbox": {...} | None,
          "layer_counts": {...},
        }
    """
    dxf_path = Path(dxf_path)
    checks: list[dict[str, Any]] = []

    if not dxf_path.is_file():
        return {
            "passed": False,
            "confidence": 0.0,
            "checks": [_check("file_exists", False, str(dxf_path))],
            "bbox": None,
            "layer_counts": {},
            "error": f"dxf not found: {dxf_path}",
        }
    checks.append(_check("file_exists", True, str(dxf_path)))

    try:
        import ezdxf  # type: ignore
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        checks.append(_check("readable", False, str(exc)))
        return {
            "passed": False,
            "confidence": 0.0,
            "checks": checks,
            "bbox": None,
            "layer_counts": {},
            "error": f"ezdxf readfile failed: {exc}",
        }
    checks.append(_check("readable", True, ""))

    msp = doc.modelspace()
    layer_counts: dict[str, int] = {}
    # Accumulate bbox for OUTLINE layer specifically.
    ox_min = oy_min = float("inf")
    ox_max = oy_max = float("-inf")

    def _extend_bbox(x: float, y: float) -> None:
        nonlocal ox_min, oy_min, ox_max, oy_max
        if x < ox_min: ox_min = x
        if y < oy_min: oy_min = y
        if x > ox_max: ox_max = x
        if y > oy_max: oy_max = y

    for ent in msp:
        layer = getattr(ent.dxf, "layer", "0")
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        if layer != "OUTLINE":
            continue
        etype = ent.dxftype()
        try:
            if etype == "LINE":
                _extend_bbox(float(ent.dxf.start[0]), float(ent.dxf.start[1]))
                _extend_bbox(float(ent.dxf.end[0]), float(ent.dxf.end[1]))
            elif etype == "LWPOLYLINE":
                for p in ent.get_points("xy"):
                    _extend_bbox(float(p[0]), float(p[1]))
            elif etype == "POLYLINE":
                for v in ent.vertices:
                    _extend_bbox(float(v.dxf.location[0]), float(v.dxf.location[1]))
            elif etype == "CIRCLE":
                cx = float(ent.dxf.center[0])
                cy = float(ent.dxf.center[1])
                r = float(ent.dxf.radius)
                _extend_bbox(cx - r, cy - r)
                _extend_bbox(cx + r, cy + r)
            elif etype == "ARC":
                cx = float(ent.dxf.center[0])
                cy = float(ent.dxf.center[1])
                r = float(ent.dxf.radius)
                _extend_bbox(cx - r, cy - r)
                _extend_bbox(cx + r, cy + r)
        except Exception:
            continue

    outline_count = layer_counts.get("OUTLINE", 0)
    bend_count = layer_counts.get("BEND", 0)
    holes_count = layer_counts.get("HOLES", 0)

    checks.append(_check(
        "outline_layer_has_geometry",
        outline_count > 0,
        f"{outline_count} entities on OUTLINE",
    ))
    checks.append(_check(
        "bend_layer_present",
        True,
        f"{bend_count} entities on BEND (informational)",
    ))
    checks.append(_check(
        "holes_count_matches_expected",
        holes_count == expected_holes,
        f"expected={expected_holes} actual={holes_count}",
    ))

    bbox: Optional[dict[str, float]] = None
    if outline_count > 0 and ox_min != float("inf"):
        bbox = {
            "xmin": ox_min, "ymin": oy_min,
            "xmax": ox_max, "ymax": oy_max,
            "width": ox_max - ox_min,
            "height": oy_max - oy_min,
        }

    if expected_bbox_mm is not None:
        exp_w, exp_h = expected_bbox_mm
        if bbox is None:
            checks.append(_check("bbox_matches_expected", False, "no OUTLINE bbox"))
        else:
            w_err = abs(bbox["width"] - exp_w) / max(exp_w, 1e-6)
            h_err = abs(bbox["height"] - exp_h) / max(exp_h, 1e-6)
            ok = (w_err <= bbox_tolerance) and (h_err <= bbox_tolerance)
            checks.append(_check(
                "bbox_matches_expected",
                ok,
                f"expected=({exp_w:.2f},{exp_h:.2f}) "
                f"actual=({bbox['width']:.2f},{bbox['height']:.2f}) "
                f"err=({w_err*100:.1f}%,{h_err*100:.1f}%) tol={bbox_tolerance*100:.0f}%",
            ))

    total = len(checks)
    passed_count = sum(1 for c in checks if c["ok"])
    confidence = passed_count / total if total > 0 else 0.0
    # Any hard failure on a gating check zeroes the pass flag.
    gating_names = {"file_exists", "readable", "outline_layer_has_geometry",
                    "holes_count_matches_expected", "bbox_matches_expected"}
    passed = all(c["ok"] for c in checks if c["name"] in gating_names)

    return {
        "passed": passed,
        "confidence": round(confidence, 3),
        "checks": checks,
        "bbox": bbox,
        "layer_counts": layer_counts,
    }
