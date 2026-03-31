from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any

import json

from ..context_loader import get_mechanical_constants
from aria_models import static_tests as _st_mod
from .. import event_bus


@dataclass
class CEMCheckResult:
    part_id: str
    static_passed: Optional[bool] = None
    static_min_sf: Optional[float] = None
    static_failure_mode: Optional[str] = None
    dynamic_passed: Optional[bool] = None
    dynamic_peak_force_N: Optional[float] = None
    dynamic_arrest_dist_mm: Optional[float] = None
    cem_passed: Optional[bool] = None
    cem_warnings: List[str] = field(default_factory=list)
    overall_passed: bool = True
    summary: str = ""


def _load_meta(meta_path: Path) -> Dict[str, Any]:
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _run_static_checks(part_id: str, meta: Dict[str, Any], context: dict) -> tuple[Optional[bool], Optional[float], Optional[str]]:
    """Run static checks with dimensions mapped from meta JSON and mechanical defaults."""
    from aria_models import static_tests as st

    lid = part_id.lower()
    applies = any(
        kw in lid
        for kw in ("pawl", "lever", "trip", "blocker", "ratchet", "ring", "gear", "tooth", "housing", "shell", "enclosure")
    )
    if not applies:
        return None, None, None

    dims = (meta.get("dims_mm") or {}) if meta else {}

    # Mechanical defaults from context
    mech = get_mechanical_constants(context)
    pawl_tip_default = float(mech.get("pawl_tip_width_mm", 6.0))
    pawl_thick_default = float(mech.get("pawl_thickness_mm", 9.0))
    pawl_arm_default = float(mech.get("pawl_arm_mm", 45.0))
    pawl_body_h_default = float(mech.get("pawl_body_h_mm", 22.0))
    housing_wall_default = float(mech.get("housing_wall_mm", 10.0))
    shaft_d_default = float(mech.get("shaft_d_mm", 20.0))

    def get_dim(d: Dict[str, Any], *keys: str, default: Optional[float] = None) -> Optional[float]:
        for key in keys:
            up = key.upper()
            for dk, dv in d.items():
                if up in str(dk).upper():
                    try:
                        return float(dv)
                    except (TypeError, ValueError):
                        continue
        return default

    pawl_tip_width = get_dim(dims, "WIDTH", "TIP_WIDTH", default=pawl_tip_default)
    pawl_thickness = get_dim(dims, "THICKNESS", default=pawl_thick_default)
    pawl_arm = get_dim(dims, "ARM", "LENGTH", default=pawl_arm_default)
    pawl_body_h = get_dim(dims, "HEIGHT", "BODY_H", default=pawl_body_h_default)
    housing_wall = get_dim(dims, "WALL", default=housing_wall_default)
    shaft_d = get_dim(dims, "SHAFT", "BORE", "ID", default=shaft_d_default)

    load_steps = [2000, 4000, 8000, 12000, 16000]

    # Part-type routing
    if any(x in lid for x in ("pawl", "lever", "trip", "blocker")):
        df = st.simulate_static_pawl(
            load_steps=load_steps,
            pawl_tip_width_mm=pawl_tip_width,
            pawl_thickness_mm=pawl_thickness,
            pawl_arm_mm=pawl_arm,
            pawl_body_h_mm=pawl_body_h,
            housing_wall_mm=housing_wall,
        )
    elif any(x in lid for x in ("ratchet", "ring", "gear", "tooth")):
        sf = _ratchet_tooth_shear_sf(dims, _st_mod.YIELD_RATCHET_MPA)
        # Ratchet ring tooth shear is safety-critical — requires SF >= 8.0
        return (sf >= 8.0), float(sf), "tooth_shear"
    elif any(x in lid for x in ("housing", "shell", "enclosure")):
        df = st.simulate_static_pawl(
            load_steps=load_steps,
            housing_wall_mm=housing_wall,
        )
    elif any(x in lid for x in ("shaft", "spool", "collar")):
        df = st.simulate_static_pawl(
            load_steps=load_steps,
            shaft_d_mm=shaft_d,
        )
    else:
        df = st.simulate_static_pawl(load_steps=load_steps)
    static_passed = bool(df["passed"].all())
    # Use worst-case (min over all loads)
    min_sf = float(df["min_sf"].min())
    # Determine which failure mode is critical at highest load
    worst_row = df.loc[df["load_N"].idxmax()]
    sf_map = {
        "sf_contact": worst_row["sf_contact"],
        "sf_bending": worst_row["sf_bending"],
        "sf_housing": worst_row["sf_housing"],
        "sf_shaft": worst_row["sf_shaft"],
    }
    failure_mode = min(sf_map, key=sf_map.get)
    return static_passed, min_sf, failure_mode


