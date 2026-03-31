"""
aria_os/post_gen_validator.py

Post-generation geometry validation loop.

After any STEP/STL is produced, this module:
  1. parse_spec()           — extract measurable constraints from goal + plan
  2. check_geometry()       — trimesh bbox / volume / bore / watertight checks
  3. check_output_quality() — STEP readability, STL watertight, auto-repair
  4. render_to_png()        — headless matplotlib 3-view render → PNG bytes
  5. check_visual()         — send PNG + spec to Claude vision → YES/NO
  6. run_validation_loop()  — up to max_attempts retries with failure context injection

All functions are importable individually; trimesh and matplotlib are soft deps
(graceful degradation when not installed).

generate_fn protocol (v2):
    generate_fn(plan, step_path, stl_path, repo_root,
                previous_failures=None) -> dict
    Returns at minimum: {status, step_path, stl_path, error}
    Callers that don't accept previous_failures are supported via signature inspection.
"""
from __future__ import annotations

import copy
import inspect
import re
import math
import base64
import traceback
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 1. Spec parser
# ---------------------------------------------------------------------------

def parse_spec(goal: str, plan: dict[str, Any]) -> dict[str, Any]:
    """
    Extract measurable geometric constraints from the user goal and plan.

    Returned dict keys (all optional — only present when detected):
        od_mm        : float  — outer diameter in mm
        bore_mm      : float  — bore / inner diameter in mm
        height_mm    : float  — axial height / thickness in mm
        width_mm     : float  — planar width in mm
        depth_mm     : float  — planar depth in mm
        n_teeth      : int    — tooth count (ratchet/gear parts)
        has_bore     : bool   — True if a through-hole is expected
        volume_min   : float  — minimum plausible volume in mm³ (10 % margin)
        volume_max   : float  — maximum plausible volume in mm³ (10 % margin)
        part_id      : str    — canonical part identifier
        tol_mm       : float  — dimensional tolerance (default 5 mm / 2.5 %)
        tol_frac     : float  — fractional tolerance for relative checks
    """
    spec: dict[str, Any] = {}
    text = goal.lower() + " " + str(plan.get("text", "")).lower()

    pid = plan.get("part_id", "")
    if pid:
        spec["part_id"] = pid

    params = plan.get("params", {}) or {}

    def _from_params_or_text(key_variants: list[str], text_patterns: list[str]) -> Optional[float]:
        for k in key_variants:
            if k in params and params[k] is not None:
                try:
                    return float(params[k])
                except (TypeError, ValueError):
                    pass
        for pat in text_patterns:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    return float(m.group(1))
                except (TypeError, ValueError):
                    pass
        return None

    od = _from_params_or_text(
        ["od_mm", "od", "outer_diameter", "diameter"],
        [r"(\d+(?:\.\d+)?)\s*mm\s*od", r"od\s*[=:]\s*(\d+(?:\.\d+)?)",
         r"(\d+(?:\.\d+)?)\s*mm\s*outer", r"(\d+(?:\.\d+)?)\s*mm\s+diameter"])
    if od:
        spec["od_mm"] = od

    bore = _from_params_or_text(
        ["bore_mm", "bore", "id_mm", "inner_diameter", "shaft_diameter"],
        [r"(\d+(?:\.\d+)?)\s*mm\s*bore", r"bore\s*[=:]\s*(\d+(?:\.\d+)?)",
         r"(\d+(?:\.\d+)?)\s*mm\s*id\b"])
    if bore:
        spec["bore_mm"] = bore
        spec["has_bore"] = True
    elif any(w in text for w in ["bore", "through hole", "shaft", "hollow", "tube"]):
        spec["has_bore"] = True

    height = _from_params_or_text(
        ["thickness_mm", "height_mm", "height", "thickness", "width"],
        [r"(\d+(?:\.\d+)?)\s*mm\s*thick", r"thickness\s*[=:]\s*(\d+(?:\.\d+)?)",
         r"(\d+(?:\.\d+)?)\s*mm\s*tall", r"(\d+(?:\.\d+)?)\s*mm\s*high"])
    if height:
        spec["height_mm"] = height

    n_teeth_raw = _from_params_or_text(
        ["n_teeth", "teeth", "tooth_count"],
        [r"(\d+)\s*teeth", r"(\d+)-tooth", r"teeth\s*[=:]\s*(\d+)"])
    if n_teeth_raw:
        spec["n_teeth"] = int(n_teeth_raw)

    # --- volume bounds ---
    if od and height:
        r_out = od / 2.0
        r_in  = (bore / 2.0) if bore else 0.0
        vol_nominal = math.pi * (r_out**2 - r_in**2) * height
        margin = 0.10
        spec["volume_min"] = vol_nominal * (1 - margin)
        spec["volume_max"] = vol_nominal * (1 + margin)

    # --- tolerances (part-specific) ---
    # Safety-critical / precision parts get tighter tolerances.
    _TIGHT = {"aria_ratchet_ring", "aria_catch_pawl", "aria_cam_collar", "aria_spool"}
    _MED   = {"aria_brake_drum", "aria_rope_guide", "aria_housing",
              "aria_flange", "aria_shaft", "aria_pulley"}
    # Enclosures/cases: input dims describe the CONTAINED object, not the case itself.
    # Output bbox is always larger (walls + bumpers). Use wide tolerance.
    _ENCLOSURE_KW = ("case", "enclosure", "cover", "sleeve", "holder", "shell")
    pid = plan.get("part_id", "")
    _goal_lower = goal.lower() if isinstance(goal, str) else ""
    _is_enclosure = any(kw in pid.lower() or kw in _goal_lower for kw in _ENCLOSURE_KW)
    if pid in _TIGHT:
        spec["tol_mm"]   = 2.0
        spec["tol_frac"] = 0.02
    elif pid in _MED:
        spec["tol_mm"]   = 3.0
        spec["tol_frac"] = 0.03
    elif _is_enclosure:
        spec["tol_mm"]   = 15.0
        spec["tol_frac"] = 0.20
    else:
        spec["tol_mm"]   = 5.0
        spec["tol_frac"] = 0.05

    return spec


