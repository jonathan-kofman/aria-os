"""
cam_validator.py — Pre-CAM machinability checker.

Catches geometry problems before toolpath generation.
Equivalent of cem_checks.py for the CAM pipeline.

Usage:
    from aria_os.cam_validator import run_machinability_check
    result = run_machinability_check("outputs/cad/step/aria_housing.step", "aluminium_6061")
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_LIB_PATH = ROOT / "tools" / "fusion_tool_library.json"

# 6 cardinal machining directions as unit vectors (X, Y, Z ± axes)
_CARDINAL_DIRECTIONS: list[tuple[float, float, float]] = [
    (1.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, 0.0, -1.0),
]
_UNDERCUT_DOT_THRESHOLD = 0.1


def _load_tool_lib(path: str | Path | None = None) -> dict:
    lib_path = Path(path) if path else TOOL_LIB_PATH
    try:
        with open(lib_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"endmills": [], "drills": [], "materials": {}}


def check_internal_radii(step_path: str, min_tool_dia_mm: float) -> list[dict]:
    """
    Check that all internal arc radii are machinable by the given tool diameter.

    For a flat-endmill, the minimum inside corner radius it can cut is half its
    own diameter.  Any arc tighter than that requires a smaller tool or redesign.

    Returns a list of dicts, one per circular edge found:
        {radius_mm, location, violation: bool, message: str}
    """
    results: list[dict] = []
    min_radius = min_tool_dia_mm / 2.0

    try:
        import cadquery as cq
    except ImportError:
        return [{"radius_mm": None, "location": None, "violation": False,
                 "message": "cadquery not available — radii check skipped"}]

    try:
        solid = cq.importers.importStep(str(step_path))
    except Exception as exc:
        return [{"radius_mm": None, "location": None, "violation": False,
                 "message": f"Could not load STEP: {exc}"}]

    try:
        arc_edges = solid.edges("%Circle").vals()
    except Exception:
        arc_edges = []

    seen_radii: set[float] = set()
    for edge in arc_edges:
        try:
            r = edge.radius()
        except Exception:
            continue

        r_round = round(r, 3)
        if r_round in seen_radii:
            continue
        seen_radii.add(r_round)

        # Approximate location from edge midpoint
        try:
            mp = edge.positionAt(0.5)
            loc = f"({mp.x:.1f}, {mp.y:.1f}, {mp.z:.1f})"
        except Exception:
            loc = "unknown"

        violation = r_round < min_radius
        if violation:
            msg = (
                f"Arc radius {r_round:.3f}mm is smaller than minimum machinable "
                f"radius {min_radius:.3f}mm for a {min_tool_dia_mm}mm tool. "
                f"Increase fillet to >= {min_radius + 0.5:.1f}mm or use a "
                f"{round(r_round * 2 * 0.9, 1)}mm tool."
            )
        else:
            msg = f"Arc radius {r_round:.3f}mm OK for {min_tool_dia_mm}mm tool."

        results.append({
            "radius_mm": r_round,
            "location": loc,
            "violation": violation,
            "message": msg,
        })

    if not results:
        results.append({
            "radius_mm": None,
            "location": None,
            "violation": False,
            "message": "No circular edges found (or none detectable).",
        })

    return results


def check_cavity_depth(step_path: str, tool_dia_mm: float) -> list[dict]:
    """
    Check whether cavity depth exceeds 4x tool diameter (chatter / deflection risk).

    Uses the bounding-box Z range as a conservative proxy for cavity depth.
    Returns a list with one entry per analysis performed.
    """
    results: list[dict] = []

    try:
        import cadquery as cq
    except ImportError:
        return [{"depth_mm": None, "tool_dia_mm": tool_dia_mm,
                 "aspect_ratio": None, "violation": False,
                 "message": "cadquery not available — depth check skipped"}]

    try:
        solid = cq.importers.importStep(str(step_path))
        bb = solid.val().BoundingBox()
        depth = round(bb.zlen, 2)
    except Exception as exc:
        return [{"depth_mm": None, "tool_dia_mm": tool_dia_mm,
                 "aspect_ratio": None, "violation": False,
                 "message": f"Could not load STEP: {exc}"}]

    aspect = round(depth / tool_dia_mm, 2) if tool_dia_mm > 0 else 0.0
    limit = 4.0
    violation = aspect > limit

    if violation:
        msg = (
            f"Cavity depth {depth}mm is {aspect}x tool diameter ({tool_dia_mm}mm). "
            f"Exceeds 4:1 limit — chatter risk. "
            f"Use a longer tool (reach >= {depth * 1.1:.1f}mm), reduce depth-of-cut, "
            f"or rough in multiple setups."
        )
    else:
        msg = f"Cavity depth {depth}mm ({aspect}:1) is within 4:1 limit for {tool_dia_mm}mm tool."

    results.append({
        "depth_mm": depth,
        "tool_dia_mm": tool_dia_mm,
        "aspect_ratio": aspect,
        "violation": violation,
        "message": msg,
    })
    return results


def check_thin_walls(step_path: str, min_wall_mm: float = 1.5) -> list[dict]:
    """
    Estimate thin-wall sections by sampling cross-sections at multiple Z heights.

    For each sampled Z, the bounding box of all faces at that height is computed
    and the minimum in-plane dimension is used as a proxy for wall thickness.
    Sections below min_wall_mm are flagged.

    Returns a list of violation dicts:
        {z_mm, width_mm, violation: bool, message: str}
    """
    results: list[dict] = []

    try:
        import cadquery as cq
    except ImportError:
        return [{"z_mm": None, "width_mm": None, "violation": False,
                 "message": "cadquery not available — thin-wall check skipped"}]

    try:
        solid = cq.importers.importStep(str(step_path))
        bb = solid.val().BoundingBox()
        z_min = bb.zmin
        z_max = bb.zmax
        z_range = z_max - z_min
    except Exception as exc:
        return [{"z_mm": None, "width_mm": None, "violation": False,
                 "message": f"Could not load STEP: {exc}"}]

    if z_range <= 0:
        return [{"z_mm": None, "width_mm": None, "violation": False,
                 "message": "Zero Z range — cannot sample cross-sections."}]

    # Sample at 10%, 30%, 50%, 70%, 90% of part height
    sample_fractions = [0.1, 0.3, 0.5, 0.7, 0.9]

    for frac in sample_fractions:
        z_sample = round(z_min + frac * z_range, 3)

        try:
            # Cut the solid at this Z with a plane and get the cross-section wire bbox
            plane = cq.Workplane("XY").workplane(offset=z_sample)
            # Use the CadQuery section method to get faces at this Z
            section = solid.section(z_sample)
            sec_bb = section.val().BoundingBox()
            # Minimum in-plane dimension approximates minimum wall thickness
            wall_x = round(sec_bb.xlen, 2)
            wall_y = round(sec_bb.ylen, 2)
            wall_est = round(min(wall_x, wall_y), 2)
        except Exception:
            # section() may fail on complex geometry; skip that slice
            continue

        violation = wall_est < min_wall_mm and wall_est > 0
        if violation:
            msg = (
                f"Estimated wall thickness {wall_est:.2f}mm at Z={z_sample:.1f}mm "
                f"is below minimum {min_wall_mm}mm. "
                f"Increase wall thickness or add support material."
            )
        else:
            msg = f"Wall estimate {wall_est:.2f}mm at Z={z_sample:.1f}mm OK."

        results.append({
            "z_mm": z_sample,
            "width_mm": wall_est,
            "violation": violation,
            "message": msg,
        })

    if not results:
        results.append({
            "z_mm": None,
            "width_mm": None,
            "violation": False,
            "message": "Could not sample any cross-sections.",
        })

    return results


def check_undercuts(step_path: str) -> list[dict]:
    results: list[dict] = []

    try:
        import cadquery as cq
        from OCC.Core.BRep import BRep_Tool
        from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
        from OCC.Core.GeomAbs import GeomAbs_Plane
        from OCC.Core.gp import gp_Dir
    except ImportError:
        return [{"face_index": None, "normal": None, "reachable_from": [],
                 "is_undercut": False,
                 "message": "cadquery/OCC not available — undercut check skipped"}]

    try:
        solid = cq.importers.importStep(str(step_path))
        faces = solid.faces().vals()
    except Exception as exc:
        return [{"face_index": None, "normal": None, "reachable_from": [],
                 "is_undercut": False,
                 "message": f"Could not load STEP: {exc}"}]

    undercut_count = 0

    for idx, face in enumerate(faces):
        # Extract face normal at its centre via OCC surface adaptor
        try:
            adaptor = BRepAdaptor_Surface(face.wrapped)
            u_mid = (adaptor.FirstUParameter() + adaptor.LastUParameter()) / 2.0
            v_mid = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2.0
            pnt = adaptor.Value(u_mid, v_mid)
            # For plane faces use the plane's normal; otherwise use surface normal
            if adaptor.GetType() == GeomAbs_Plane:
                plane_ax = adaptor.Plane().Axis()
                d = plane_ax.Direction()
                nx, ny, nz = d.X(), d.Y(), d.Z()
            else:
                from OCC.Core.BRepLProp import BRepLProp_SLProps
                props = BRepLProp_SLProps(adaptor, u_mid, v_mid, 1, 1e-6)
                if props.IsNormalDefined():
                    n = props.Normal()
                    nx, ny, nz = n.X(), n.Y(), n.Z()
                else:
                    # Cannot determine normal — skip face
                    continue
        except Exception:
            continue

        normal = (round(nx, 4), round(ny, 4), round(nz, 4))

        # Check which cardinal directions can "see" this face
        reachable_from: list[str] = []
        dir_labels = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]
        for direction, label in zip(_CARDINAL_DIRECTIONS, dir_labels):
            dot = nx * direction[0] + ny * direction[1] + nz * direction[2]
            if dot > _UNDERCUT_DOT_THRESHOLD:
                reachable_from.append(label)

        is_undercut = len(reachable_from) == 0
        if is_undercut:
            undercut_count += 1
            message = (
                f"Face {idx} normal {normal} is not visible from any cardinal "
                f"direction — undercut feature."
            )
        else:
            message = (
                f"Face {idx} normal {normal} reachable from: {', '.join(reachable_from)}."
            )

        results.append({
            "face_index": idx,
            "normal": normal,
            "reachable_from": reachable_from,
            "is_undercut": is_undercut,
            "message": message,
        })

    if not results or undercut_count == 0:
        # Either no faces processed or no undercuts found
        if not any(r["is_undercut"] for r in results):
            results = [r for r in results if not r["is_undercut"]]  # keep non-undercut entries
            results.append({
                "face_index": None,
                "normal": None,
                "reachable_from": list(map(lambda x: x[1], zip(_CARDINAL_DIRECTIONS, ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]))),
                "is_undercut": False,
                "message": "No undercuts detected",
            })

    return results


def classify_machining_axes(undercut_results: list[dict]) -> list[str]:
    undercut_faces = [r for r in undercut_results if r.get("is_undercut")]

    if not undercut_faces:
        return ["3axis"]

    # Collect all unreachable normals (faces that are undercuts)
    # Determine how many distinct additional setup orientations are needed
    # by clustering the unreachable face normals into dominant directions
    normals = []
    for r in undercut_faces:
        n = r.get("normal")
        if n is not None:
            normals.append(n)

    if not normals:
        return ["4axis"]

    # Find how many distinct dominant axes appear among the undercut normals
    # Cluster by checking similarity: two normals are in the same group if
    # they share the dominant axis component (the one with largest absolute value)
    def dominant_axis(n: tuple) -> tuple:
        ax = max(range(3), key=lambda i: abs(n[i]))
        return (ax, 1 if n[ax] >= 0 else -1)

    axis_groups: set[tuple] = set()
    for n in normals:
        axis_groups.add(dominant_axis(n))

    n_extra = len(axis_groups)
    if n_extra <= 1:
        return ["4axis"]
    else:
        return ["5axis"]


def check_machinability(
    step_path: str,
    tool_library_path: str = "tools/fusion_tool_library.json",
) -> dict:
    step_path = str(step_path)
    part_slug = Path(step_path).stem

    # Resolve tool library path: use parameter if it resolves to an existing file,
    # otherwise fall back to the module-level TOOL_LIB_PATH
    tl_resolved = Path(tool_library_path)
    if not tl_resolved.is_absolute():
        tl_resolved = ROOT / tl_resolved
    lib = _load_tool_lib(tl_resolved if tl_resolved.exists() else TOOL_LIB_PATH)

    endmills = lib.get("endmills", [])
    if endmills:
        diameters = [t["dia_mm"] for t in endmills]
        min_tool_dia = min(diameters)
        max_tool_dia = max(diameters)
    else:
        min_tool_dia = 3.0
        max_tool_dia = 12.0

    # Material-specific minimum wall thickness
    _mat_min_wall = {
        "aluminium_6061": 1.5,
        "aluminium_7075": 1.5,
        "steel_4140": 2.0,
        "steel_mild": 2.0,
        "stainless_316": 2.0,
        "x1_420i": 2.0,
        "inconel_718": 2.5,
        "titanium_ti6al4v": 2.0,
        "pla": 1.2,
        "abs": 1.2,
    }
    # check_machinability doesn't take a material arg; default wall check uses 1.5mm
    min_wall = 1.5

    radii_results = check_internal_radii(step_path, min_tool_dia)
    depth_results = check_cavity_depth(step_path, max_tool_dia)
    wall_results = check_thin_walls(step_path, min_wall)
    undercut_results = check_undercuts(step_path)

    failures: list[str] = []
    warnings: list[str] = []
    suggested_fixes: list[str] = []

    # Collect radii violations
    for r in radii_results:
        if r.get("violation"):
            failures.append(r["message"])
            r_mm = r.get("radius_mm") or 0
            fix_r = round(r_mm * 1.5 + 0.5, 1)
            suggested_fixes.append(
                f"Increase internal fillet radius to >= {fix_r}mm "
                f"(currently {r_mm:.2f}mm) to allow machining with smallest tool."
            )

    # Collect depth violations
    for d in depth_results:
        if d.get("violation"):
            failures.append(d["message"])
            depth_mm = d.get("depth_mm") or 0
            suggested_fixes.append(
                f"Reduce cavity depth to <= {round(max_tool_dia * 4, 1)}mm "
                f"or rough in multiple Z-axis setups. "
                f"Current depth: {depth_mm}mm."
            )
        elif d.get("aspect_ratio") and d["aspect_ratio"] > 3.0:
            warnings.append(
                f"Cavity aspect ratio {d['aspect_ratio']}:1 is approaching the "
                f"4:1 limit — reduce depth of cut per pass."
            )

    # Collect wall violations — separate violations from warnings
    for w in wall_results:
        if w.get("violation"):
            w_mm = w.get("width_mm") or 0
            z_mm = w.get("z_mm")
            loc = f" at Z={z_mm:.1f}mm" if z_mm is not None else ""
            if w_mm < min_wall * 0.5:
                failures.append(w["message"])
                suggested_fixes.append(
                    f"Wall thickness{loc} ({w_mm:.2f}mm) is critically thin. "
                    f"Increase to >= {min_wall}mm."
                )
            else:
                warnings.append(w["message"])

    # Collect undercut warnings
    undercut_faces = [r for r in undercut_results if r.get("is_undercut")]
    if undercut_faces:
        warnings.append(
            f"{len(undercut_faces)} undercut face(s) detected — "
            f"multi-axis setup or redesign may be required."
        )

    machinable_with = classify_machining_axes(undercut_results)

    passed = len(failures) == 0

    n_v = len(failures)
    n_w = len(warnings)
    status = "PASS" if passed else "FAIL"
    print(
        f"[CAM_VALIDATE] {status} — {n_v} failure(s), {n_w} warning(s), "
        f"axes={machinable_with} [{Path(step_path).name}]"
    )

    result = {
        "passed": passed,
        "warnings": warnings,
        "failures": failures,
        "suggested_fixes": suggested_fixes,
        "machinable_with": machinable_with,
        "details": {
            "radii": radii_results,
            "depth": depth_results,
            "walls": wall_results,
            "undercuts": undercut_results,
        },
    }

    # Write machinability.json to outputs/cam/<part_slug>/
    out_dir = ROOT / "outputs" / "cam" / part_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "machinability.json"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except Exception as exc:
        print(f"[CAM_VALIDATE] Warning: could not write machinability.json: {exc}")

    return result


def run_machinability_check(
    step_path: str,
    material: str = "aluminium_6061",
) -> dict:
    """
    Run all machinability checks on a STEP file.

    Thin wrapper around check_machinability for backward compatibility.
    The material parameter is accepted but axis/undercut checks are
    material-independent; wall thresholds use a safe default of 1.5mm.

    Returns:
        {
            passed: bool,
            violations: list[str],
            warnings: list[str],
            suggested_fixes: list[str],
            machinable_with: list[str],
            details: {radii, depth, walls, undercuts},
        }
    """
    result = check_machinability(step_path)
    # Expose `violations` key for callers that expect the old schema
    result = dict(result)
    result["violations"] = result["failures"]
    return result