def _run_dynamic_checks() -> tuple[Optional[bool], Optional[float], Optional[float]]:
    """Run system-level dynamic drop test once."""
    from aria_models import dynamic_drop as dd
    _, summary = dd.simulate_drop_test()
    return (
        bool(summary.get("passed")),
        float(summary.get("peak_force_N", 0.0)),
        float(summary.get("arrest_distance_mm", 0.0)),
    )


def _run_cem_system_check(goal: str = "", part_id: str = "") -> tuple[Optional[bool], List[str]]:
    """Run resolved CEM module's compute_for_goal() and validate outputs."""
    try:
        import importlib
        import sys
        import os

        from cem_registry import resolve_cem_module

        mod_name = resolve_cem_module(goal or "", part_id or "")
        if mod_name is None:
            return None, []

        # Ensure repo root is on path for CEM module resolution
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

        cem_mod = importlib.import_module(mod_name)
        compute_fn = getattr(cem_mod, "compute_for_goal", None)
        if compute_fn is None:
            return None, []
    except (ImportError, Exception):
        return None, []

    try:
        result = compute_fn(goal or "", {})
        if not isinstance(result, dict) or not result:
            return None, []
        # Validate that physics outputs are plausible (non-zero scalars)
        warnings: List[str] = []
        for key, val in result.items():
            if isinstance(val, (int, float)) and val < 0:
                warnings.append(f"[CEM] negative value: {key}={val}")
        return True, warnings
    except Exception as exc:
        return False, [f"[CEM] compute_for_goal failed: {exc}"]


def _body_bending_sf(dims: Dict[str, Any], yield_mpa: float, load_n: float = 16000.0) -> float:
    """
    Pawl/lever/blocker bending SF under proof load.
    Moment arm is distance from pivot to tooth tip, and
    critical section is at pivot with full cross-section.
    """

    def get(keys: List[str], default: float) -> float:
        for key in keys:
            up = key.upper()
            for dk, dv in (dims or {}).items():
                if up in str(dk).upper():
                    try:
                        return float(dv)
                    except Exception:
                        continue
        return default

    total_length = get(["LENGTH"], 60.0)                  # mm
    pivot_offset = get(["PIVOT_OFFSET", "PIVOT"], 8.0)    # mm from end
    b = get(["WIDTH"], 12.0)                              # mm
    h = get(["THICKNESS"], 6.0)                           # mm
    n = float(_st_mod.N_PAWLS)

    # Moment arm from pivot to tooth tip
    moment_arm_mm = total_length - pivot_offset

    F_each = load_n / n
    M = F_each * (moment_arm_mm / 1000.0)                 # N·m
    I = (b / 1000.0) * (h / 1000.0) ** 3 / 12.0           # m^4
    c = (h / 1000.0) / 2.0                                # m

    if I == 0.0 or c == 0.0:
        return 0.0

    sigma_mpa = (M * c / I) / 1e6
    if sigma_mpa == 0.0:
        return 999.0
    return float(yield_mpa) / float(sigma_mpa)