# ---------------------------------------------------------------------------
# 2. Geometric validation (trimesh)
# ---------------------------------------------------------------------------

def check_geometry(stl_path: str, spec: dict[str, Any]) -> dict[str, Any]:
    """
    Load STL with trimesh and validate against spec.

    Returns dict:
        passed    : bool
        failures  : list[str]
        bbox      : dict | None
        volume    : float | None
        watertight: bool | None
    """
    result: dict[str, Any] = {
        "passed": True,
        "failures": [],
        "bbox": None,
        "volume": None,
        "watertight": None,
    }

    try:
        import trimesh  # type: ignore
    except ImportError:
        result["failures"].append("trimesh not installed — geometric checks skipped")
        return result

    p = Path(stl_path)
    if not p.exists() or p.stat().st_size < 100:
        result["passed"] = False
        result["failures"].append(f"STL file missing or empty: {stl_path}")
        return result

    try:
        mesh = trimesh.load(str(p), force="mesh")
    except Exception as exc:
        result["passed"] = False
        result["failures"].append(f"trimesh load failed: {exc}")
        return result

    # --- watertight ---
    result["watertight"] = mesh.is_watertight
    if not mesh.is_watertight:
        result["failures"].append("Mesh is not watertight (open shell detected)")
        result["passed"] = False

    # --- bounding box ---
    ext = mesh.bounding_box.extents
    bb = {"x": float(ext[0]), "y": float(ext[1]), "z": float(ext[2])}
    result["bbox"] = bb

    # Scale tolerance: 15% of nominal or 2mm floor (whichever is larger).
    # 15% accounts for cases/enclosures where output is intentionally larger
    # than the contained object (phone dims + walls + bumpers).
    _tol_floor = spec.get("tol_mm", 2.0)
    tol_frac   = spec.get("tol_frac", 0.15)

    od = spec.get("od_mm")
    if od:
        for axis, val in [("x", bb["x"]), ("y", bb["y"])]:
            allowed = max(_tol_floor, od * tol_frac)
            if abs(val - od) > allowed:
                result["failures"].append(
                    f"Bbox {axis.upper()} = {val:.1f} mm; expected ~{od:.1f} mm "
                    f"(±{allowed:.1f}) — outer diameter mismatch"
                )
                result["passed"] = False

    height = spec.get("height_mm")
    if height:
        # Check if ANY bbox axis matches the expected height (not just the minimum).
        # For box-notation specs (WxHxD), the height may map to any axis depending
        # on the part's orientation (e.g. phone case: height=78.1 is the Y axis).
        allowed_h = max(_tol_floor, height * tol_frac)
        _any_match = any(abs(v - height) <= allowed_h for v in bb.values())
        if not _any_match:
            _closest = min(bb.values(), key=lambda v: abs(v - height))
            result["failures"].append(
                f"No bbox axis matches expected height ~{height:.1f} mm "
                f"(+/-{allowed_h:.1f}); closest = {_closest:.1f} mm"
            )
            result["passed"] = False

    # Width and depth checks (same any-axis logic)
    for _dim_key in ("width_mm", "depth_mm"):
        _dim_val = spec.get(_dim_key)
        if _dim_val:
            _dim_tol = max(_tol_floor, _dim_val * tol_frac)
            _dim_match = any(abs(v - _dim_val) <= _dim_tol for v in bb.values())
            if not _dim_match:
                _dim_closest = min(bb.values(), key=lambda v: abs(v - _dim_val))
                result["failures"].append(
                    f"No bbox axis matches expected {_dim_key} ~{_dim_val:.1f} mm "
                    f"(+/-{_dim_tol:.1f}); closest = {_dim_closest:.1f} mm"
                )
                result["passed"] = False

    # --- volume ---
    vol = float(mesh.volume)
    result["volume"] = vol

    if "volume_min" in spec and "volume_max" in spec:
        if not (spec["volume_min"] <= vol <= spec["volume_max"]):
            result["failures"].append(
                f"Volume = {vol:.0f} mm³; expected "
                f"[{spec['volume_min']:.0f}, {spec['volume_max']:.0f}] mm³ — "
                f"volume out of bounds"
            )
            result["passed"] = False

    # --- bore detection ---
    if spec.get("has_bore"):
        bore_found = _detect_bore(mesh, bb)
        if not bore_found:
            result["failures"].append(
                "Expected through-bore not detected — mesh appears solid; "
                "ensure bore/hole is cut completely through the part"
            )
            result["passed"] = False

    return result


