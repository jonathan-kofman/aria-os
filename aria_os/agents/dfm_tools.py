"""DFM analysis tool functions — geometry extraction and manufacturability checks."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def analyze_step_geometry(step_path: str) -> dict[str, Any]:
    """Load a STEP file via CadQuery and extract geometry metrics.

    Returns dict with bbox, volume, surface_area, face/edge/solid counts.
    """
    result: dict[str, Any] = {
        "bbox_mm": [0.0, 0.0, 0.0],
        "volume_mm3": 0.0,
        "surface_area_mm2": 0.0,
        "face_count": 0,
        "edge_count": 0,
        "solid_count": 0,
        "error": None,
    }

    if not Path(step_path).exists():
        result["error"] = f"STEP file not found: {step_path}"
        return result

    try:
        import cadquery as cq

        shape = cq.importers.importStep(str(step_path))
        val = shape.val()
        bb = val.BoundingBox()

        result["bbox_mm"] = [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)]
        result["face_count"] = len(val.Faces())
        result["edge_count"] = len(val.Edges())
        result["solid_count"] = len(val.Solids())

        # Volume and surface area from OCC
        try:
            from OCC.Core.GProp import GProp_GProps
            from OCC.Core.BRepGProp import brepgprop_VolumeProperties, brepgprop_SurfaceProperties

            v_props = GProp_GProps()
            brepgprop_VolumeProperties(val.wrapped, v_props)
            result["volume_mm3"] = round(abs(v_props.Mass()), 3)

            s_props = GProp_GProps()
            brepgprop_SurfaceProperties(val.wrapped, s_props)
            result["surface_area_mm2"] = round(abs(s_props.Mass()), 3)
        except ImportError:
            # Fallback: estimate from bbox
            dims = result["bbox_mm"]
            result["volume_mm3"] = round(dims[0] * dims[1] * dims[2] * 0.6, 3)  # ~60% fill
            sa = 2 * (dims[0] * dims[1] + dims[1] * dims[2] + dims[0] * dims[2])
            result["surface_area_mm2"] = round(sa, 3)
        except Exception:
            dims = result["bbox_mm"]
            result["volume_mm3"] = round(dims[0] * dims[1] * dims[2] * 0.6, 3)
            sa = 2 * (dims[0] * dims[1] + dims[1] * dims[2] + dims[0] * dims[2])
            result["surface_area_mm2"] = round(sa, 3)

    except ImportError:
        result["error"] = "cadquery not available"
    except Exception as exc:
        result["error"] = str(exc)[:300]

    return result


def estimate_wall_thickness(step_path: str) -> float:
    """Estimate average wall thickness using volume/surface_area * 2 heuristic.

    For a hollow shell, V ~ SA * t/2, so t ~ 2V/SA.
    For a solid block, this gives roughly the smallest dimension.
    Returns thickness in mm, or -1.0 on error.
    """
    geo = analyze_step_geometry(step_path)
    if geo.get("error"):
        return -1.0

    sa = geo["surface_area_mm2"]
    vol = geo["volume_mm3"]

    if sa < 0.01:
        return -1.0

    return round(2.0 * vol / sa, 3)


def check_undercuts(step_path: str) -> dict[str, Any]:
    """Check for undercut faces — reuses cam_validator if available.

    Returns dict with undercut_count, axis_classification, and details.
    """
    result: dict[str, Any] = {
        "undercut_count": 0,
        "axis_classification": "3axis",
        "faces_checked": 0,
        "details": [],
    }

    # Try existing cam_validator (two possible locations)
    _cam_check_undercuts = None
    _cam_classify_axes = None
    try:
        from ..cam.cam_validator import check_undercuts as _cam_check_undercuts
        from ..cam.cam_validator import classify_machining_axes as _cam_classify_axes
    except ImportError:
        try:
            from ..cam_validator import check_undercuts as _cam_check_undercuts
            from ..cam_validator import classify_machining_axes as _cam_classify_axes
        except ImportError:
            pass

    if _cam_check_undercuts and _cam_classify_axes:
        try:
            undercut_results = _cam_check_undercuts(str(step_path))
            undercut_faces = [r for r in undercut_results if r.get("is_undercut")]
            result["undercut_count"] = len(undercut_faces)
            result["faces_checked"] = len(undercut_results)
            result["axis_classification"] = _cam_classify_axes(undercut_results)[0]
            result["details"] = undercut_results[:10]  # truncate
            return result
        except Exception:
            pass

    # Fallback: basic face-normal analysis via CadQuery/OCC
    try:
        import cadquery as cq
        from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
        from OCC.Core.BRepTools import breptools_UVBounds
        from OCC.Core.gp import gp_Pnt, gp_Vec

        shape = cq.importers.importStep(str(step_path))
        faces = shape.faces().vals()
        result["faces_checked"] = len(faces)

        cardinal_dirs = [
            (0, 0, 1), (0, 0, -1),
            (0, 1, 0), (0, -1, 0),
            (1, 0, 0), (-1, 0, 0),
        ]

        undercut_count = 0
        for face in faces:
            adaptor = BRepAdaptor_Surface(face.wrapped)
            umin, umax, vmin, vmax = breptools_UVBounds(face.wrapped)
            umid = (umin + umax) / 2
            vmid = (vmin + vmax) / 2

            pt = gp_Pnt()
            d1u = gp_Vec()
            d1v = gp_Vec()
            adaptor.D1(umid, vmid, pt, d1u, d1v)

            normal = d1u.Crossed(d1v)
            if normal.Magnitude() < 1e-10:
                continue
            normal.Normalize()
            nx, ny, nz = normal.X(), normal.Y(), normal.Z()

            # Reachable if normal aligns with any cardinal direction (dot > 0.1)
            reachable = any(
                (nx * dx + ny * dy + nz * dz) > 0.1
                for dx, dy, dz in cardinal_dirs
            )
            if not reachable:
                undercut_count += 1

        result["undercut_count"] = undercut_count
        if undercut_count == 0:
            result["axis_classification"] = "3axis"
        elif undercut_count <= 4:
            result["axis_classification"] = "4axis"
        else:
            result["axis_classification"] = "5axis"

    except ImportError:
        result["details"] = [{"message": "cadquery/OCC not available"}]
    except Exception as exc:
        result["details"] = [{"message": f"undercut check error: {exc}"}]

    return result


def classify_machining_axes(step_path: str) -> str:
    """Return '3axis', '4axis', or '5axis' based on undercut analysis."""
    uc = check_undercuts(step_path)
    return uc["axis_classification"]


def estimate_feature_complexity(face_count: int, edge_count: int) -> str:
    """Classify feature complexity based on face and edge counts.

    Returns 'simple', 'moderate', or 'complex'.
    """
    # Simple heuristic: more faces/edges = more complex
    total = face_count + edge_count
    if total < 30:
        return "simple"
    elif total < 120:
        return "moderate"
    else:
        return "complex"