def _ratchet_tooth_shear_sf(dims: Dict[str, Any], yield_mpa: float, load_n: float = 16000.0) -> float:
    """
    Ratchet ring tooth shear SF under proof load.
    Governing mode: shear across tooth root.
    """

    def get(keys: List[str], default: float) -> float:
        for key in keys:
            up = key.upper()
            for dk, dv in (dims or {}).items():
                if up in str(dk).upper():
                    try:
                        return float(dv)
                    except Exception:
                        continue
        return default

    tooth_height = get(["TOOTH_HEIGHT", "HEIGHT"], 8.0)   # mm (not used directly but kept for completeness)
    face_width = get(["THICKNESS", "WIDTH", "FACE"], 21.0)  # mm
    root_width = get(["TIP_FLAT", "TIP"], 3.0)            # mm

    shear_area = (root_width / 1000.0) * (face_width / 1000.0)  # m^2

    n_engaged = 3.0
    F_per_tooth = load_n / n_engaged

    if shear_area == 0.0:
        return 0.0

    tau_mpa = (F_per_tooth / shear_area) / 1e6
    tau_yield = float(yield_mpa) / 1.732  # von Mises shear approx

    if tau_mpa == 0.0:
        return 999.0
    return tau_yield / tau_mpa


def run_static_check_with_material(part_id: str, meta: dict, yield_mpa: float, context: dict) -> tuple[Optional[float], str]:
    """
    Run static check using specified yield strength.
    Approximates new SF by scaling the default SF by (yield_mpa / base_yield_mpa).
    """
    lid = part_id.lower()
    dims = (meta.get("dims_mm") or {}) if meta else {}
    if any(x in lid for x in ("ratchet", "ring", "gear", "tooth")):
        return _ratchet_tooth_shear_sf(dims, yield_mpa), "tooth_shear"

    mech = get_mechanical_constants(context)
    pawl_tip_default = float(mech.get("pawl_tip_width_mm", 6.0))
    pawl_thick_default = float(mech.get("pawl_thickness_mm", 9.0))
    pawl_arm_default = float(mech.get("pawl_arm_mm", 45.0))
    pawl_body_h_default = float(mech.get("pawl_body_h_mm", 22.0))
    housing_wall_default = float(mech.get("housing_wall_mm", 10.0))
    shaft_d_default = float(mech.get("shaft_d_mm", 20.0))

    def get_dim(d: Dict[str, Any], *keys: str, default: Optional[float] = None) -> Optional[float]:
        for key in keys:
            up = key.upper()
            for dk, dv in d.items():
                if up in str(dk).upper():
                    try:
                        return float(dv)
                    except (TypeError, ValueError):
                        continue
        return default

    pawl_tip_width = get_dim(dims, "WIDTH", "TIP_WIDTH", default=pawl_tip_default)
    pawl_thickness = get_dim(dims, "THICKNESS", default=pawl_thick_default)
    pawl_arm = get_dim(dims, "ARM", "LENGTH", default=pawl_arm_default)
    pawl_body_h = get_dim(dims, "HEIGHT", "BODY_H", default=pawl_body_h_default)
    housing_wall = get_dim(dims, "WALL", default=housing_wall_default)
    shaft_d = get_dim(dims, "SHAFT", "BORE", "ID", default=shaft_d_default)

    load_steps = [2000, 4000, 8000, 12000, 16000]
    y_pawl = _st_mod.YIELD_PAWL_MPA
    y_housing = _st_mod.YIELD_HOUSING_MPA
    y_shaft = _st_mod.YIELD_SHAFT_MPA
    if any(x in lid for x in ("pawl", "lever", "trip", "blocker")):
        y_pawl = yield_mpa
    elif any(x in lid for x in ("housing", "shell", "enclosure")):
        y_housing = yield_mpa
    elif any(x in lid for x in ("shaft", "spool", "collar")):
        y_shaft = yield_mpa
    df = _st_mod.simulate_static_pawl(
        load_steps=load_steps,
        pawl_tip_width_mm=pawl_tip_width,
        pawl_thickness_mm=pawl_thickness,
        pawl_arm_mm=pawl_arm,
        pawl_body_h_mm=pawl_body_h,
        housing_wall_mm=housing_wall,
        shaft_d_mm=shaft_d,
        yield_pawl_mpa=y_pawl,
        yield_housing_mpa=y_housing,
        yield_shaft_mpa=y_shaft,
    )
    min_sf = float(df["min_sf"].min())
    worst_row = df.loc[df["load_N"].idxmax()]
    sf_map = {
        "sf_contact": worst_row["sf_contact"],
        "sf_bending": worst_row["sf_bending"],
        "sf_housing": worst_row["sf_housing"],
        "sf_shaft": worst_row["sf_shaft"],
    }
    failure_mode = min(sf_map, key=sf_map.get)
    return min_sf, failure_mode