def _detect_bore(mesh: Any, bb: dict) -> bool:
    """
    Volume-fraction bore detector. Ratio of mesh volume to enclosing solid-cylinder
    volume < 0.65 → bore present.  No rtree required.
    """
    try:
        r_approx = min(bb["x"], bb["y"]) / 2.0
        h_approx = bb.get("z", 1.0)
        solid_vol = math.pi * r_approx * r_approx * h_approx
        if solid_vol <= 0:
            return True
        return (float(mesh.volume) / solid_vol) < 0.65
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 3. Output quality: STEP readability + STL watertight + auto-repair
# ---------------------------------------------------------------------------

def validate_step(step_path: str) -> dict[str, Any]:
    """
    Verify a STEP file is readable and contains at least one solid.

    Returns {readable, solid_count, file_size_bytes, error}.
    """
    result: dict[str, Any] = {
        "readable": False,
        "solid_count": 0,
        "file_size_bytes": 0,
        "error": None,
    }

    p = Path(step_path)
    if not p.exists():
        result["error"] = f"STEP file not found: {step_path}"
        return result

    result["file_size_bytes"] = p.stat().st_size

    # Try cadquery import first
    try:
        import cadquery as cq  # type: ignore
        shape = cq.importers.importStep(str(p))
        result["readable"]    = True
        result["solid_count"] = len(shape.solids().vals())
        return result
    except ImportError:
        pass
    except Exception as exc:
        result["error"] = str(exc)
        return result

    # Fallback: just check ISO-10303 header
    try:
        header = p.read_bytes()[:40]
        if b"ISO-10303" in header or b"STEP" in header:
            result["readable"]    = True
            result["solid_count"] = -1  # unknown without CQ
        else:
            result["error"] = "File does not appear to be a valid STEP"
    except OSError as exc:
        result["error"] = str(exc)

    return result


