from __future__ import annotations

"""Parametric sweep optimizer for ARIA-OS generated CadQuery parts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

import re
import json

from .context_loader import load_context
from . import exporter
from . import validator
from .cem_checks import run_static_check_with_material


@dataclass
class OptimizationResult:
    part_name: str
    goal: str
    constraints: List[str]
    iterations: int
    best_params: Dict[str, float] = field(default_factory=dict)
    best_score: float = 0.0
    best_step_path: str = ""
    all_results: List[Dict[str, Any]] = field(default_factory=list)
    converged: bool = False
    summary: str = ""


class PartOptimizer:
    """Simple parametric sweep optimizer driven by module-level numeric constants."""

    def __init__(self, repo_root: Optional[Path] = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = Path(repo_root)
        self.context = load_context(self.repo_root)

    def optimize(
        self,
        base_code_path: str,
        goal: str,
        constraints: List[str],
        context: Optional[dict] = None,
        max_iterations: int = 20,
    ) -> OptimizationResult:
        """
        Parametric sweep with constraint filtering.

        base_code_path: path to generated code (.py)
        goal: "minimize_weight" | "maximize_sf" | "minimize_size"
        constraints: e.g. ["SF>=2.0", "wall>=6mm", "OD<=220mm"]
        """
        if context is None:
            context = self.context

        code_path = Path(base_code_path)
        if not code_path.is_absolute():
            code_path = (self.repo_root / base_code_path).resolve()
        if not code_path.exists():
            return OptimizationResult(
                part_name=code_path.stem,
                goal=goal,
                constraints=constraints,
                iterations=0,
                converged=False,
                summary=f"Base code not found: {code_path}",
            )

        base_code = code_path.read_text(encoding="utf-8")
        part_name = code_path.stem

        params = _extract_module_constants(base_code)
        if not params:
            return OptimizationResult(
                part_name=part_name,
                goal=goal,
                constraints=constraints,
                iterations=0,
                converged=False,
                summary="No module-level numeric constants found to tune.",
            )

        sweep_plan = _build_sweep_plan(goal, params)
        if not sweep_plan:
            return OptimizationResult(
                part_name=part_name,
                goal=goal,
                constraints=constraints,
                iterations=0,
                converged=False,
                summary=f"No sweepable parameters for goal '{goal}'.",
            )

        # Generate parameter sets (simple 1D sweeps per selected parameter)
        param_sets: List[Dict[str, float]] = []
        for pname, values in sweep_plan.items():
            for v in values:
                pcopy = dict(params)
                pcopy[pname] = v
                param_sets.append(pcopy)

        results: List[Dict[str, Any]] = []
        best_score: Optional[float] = None
        best_params: Dict[str, float] = {}
        best_step_path: str = ""
        iterations = 0

        for idx, pvals in enumerate(param_sets, start=1):
            if iterations >= max_iterations:
                break
            iterations += 1

            variant_name = f"{part_name}_opt{idx}"
            step_path, stl_path = _get_paths_for_variant(variant_name, self.repo_root)
            modified_code = _inject_params_into_code(base_code, pvals)

            inject = {"STEP_PATH": str(step_path), "STL_PATH": str(stl_path), "PART_NAME": variant_name}
            step_path.parent.mkdir(parents=True, exist_ok=True)
            stl_path.parent.mkdir(parents=True, exist_ok=True)

            # Check parameter-only constraints before expensive validation
            param_constraints_ok = all(
                _eval_constraint(c, pvals, None) for c in constraints
            )
            if not param_constraints_ok:
                continue

            vres = validator.validate(
                modified_code,
                expected_bbox=None,
                step_path=step_path,
                min_step_size_kb=1.0,
                inject_namespace=inject,
            )
            if not vres.passed:
                results.append(
                    {
                        "params": dict(pvals),
                        "passed": False,
                        "reason": vres.error or "; ".join(vres.errors),
                    }
                )
                continue

            meta_path = Path(exporter.get_meta_path(variant_name, self.repo_root))
            meta: Dict[str, Any] = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}

            # Use calibrated static check directly for optimization scoring.
            part_id_lower = variant_name.lower()
            if any(x in part_id_lower for x in ("pawl", "lever", "trip", "blocker", "ratchet", "ring")):
                baseline_yield = 1470.0  # 4340 steel baseline for catch parts
            else:
                baseline_yield = 276.0   # 6061 baseline for others

            sf_val, failure_mode = run_static_check_with_material(
                variant_name, meta, baseline_yield, context
            )

            class _CEMProxy:
                def __init__(self, sf: Optional[float]):
                    self.static_min_sf = sf
                    self.dynamic_peak_force_N = None
                    self.dynamic_arrest_dist_mm = None
                    # Treat presence of an SF as overall pass; constraints enforce thresholds.
                    self.overall_passed = sf is not None

            cem_result = _CEMProxy(sf_val)

            # Basic metrics for scoring
            bbox = None
            weight_g = None
            sf = cem_result.static_min_sf
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    bbox = meta.get("bbox_mm") or {}
                    vol_mm3 = float(bbox.get("x", 0.0)) * float(bbox.get("y", 0.0)) * float(bbox.get("z", 0.0))
                    # Approximate weight using 6061 density and fill factor
                    density = 2700.0  # kg/m^3
                    fill_factor = 0.6
                    weight_kg = vol_mm3 * 1e-9 * density * fill_factor
                    weight_g = weight_kg * 1000.0
                except Exception:
                    pass

            score = _compute_score(goal, sf, weight_g, bbox)

            # Enforce all constraints (CEM + parameter) with actual CEM result
            constraint_ok = all(_eval_constraint(c, pvals, cem_result) for c in constraints)

            entry: Dict[str, Any] = {
                "params": dict(pvals),
                "score": score,
                "sf": sf,
                "weight_g": weight_g,
                "passed": constraint_ok and cem_result.overall_passed,
                "cem_overall_passed": cem_result.overall_passed,
                "step_path": str(step_path),
            }
            results.append(entry)

            if not entry["passed"]:
                continue

            if best_score is None or score > best_score:
                best_score = score
                best_params = dict(pvals)
                best_step_path = str(step_path)

        converged = best_score is not None
        if not converged and results:
            # Fall back to best-by-score even if constraints not fully satisfied
            best_candidate = max(results, key=lambda r: r.get("score", float("-inf")))
            best_score = best_candidate.get("score", 0.0)
            best_params = best_candidate.get("params", {})
            best_step_path = best_candidate.get("step_path", "")

        summary = _summarize_result(goal, converged, best_score, best_params, constraints)

        return OptimizationResult(
            part_name=part_name,
            goal=goal,
            constraints=constraints,
            iterations=iterations,
            best_params=best_params,
            best_score=best_score or 0.0,
            best_step_path=best_step_path,
            all_results=results,
            converged=converged,
            summary=summary,
        )

    def optimize_and_regenerate(
        self,
        base_code_path: str,
        goal: str,
        constraints: list[str],
        context: dict,
        material: str = None,
        max_iterations: int = 20,
    ) -> dict:
        """
        1. Run optimize() to find best params
        2. If converged: build a generation prompt from optimized params + material
        3. Run orchestrator.run() with that prompt
        4. Return both optimization and generation results
        """
        opt_result = self.optimize(
            base_code_path=base_code_path,
            goal=goal,
            constraints=constraints,
            context=context,
            max_iterations=max_iterations,
        )

        # If material not specified, run material study to get recommendation.
        recommended_material = material
        if recommended_material is None:
            try:
                from .material_study import run_material_study
                outputs_dir = self.repo_root / "outputs"
                study = run_material_study(Path(base_code_path).stem, context, outputs_dir)
                if study and getattr(study, "recommendation", None):
                    recommended_material = study.recommendation.name
            except Exception:
                recommended_material = None

        combined: dict = {
            "optimization": opt_result,
            "generation": None,
            "optimized_params": opt_result.best_params,
            "recommended_material": recommended_material,
            "output_step": "",
            "summary": "",
        }

        if not opt_result.converged:
            combined["summary"] = (
                f"Did not converge for {opt_result.part_name} "
                f"(iterations={opt_result.iterations}); skipping regeneration."
            )
            return combined

        optimized_prompt = self._params_to_prompt(
            base_code_path=base_code_path,
            best_params=opt_result.best_params,
            material=recommended_material,
            context=context,
        )

        try:
            from .orchestrator import run as aria_run
            gen_result = aria_run(optimized_prompt, repo_root=self.repo_root)
        except Exception as e:
            combined["summary"] = f"Optimization converged but regeneration failed: {e}"
            return combined

        combined["generation"] = gen_result
        combined["output_step"] = (gen_result or {}).get("step_path", "")

        # Build a short summary string
        part_name = Path(base_code_path).stem
        if opt_result.best_params:
            p0 = next(iter(opt_result.best_params.items()))
            combined["summary"] = (
                f"Optimized {part_name}: {p0[0]}→{p0[1]} (goal={goal}), "
                f"material={recommended_material or 'N/A'}"
            )
        else:
            combined["summary"] = (
                f"Optimized {part_name} (goal={goal}), material={recommended_material or 'N/A'}"
            )
        return combined

    def _params_to_prompt(
        self,
        base_code_path: str,
        best_params: dict,
        material: str,
        context: dict,
    ) -> str:
        """
        Read the original generated code to extract part type and feature hints,
        then build a natural language prompt with optimized dimensions injected.
        """
        code_path = Path(base_code_path)
        if not code_path.is_absolute():
            code_path = (self.repo_root / base_code_path).resolve()
        code = code_path.read_text(encoding="utf-8", errors="ignore") if code_path.exists() else ""

        stem = code_path.stem
        # Turn filename into a human-ish part name
        part_name = stem
        part_name = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_", "", part_name)
        part_name = part_name.replace("_", " ").strip()

        # Attempt to preserve feature descriptions from comments
        feature_lines: list[str] = []
        for line in (code or "").splitlines():
            s = line.strip()
            if s.startswith("#") and "PART PARAMETERS" not in s and "META JSON" not in s and "END PARAMETERS" not in s:
                # keep short, human comments only
                t = s.lstrip("#").strip()
                if t and len(t) < 140:
                    feature_lines.append(t)
        feature_blob = "; ".join(feature_lines[:8])

        # Build params fragment
        params_frag = ", ".join(
            f"{k} {v}mm" if str(k).upper().endswith("_MM") else f"{k} {v}"
            for k, v in (best_params or {}).items()
        )

        mat_frag = f", material {material}" if material else ""
        extra = f", preserve features: {feature_blob}" if feature_blob else ""

        return f"generate {part_name} optimized: {params_frag}{mat_frag}{extra}"


def _extract_module_constants(code: str) -> Dict[str, float]:
    """Find simple ALL_CAPS = number constants at module top-level."""
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE)
    params: Dict[str, float] = {}
    for m in pattern.finditer(code):
        name, val = m.group(1), float(m.group(2))
        params[name] = val
    return params


def _find_param(params: Dict[str, float], *keywords: str) -> Optional[Tuple[str, float]]:
    """Find first param whose name contains any of the given keywords."""
    for key in params:
        upper = key.upper()
        for kw in keywords:
            if kw.upper() in upper:
                return key, params[key]
    return None


def _build_sweep_plan(goal: str, params: Dict[str, float]) -> Dict[str, List[float]]:
    """Decide which parameters to sweep and their value ranges."""
    goal = goal.strip().lower()
    sweep: Dict[str, List[float]] = {}

    if goal == "minimize_weight":
        cand = _find_param(params, "THICKNESS", "WALL", "HEIGHT")
        if not cand:
            return {}
        name, base = cand
        values: List[float] = []
        step = 0.5
        v = base
        while v >= max(0.5, base * 0.4):
            values.append(round(v, 3))
            v -= step
        sweep[name] = values
    elif goal == "maximize_sf":
        cand = _find_param(params, "THICKNESS", "WALL", "ENGAGEMENT")
        if not cand:
            return {}
        name, base = cand
        values: List[float] = []
        current_val = float(base)
        step = max(1.0, current_val * 0.2)  # 20% steps, at least 1mm
        v = current_val
        # Sweep upward up to 4x current or until we hit a reasonable count
        while v <= current_val * 4.0 and len(values) < 20:
            values.append(round(v, 2))
            v += step
        sweep[name] = values
    elif goal == "minimize_size":
        cand = _find_param(params, "OD", "OUTER", "LENGTH", "RADIUS")
        if not cand:
            return {}
        name, base = cand
        values = []
        step = 2.0
        v = base
        while v >= max(10.0, base * 0.8):
            values.append(round(v, 3))
            v -= step
        sweep[name] = values
    return sweep


def _inject_params_into_code(code: str, params: Dict[str, float]) -> str:
    """Replace module-level constant assignments with new numeric values."""
    for name, value in params.items():
        pattern = re.compile(rf"^({name}\s*=\s*)\d+(?:\.\d+)?\s*$", re.MULTILINE)
        replacement_val = str(value)
        code, _ = pattern.subn(lambda m: m.group(1) + replacement_val, code)
    return code


_CEM_METRICS = {
    "SF": lambda r: r.static_min_sf,
    "MIN_SF": lambda r: r.static_min_sf,
    "PEAK_FORCE": lambda r: r.dynamic_peak_force_N,
    "ARREST_DIST": lambda r: r.dynamic_arrest_dist_mm,
}


def _eval_constraint(constraint: str, params: Dict[str, float], cem_result: Any) -> bool:
    """
    Evaluate a single constraint string against params and optional CEM result.
    Examples: "SF>=2.0", "THICKNESS_MM>=4.0", "OD<=220mm".
    """
    if not constraint:
        return True
    text = constraint.upper().replace("MM", "")
    m = re.match(r"([A-Z_]+)\s*(>=|<=|>|<|==)\s*([0-9.]+)", text)
    if not m:
        return True
    name, op, val = m.group(1), m.group(2), float(m.group(3))

    # Determine actual value: CEM metric vs parameter
    actual = None
    if name in _CEM_METRICS and cem_result is not None:
        actual = _CEM_METRICS[name](cem_result)
        # Debug: show SF when checked
        if name in ("SF", "MIN_SF"):
            print(f"[OPT] Checking SF constraint: actual={actual}, target {op} {val}")
        if actual is None:
            return True
    else:
        for k, v in params.items():
            if name in k.upper():
                actual = v
                break
        if actual is None:
            return True

    if op == ">=":
        return actual >= val
    if op == "<=":
        return actual <= val
    if op == ">":
        return actual > val
    if op == "<":
        return actual < val
    if op == "==":
        return actual == val
    return True


def _compute_score(
    goal: str,
    sf: Optional[float],
    weight_g: Optional[float],
    bbox: Optional[Dict[str, Any]],
) -> float:
    goal = goal.strip().lower()
    if goal == "maximize_sf":
        return float(sf or 0.0)
    if goal == "minimize_weight":
        return -float(weight_g or 0.0)
    if goal == "minimize_size":
        if not bbox:
            return 0.0
        v = float(bbox.get("x", 0.0)) * float(bbox.get("y", 0.0)) * float(bbox.get("z", 0.0))
        return -v
    return 0.0


def _get_paths_for_variant(name: str, repo_root: Path) -> Tuple[Path, Path]:
    paths = exporter.get_output_paths(name, repo_root)
    return Path(paths["step_path"]), Path(paths["stl_path"])


def _summarize_result(
    goal: str,
    converged: bool,
    best_score: Optional[float],
    best_params: Dict[str, float],
    constraints: List[str],
) -> str:
    if not converged:
        return "No variant satisfied all constraints; best candidate reported for inspection."
    return (
        f"Goal={goal}, best_score={best_score}, "
        f"params={best_params}, constraints={constraints}"
    )