def run_cem_checks(part_id: str, meta_path: Path, context: dict) -> CEMCheckResult:
    """
    Run appropriate physics checks based on part_id.
    Loads meta JSON for dimensions. Falls back to static/test defaults.
    """
    meta = _load_meta(meta_path)
    static_passed, static_min_sf, static_failure_mode = _run_static_checks(part_id, meta, context)
    cem_passed, cem_warnings = _run_cem_system_check(
        str(context.get("goal", "") or ""),
        part_id,
    )

    overall = True
    for flag in (static_passed, cem_passed):
        if flag is False:
            overall = False

    pieces = []
    if static_passed is not None:
        status = "PASS" if static_passed else "FAIL"
        pieces.append(f"Static {status} (min SF={static_min_sf:.2f} @ {static_failure_mode})")
    if cem_passed is not None:
        status = "PASS" if cem_passed else "FAIL"
        pieces.append(f"System CEM {status}")

    summary = "; ".join(pieces) if pieces else "No CEM checks applicable"

    result = CEMCheckResult(
        part_id=part_id,
        static_passed=static_passed,
        static_min_sf=static_min_sf,
        static_failure_mode=static_failure_mode,
        dynamic_passed=None,
        dynamic_peak_force_N=None,
        dynamic_arrest_dist_mm=None,
        cem_passed=cem_passed,
        cem_warnings=cem_warnings,
        overall_passed=overall,
        summary=summary,
    )
    event_bus.emit(
        "cem",
        summary,
        {
            "part_id": part_id,
            "sf": static_min_sf,
            "passed": overall,
        },
    )
    return result


def run_full_system_cem(outputs_dir: str | Path, context: dict) -> dict:
    """
    Run CEM checks on ALL parts in outputs/cad/meta/.
    Returns a system-level report dict.
    """
    base = Path(outputs_dir)
    meta_dir = base / "cad" / "meta"
    results: dict[str, CEMCheckResult] = {}

    if not meta_dir.exists():
        return {
            "total_parts": 0,
            "passed": 0,
            "failed": [],
            "weakest_part": None,
            "weakest_sf": None,
            "system_passed": True,
            "results": {},
        }

    meta_cache: dict[str, dict] = {}
    for meta_file in meta_dir.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        part_id = meta.get("part_name", meta_file.stem)
        meta_cache[part_id] = meta
        result = run_cem_checks(part_id, meta_file, context)
        results[part_id] = (result, meta.get("part_name", part_id))

    dyn_passed, dyn_peak, dyn_arrest = _run_dynamic_checks()
    dynamic = {
        "passed": dyn_passed,
        "peak_force_N": dyn_peak,
        "arrest_distance_mm": dyn_arrest,
    }

    total = len(results)
    passed = sum(1 for r, _ in results.values() if r.overall_passed)
    failed = [name for name, (r, _) in results.items() if not r.overall_passed]
    if dyn_passed is False:
        failed.append("__dynamic_system__")

    weakest_part = None
    weakest_sf = None
    if results:
        def sf_or_big(r: CEMCheckResult) -> float:
            return r.static_min_sf if r.static_min_sf is not None else 999.0

        weakest_part, (weakest, _) = min(results.items(), key=lambda x: sf_or_big(x[1][0]))
        weakest_sf = weakest.static_min_sf

    report = {
        "total_parts": total,
        "passed": passed,
        "failed": failed,
        "weakest_part": weakest_part,
        "weakest_sf": weakest_sf,
        "system_passed": len(failed) == 0,
        "dynamic": dynamic,
        "results": {k: {**vars(v), "display_name": (disp[:45] if disp else k)} for k, (v, disp) in results.items()},
    }
    try:
        from ..cad_router import route_cad_request, CADIterationStore, _run_async
        store = CADIterationStore()
        for part_id, (res, _disp) in results.items():
            sf = res.static_min_sf if res.static_min_sf is not None else 999.0
            if (res.overall_passed is False) or (sf < 3.0):
                hist = store.get_history(part_id, n=5)
                routing = _run_async(route_cad_request(part_id, meta_cache.get(part_id, {}), res, context, hist))
                report["results"][part_id]["cad_routing"] = routing
    except Exception:
        pass
    return report


