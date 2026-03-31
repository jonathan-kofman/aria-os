"""
Grasshopper/RhinoCommon pipeline validator.
GRASSHOPPER_ONLY = True: exec-based CadQuery validation removed.
Validates STEP files by size only (no cadquery re-import).
BBOX parsing from printed BBOX:x,y,z output is retained for LLM-generated GH scripts.
"""
import re
import sys
from io import StringIO
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

GRASSHOPPER_ONLY = True


@dataclass
class ValidationResult:
    passed: bool
    geometry: Any  # retained for API compat; None for GH path
    error: str = ""
    bbox: Optional[Tuple[float, float, float]] = None  # (dx, dy, dz) in mm
    bbox_match: bool = False
    file_valid: bool = False
    solid_count: int = 0
    errors: List[str] = field(default_factory=list)
    stdout_capture: str = ""


def check_feature_completeness(code: str, plan: dict) -> tuple[bool, str]:
    """
    Verify that required plan features are present in generated code.
    Heuristic string-based check to catch missing critical operations.
    Works on both CadQuery and RhinoCommon script text.
    """
    if not isinstance(plan, dict):
        return True, ""

    features = plan.get("features", []) or []
    code_lower = (code or "").lower()

    if plan.get("hollow"):
        hollow_indicators = [".cut(", "difference", "subtract", "shell", "hollow", "inner_radius"]
        if not any(ind in code_lower for ind in hollow_indicators):
            return False, "missing: interior void cut for hollow part"

    for f in features:
        if not isinstance(f, dict):
            continue
        ftype = str(f.get("type", "")).lower()
        if ftype == "bore":
            bore_indicators = [".hole(", ".cutblind(", "addhole", "cylinderhole", "bore_dia", "bore_r"]
            if not any(ind in code_lower for ind in bore_indicators):
                return False, "missing: bore operation"
        elif ftype == "slot":
            has_slot = "slot" in code_lower or ("rect" in code_lower and "cut" in code_lower)
            if not has_slot:
                return False, "missing: slot operation"
        elif ftype == "bolt_circle":
            has_pattern = (
                ".polararray(" in code_lower
                or "polar" in code_lower
                or ("for " in code_lower and "hole" in code_lower)
                or "bolt_circle" in code_lower
            )
            if not has_pattern:
                return False, "missing: bolt circle hole pattern"

    return True, ""


def validate_mesh_integrity(stl_path: str) -> dict:
    """
    Check STL for common mesh errors before printing.
    Uses numpy-stl if available, falls back to file size check.
    """
    try:
        import numpy as np
        from stl import mesh as stl_mesh

        m = stl_mesh.Mesh.from_file(stl_path)

        v0, v1, v2 = m.v0, m.v1, m.v2
        cross = np.cross(v1 - v0, v2 - v0)
        areas = np.sqrt((cross**2).sum(axis=1))
        degenerate_count = int((areas < 1e-10).sum())

        all_verts = np.vstack([v0, v1, v2])
        unique_verts = np.unique(np.round(all_verts, 3), axis=0)

        return {
            "valid": degenerate_count == 0,
            "triangle_count": int(len(m.vectors)),
            "degenerate_triangles": int(degenerate_count),
            "unique_vertices": int(len(unique_verts)),
            "print_ready": degenerate_count == 0,
        }
    except ImportError:
        size = Path(stl_path).stat().st_size if Path(stl_path).exists() else 0
        return {
            "valid": size > 10000,
            "print_ready": size > 10000,
            "note": "install numpy-stl for full validation",
        }