def check_and_repair_stl(stl_path: str) -> dict[str, Any]:
    """
    Load STL, check watertight status, attempt repair if needed, re-export.

    Returns:
        watertight_before : bool
        watertight_after  : bool
        repaired          : bool  — True if repair improved the mesh and was saved
        repair_attempted  : bool
        file_size_bytes   : int
        error             : str | None
    """
    result: dict[str, Any] = {
        "watertight_before": None,
        "watertight_after":  None,
        "repaired":          False,
        "repair_attempted":  False,
        "file_size_bytes":   0,
        "error":             None,
    }

    p = Path(stl_path)
    if not p.exists():
        result["error"] = f"STL not found: {stl_path}"
        return result

    result["file_size_bytes"] = p.stat().st_size

    try:
        import trimesh  # type: ignore
    except ImportError:
        result["error"] = "trimesh not installed"
        return result

    try:
        mesh = trimesh.load(str(p), force="mesh")
    except Exception as exc:
        result["error"] = f"Load failed: {exc}"
        return result

    result["watertight_before"] = mesh.is_watertight

    if mesh.is_watertight:
        result["watertight_after"] = True
        return result

    # Attempt repair
    result["repair_attempted"] = True
    try:
        trimesh.repair.fill_holes(mesh)
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fix_winding(mesh)
        result["watertight_after"] = mesh.is_watertight
        if mesh.is_watertight:
            mesh.export(str(p))
            result["repaired"] = True
    except Exception as exc:
        result["error"] = f"Repair failed: {exc}"
        result["watertight_after"] = False

    return result


def check_output_quality(
    step_path: str,
    stl_path: str,
) -> dict[str, Any]:
    """
    Combined output quality check: STEP readability + STL watertight + repair.

    Returns:
        step   : dict from validate_step()
        stl    : dict from check_and_repair_stl()
        passed : bool — True if both STEP is readable and STL is watertight
        failures : list[str]
    """
    qr: dict[str, Any] = {"step": {}, "stl": {}, "passed": True, "failures": []}

    qr["step"] = validate_step(step_path)
    qr["stl"]  = check_and_repair_stl(stl_path)

    if qr["step"].get("error"):
        qr["failures"].append(f"STEP validation error: {qr['step']['error']}")
        qr["passed"] = False
    elif not qr["step"].get("readable"):
        qr["failures"].append("STEP file is not readable or has wrong format")
        qr["passed"] = False

    stl_ok = qr["stl"].get("watertight_after")
    if stl_ok is False:
        qr["failures"].append(
            "STL is not watertight after repair attempt — "
            "geometry may have open edges or degenerate faces"
        )
        qr["passed"] = False
    elif qr["stl"].get("error"):
        qr["failures"].append(f"STL quality check error: {qr['stl']['error']}")

    return qr


# ---------------------------------------------------------------------------
# 4. Render to PNG (matplotlib headless)
# ---------------------------------------------------------------------------