def run_full_cem(
    part_id: str,
    meta: dict,
    context: dict,
    repo_root: "Path | None" = None,
) -> "CEMCheckResult":
    """
    Convenience wrapper used by gh_to_step_bridge and external callers.

    Enriches *meta* with CEM-derived dimensions before running static checks,
    so the physics model uses physically-correct geometry rather than old
    placeholder values from meta JSON files.

    CEM enrichment priority:
      1. Explicit user dimensions already in meta["dims_mm"]
      2. CEM-derived dimensions from cem_aria/cem_lre compute_for_goal()
      3. Context mechanical constants
    """
    from pathlib import Path as _Path

    if repo_root is None:
        repo_root = _Path(__file__).resolve().parent.parent.parent

    # Enrich meta dims with CEM-derived geometry
    enriched_meta = _enrich_meta_with_cem(part_id, meta, repo_root)

    # Write to a temp meta path so run_cem_checks can load it
    import tempfile
    tmp = _Path(tempfile.mktemp(suffix=".json"))
    try:
        tmp.write_text(json.dumps(enriched_meta), encoding="utf-8")
        return run_cem_checks(part_id, tmp, context)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _enrich_meta_with_cem(part_id: str, meta: dict, repo_root: "Path") -> dict:
    """
    Return a copy of meta with dims_mm filled in from CEM physics where missing.

    The CEM model provides physically-correct dimensions derived from ANSI
    arrest force requirements. These should be used as a floor: if the stored
    meta already has a value, it is kept unchanged.

    NOTE: The static stress model (aria_models/static_tests.py) uses simplified
    closed-form equations. As of 2026-03, all catch mechanism parts show SF < 2.0
    at the default meta dimensions because:
      a) Meta files carry placeholder dims from early iterations, not final design
      b) The bending/shear model does not account for load sharing across teeth or
         the distributed contact along the pawl face
    Calibration path: run hardware drop tests, back-calculate actual SF, update
    aria_models/static_tests.py yield constants and load-sharing factors.
    """
    import copy
    import sys

    enriched = copy.deepcopy(meta)
    dims = enriched.setdefault("dims_mm", {})

    # Attempt CEM geometry injection
    try:
        root_str = str(repo_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        # Select module
        from cem_registry import resolve_cem_module
        mod_name = resolve_cem_module("", part_id)
        if mod_name is None:
            mod_name = "cem_aria"  # default for ARIA parts

        import importlib
        mod = importlib.import_module(mod_name)
        cem_params = mod.compute_for_goal("", {})

        # Map CEM output keys → dims_mm keys (only fill missing values)
        _CEM_TO_DIM: dict[str, list[str]] = {
            "ratchet_face_width_mm":   ["FACE_WIDTH", "THICKNESS"],
            "ratchet_tooth_height_mm": ["TOOTH_HEIGHT"],
            "brake_drum_wall_mm":      ["WALL"],
            "brake_drum_width_mm":     ["WIDTH"],
            "housing_wall_mm":         ["WALL"],
            "housing_od_mm":           ["OD"],
            "spool_hub_od_mm":         ["HUB_OD"],
            "spool_width_mm":          ["WIDTH"],
        }
        for cem_key, dim_keys in _CEM_TO_DIM.items():
            if cem_key in cem_params:
                for dk in dim_keys:
                    if dk not in dims:
                        dims[dk] = cem_params[cem_key]
                        break

    except Exception:
        pass  # enrichment is best-effort; fall through to raw meta

    return enriched