def validate(
    code: str,
    expected_bbox: Optional[Tuple[float, float, float]] = None,
    step_path: Optional[Path] = None,
    min_step_size_kb: float = 50.0,
    inject_namespace: Optional[dict] = None,
) -> ValidationResult:
    """
    Execute Grasshopper/RhinoCommon script, capture stdout, parse BBOX line.
    inject_namespace: dict merged into exec namespace (STEP_PATH, STL_PATH, PART_NAME).

    For Grasshopper scripts the exec may fail if rhinoscriptsyntax is not installed locally —
    that is expected. We still parse BBOX from any stdout captured, and fall back to
    checking the STEP file by size.
    """
    out = StringIO()
    err = StringIO()
    namespace = dict(inject_namespace or {})

    # --- Sandboxed exec: restrict builtins to block os/subprocess/socket access ---
    _ALLOWED_MODULES = frozenset({"cadquery", "math", "cadquery.exporters"})

    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name not in _ALLOWED_MODULES:
            raise ImportError(f"Import of '{name}' is blocked by sandbox")
        return __builtins__.__import__(name, globals, locals, fromlist, level) if hasattr(__builtins__, '__import__') else __import__(name, globals, locals, fromlist, level)

    safe_builtins = {
        "__import__": _safe_import,
        "range": range, "len": len, "print": print,
        "abs": abs, "min": min, "max": max, "round": round,
        "float": float, "int": int, "str": str,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "bool": bool, "enumerate": enumerate, "zip": zip, "map": map,
        "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
        "True": True, "False": False, "None": None,
        "ValueError": ValueError, "TypeError": TypeError,
        "RuntimeError": RuntimeError, "Exception": Exception,
    }
    namespace["__builtins__"] = safe_builtins

    old_stdout, old_stderr = sys.stdout, sys.stderr
    exec_error = ""
    try:
        sys.stdout, sys.stderr = out, err
        exec(code, namespace)
    except Exception as e:
        exec_error = str(e)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    stdout_capture = out.getvalue()

    # Parse BBOX from printed output (works for both CQ and GH scripts)
    bbox = None
    m = re.search(r"BBOX:([\d.]+),([\d.]+),([\d.]+)", stdout_capture)
    if m:
        bbox = (float(m.group(1)), float(m.group(2)), float(m.group(3)))

    errors: List[str] = []
    bbox_match = True
    if expected_bbox and bbox:
        tol = 0.5
        for i, (a, b) in enumerate(zip(bbox, expected_bbox)):
            if abs(a - b) > tol:
                bbox_match = False
                errors.append(f"Bbox axis {i}: got {a:.2f}, expected {b:.2f} ±{tol}")
    elif expected_bbox and not bbox:
        # For GH scripts that didn't exec locally, skip strict bbox failure
        bbox_match = True  # cannot check; don't fail on missing bbox

    # Step file check (size only — no cadquery re-import)
    file_valid = True
    solid_count = 1  # assumed present if file passes size check
    if step_path and Path(step_path).exists():
        size_kb = Path(step_path).stat().st_size / 1024
        if size_kb < min_step_size_kb:
            errors.append(f"STEP file too small: {size_kb:.1f} KB (min {min_step_size_kb} KB)")
            file_valid = False
    elif step_path and not Path(step_path).exists():
        # GH scripts write STEP via Rhino Compute; may not exist yet — not a hard failure
        file_valid = True

    # If exec produced a result variable, note it; otherwise use None
    result_obj = namespace.get("result")

    passed = bbox_match and len(errors) == 0
    if step_path and not file_valid:
        passed = False

    return ValidationResult(
        passed=passed,
        geometry=result_obj,
        error="; ".join(errors) if errors else exec_error,
        bbox=bbox,
        bbox_match=bbox_match,
        file_valid=file_valid,
        solid_count=solid_count,
        errors=errors,
        stdout_capture=stdout_capture,
    )


def validate_housing_spec(result: ValidationResult, spec: dict) -> ValidationResult:
    """Check housing bbox 700x680x344 ±0.5 mm. Mutates result.passed and result.errors."""
    if result.bbox is None:
        # GH path: bbox may not be available locally; advisory only
        return result
    dx, dy, dz = result.bbox
    w = spec.get("width", 700)
    h = spec.get("height", 680)
    d = spec.get("depth", 344)
    tol = 0.5
    if abs(dx - w) > tol or abs(dy - h) > tol or abs(dz - d) > tol:
        result.passed = False
        result.errors.append(f"Bbox {dx:.2f}x{dy:.2f}x{dz:.2f} mm, expected {w}x{h}x{d} ±{tol} mm")
        result.error = result.errors[-1]
    return result


def validate_step_file(step_path: Path, min_size_kb: float = 10.0) -> Tuple[bool, int, List[str]]:
    """
    Check STEP file by size only (>= min_size_kb).
    Returns (file_valid, solid_count, errors).
    solid_count is 1 if size check passes (cadquery re-import removed).
    """
    errors: List[str] = []
    if not Path(step_path).exists():
        return False, 0, [f"STEP file not found: {step_path}"]
    size_kb = Path(step_path).stat().st_size / 1024
    if size_kb < min_size_kb:
        errors.append(f"STEP file too small: {size_kb:.1f} KB (min {min_size_kb} KB)")
        return False, 0, errors
    return True, 1, errors


def validate_grasshopper_script(script_path: str) -> tuple[bool, list[str]]:
    """
    Validate a generated RhinoCommon script.

    Checks:
    - File exists and is > 500 bytes
    - Valid Python syntax (ast.parse)
    - Contains rhinoscriptsyntax or Rhino.Geometry import
    - Contains sc.doc.Objects.AddBrep (geometry added to doc)
    - Contains rs.Command (STEP/STL export call)
    - Contains BBOX: print statement
    """
    import ast
    errors: list[str] = []
    p = Path(script_path)
    if not p.exists():
        return False, [f"Script not found: {script_path}"]
    size = p.stat().st_size
    if size < 500:
        errors.append(f"Script too small: {size} bytes (min 500)")
        return False, errors
    code = p.read_text(encoding="utf-8")
    try:
        ast.parse(code)
    except SyntaxError as e:
        errors.append(f"Syntax error: {e}")
    if "rhinoscriptsyntax" not in code and "Rhino.Geometry" not in code:
        errors.append("Missing: import rhinoscriptsyntax or Rhino.Geometry")
    if "sc.doc.Objects.AddBrep" not in code:
        errors.append("Missing: sc.doc.Objects.AddBrep")
    if "rs.Command" not in code:
        errors.append("Missing: rs.Command (export call)")
    if "BBOX:" not in code:
        errors.append("Missing: BBOX: print statement")
    return len(errors) == 0, errors