def render_to_png(stl_path: str) -> Optional[bytes]:
    """
    Render an STL file to a PNG image using matplotlib (headless Agg backend).
    Returns raw PNG bytes or None if unavailable.
    Shows three views: isometric, top-down (+Z), front (-Y).
    """
    try:
        import trimesh  # type: ignore
        import matplotlib  # type: ignore
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # type: ignore
        import numpy as np  # type: ignore
        import io
    except ImportError:
        return None

    p = Path(stl_path)
    if not p.exists():
        return None

    try:
        mesh = trimesh.load(str(p), force="mesh")
        verts = mesh.vertices
        faces = mesh.faces

        fig = plt.figure(figsize=(12, 4))
        views = [
            ("Isometric", 30,  45),
            ("Top (+Z)",   90,   0),
            ("Front (-Y)",  0, -90),
        ]

        centre = verts.mean(axis=0)
        scale  = np.abs(verts - centre).max() or 1.0

        for idx, (title, elev, azim) in enumerate(views, 1):
            ax = fig.add_subplot(1, 3, idx, projection="3d")
            tris = [verts[f] for f in faces]
            poly = Poly3DCollection(tris, alpha=0.6, linewidth=0,
                                    facecolor="steelblue", edgecolor=None)
            ax.add_collection3d(poly)
            ax.set_xlim(centre[0] - scale, centre[0] + scale)
            ax.set_ylim(centre[1] - scale, centre[1] + scale)
            ax.set_zlim(centre[2] - scale, centre[2] + scale)
            ax.view_init(elev=elev, azim=azim)
            ax.set_title(title, fontsize=9)
            ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 5. Visual validation (Claude vision)
# ---------------------------------------------------------------------------

def check_visual(
    png_bytes: bytes,
    spec: dict[str, Any],
    goal: str,
    repo_root: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Send rendered PNG + spec description to Claude vision.
    Returns {passed, answer, failures}.
    """
    result: dict[str, Any] = {"passed": True, "answer": "", "failures": []}

    try:
        import anthropic  # type: ignore
    except ImportError:
        result["failures"].append("anthropic not installed — visual check skipped")
        return result

    try:
        from .llm_client import get_anthropic_key
        api_key = get_anthropic_key(repo_root)
        if not api_key:
            result["failures"].append("ANTHROPIC_API_KEY not set — visual check skipped")
            return result
    except Exception as exc:
        result["failures"].append(f"Could not get API key: {exc}")
        return result

    spec_lines = [f"Goal: {goal}"]
    if "od_mm"     in spec: spec_lines.append(f"Expected outer diameter: {spec['od_mm']:.1f} mm")
    if "bore_mm"   in spec: spec_lines.append(f"Expected bore diameter: {spec['bore_mm']:.1f} mm")
    if "height_mm" in spec: spec_lines.append(f"Expected height/thickness: {spec['height_mm']:.1f} mm")
    if "n_teeth"   in spec: spec_lines.append(f"Expected tooth count: {spec['n_teeth']}")
    if spec.get("has_bore"): spec_lines.append("Must have a through-bore (hole through the center)")
    spec_text = "\n".join(spec_lines)

    prompt = (
        f"You are inspecting a 3D-printed mechanical part.\n\n"
        f"Specification:\n{spec_text}\n\n"
        f"The image shows three views (isometric, top, front) of the generated geometry.\n\n"
        f"Does this geometry match the specification? "
        f"Answer YES or NO on the first line, then briefly explain any discrepancies."
    )

    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    client = anthropic.Anthropic(api_key=api_key)

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        answer = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        result["answer"] = answer

        first_line = answer.splitlines()[0].strip().upper() if answer else ""
        if "YES" in first_line:
            result["passed"] = True
        elif "NO" in first_line:
            result["passed"] = False
            result["failures"].append(f"Visual check failed: {answer}")
        else:
            result["passed"] = True
            result["failures"].append(f"Visual check answer unclear: {answer[:200]}")

    except Exception as exc:
        result["failures"].append(f"Visual check API error: {exc}")

    return result


# ---------------------------------------------------------------------------
# 6. Failure context injection
# ---------------------------------------------------------------------------

def _inject_failure_context(
    plan: dict[str, Any],
    geo_failures: list[str],
    vis_failures: list[str],
) -> dict[str, Any]:
    """Return a deep-copy of plan with failure context appended to build_order + text."""
    updated = copy.deepcopy(plan)
    msgs: list[str] = []
    if geo_failures:
        msgs.append("GEOMETRY FAILURES from previous attempt:")
        msgs.extend(f"  - {f}" for f in geo_failures)
    if vis_failures:
        msgs.append("VISUAL FAILURES from previous attempt:")
        msgs.extend(f"  - {f}" for f in vis_failures)
    if msgs:
        updated.setdefault("build_order", [])
        updated["build_order"].extend(msgs)
        existing_text = updated.get("text", "")
        updated["text"] = existing_text + "\n\n" + "\n".join(msgs)
    return updated


def _build_failure_report(
    spec: dict[str, Any],
    geo_failures: list[str],
    vis_failures: list[str],
    attempts: int,
) -> str:
    lines = [f"Validation failed after {attempts} attempt(s).", "", "Spec used:"]
    for k, v in sorted(spec.items()):
        lines.append(f"  {k}: {v}")
    if geo_failures:
        lines += ["", "Geometric failures:"] + [f"  - {f}" for f in geo_failures]
    if vis_failures:
        lines += ["", "Visual failures:"] + [f"  - {f}" for f in vis_failures]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. Internal: call generate_fn with optional previous_failures kwarg
# ---------------------------------------------------------------------------

def _call_generate_fn(
    generate_fn: Callable,
    plan: dict[str, Any],
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path],
    previous_failures: list[str],
) -> dict[str, Any]:
    """
    Call generate_fn, passing previous_failures as a keyword arg if the function
    signature accepts it.  Falls back to the old 4-positional-arg call form.
    """
    try:
        sig = inspect.signature(generate_fn)
        if "previous_failures" in sig.parameters:
            return generate_fn(plan, step_path, stl_path, repo_root,
                               previous_failures=previous_failures)
    except (ValueError, TypeError):
        pass
    return generate_fn(plan, step_path, stl_path, repo_root)


# ---------------------------------------------------------------------------
# 8. Validation loop
# ---------------------------------------------------------------------------

def run_validation_loop(
    generate_fn: Callable[..., dict[str, Any]],
    goal: str,
    plan: dict[str, Any],
    step_path: str,
    stl_path: str,
    max_attempts: int = 3,
    repo_root: Optional[Path] = None,
    *,
    skip_visual: bool = False,
    check_quality: bool = False,
) -> dict[str, Any]:
    """
    Full validation loop with per-attempt failure-context injection.

    generate_fn signature (v2):
        generate_fn(plan, step_path, stl_path, repo_root,
                    previous_failures=None) -> dict
        Must return: {status, step_path, stl_path, error}

    Parameters
    ----------
    generate_fn   : callable — produces geometry
    goal          : natural-language description
    plan          : plan dict (copied; not mutated)
    step_path     : target STEP output path
    stl_path      : target STL output path
    max_attempts  : retry limit (default 3)
    repo_root     : optional repo root for API key / context loading
    skip_visual   : bypass Claude vision check (CI / offline mode)
    check_quality : also run check_output_quality() (STEP + repair)

    Returns
    -------
    dict with keys:
        status            : "success" | "failure"
        attempts          : int
        step_path         : str | None
        stl_path          : str | None
        geo_result        : dict   (last geometry check)
        vis_result        : dict   (last visual check)
        quality_result    : dict   (output quality; only when check_quality=True)
        failure_report    : str    (only on final failure)
        validation_failures : list[str]  (all unique failures across all attempts)
        generate_result   : dict   (last generate_fn output)
    """
    spec         = parse_spec(goal, plan)
    current_plan = copy.deepcopy(plan)

    # Tracking across attempts
    accumulated_failures: list[str] = []
    best_gen_result:  dict[str, Any] = {}
    best_geo:         dict[str, Any] = {"passed": False, "failures": []}
    best_vis:         dict[str, Any] = {"passed": True,  "failures": []}
    best_fail_count   = 9999

    last_geo: dict[str, Any] = {"passed": True, "failures": []}
    last_vis: dict[str, Any] = {"passed": True, "failures": []}
    last_quality: dict[str, Any] = {}

    for attempt in range(1, max_attempts + 1):
        # --- Generate ---
        try:
            gen_result = _call_generate_fn(
                generate_fn, current_plan, step_path, stl_path,
                repo_root, accumulated_failures,
            )
        except Exception:
            gen_result = {
                "status":    "failure",
                "error":     traceback.format_exc(),
                "step_path": None,
                "stl_path":  str(stl_path),
            }

        actual_stl  = gen_result.get("stl_path") or stl_path
        actual_step = gen_result.get("step_path") or step_path

        # --- Geometric validation ---
        if Path(actual_stl).exists():
            last_geo = check_geometry(actual_stl, spec)
        else:
            last_geo = {
                "passed": False,
                "failures": [f"STL not produced at {actual_stl}"],
                "bbox": None, "volume": None, "watertight": None,
            }

        # --- Output quality (optional) ---
        last_quality = {}
        if check_quality and Path(actual_step).exists() and Path(actual_stl).exists():
            last_quality = check_output_quality(actual_step, actual_stl)
            # Merge quality failures into geo failures for context injection
            for qf in last_quality.get("failures", []):
                if qf not in last_geo["failures"]:
                    last_geo["failures"].append(qf)
            if not last_quality.get("passed", True):
                last_geo["passed"] = False

        # --- Visual validation ---
        last_vis = {"passed": True, "failures": []}
        if not skip_visual and Path(actual_stl).exists():
            png_bytes = render_to_png(actual_stl)
            if png_bytes:
                last_vis = check_visual(png_bytes, spec, goal, repo_root)
            else:
                last_vis = {
                    "passed": True,
                    "failures": ["Could not render STL to PNG — visual check skipped"],
                }

        # --- Track best attempt (fewest total failures) ---
        attempt_fail_count = (
            len(last_geo.get("failures", []))
            + len(last_vis.get("failures", []))
        )
        if attempt_fail_count < best_fail_count:
            best_fail_count  = attempt_fail_count
            best_gen_result  = gen_result
            best_geo         = last_geo
            best_vis         = last_vis

        # Accumulate all unique failures for the report + next-attempt context
        for f in last_geo.get("failures", []) + last_vis.get("failures", []):
            if f not in accumulated_failures:
                accumulated_failures.append(f)

        # --- Check pass ---
        geo_ok = last_geo.get("passed", True)
        vis_ok = last_vis.get("passed", True)
        gen_ok = gen_result.get("status") in ("success", "success_cem_warning")

        if gen_ok and geo_ok and vis_ok:
            return {
                "status":             "success",
                "attempts":           attempt,
                "step_path":          gen_result.get("step_path"),
                "stl_path":           gen_result.get("stl_path"),
                "geo_result":         last_geo,
                "vis_result":         last_vis,
                "quality_result":     last_quality,
                "failure_report":     "",
                "validation_failures": [],
                "generate_result":    gen_result,
            }

        # --- Inject failure context for next attempt ---
        if attempt < max_attempts:
            current_plan = _inject_failure_context(
                current_plan,
                last_geo.get("failures", []),
                last_vis.get("failures", []),
            )

    # --- Final failure: return best attempt with full report ---
    report = _build_failure_report(
        spec,
        best_geo.get("failures", []),
        best_vis.get("failures", []),
        max_attempts,
    )
    return {
        "status":             "failure",
        "attempts":           max_attempts,
        "step_path":          best_gen_result.get("step_path"),
        "stl_path":           best_gen_result.get("stl_path"),
        "geo_result":         best_geo,
        "vis_result":         best_vis,
        "quality_result":     last_quality,
        "failure_report":     report,
        "validation_failures": accumulated_failures,
        "generate_result":    best_gen_result,
    }
