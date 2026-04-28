"""design_space.py — N-variant search + Pareto ranking.

Given a parametric template (e.g. L-bracket(width, height, thickness,
n_bolts)), sweep a grid of values, run FEA + DFM + cost on each, and
emit a Pareto-ranked summary. The "AI engineering" pitch:

    "We don't just generate one part. We explore the space, validate
     every variant under your load case, and tell you which 3 hit
     your cost / weight / strength target."

The user feeds in a template name + a small dict of (param -> [values]).
Total variants = product of value-list sizes. With 4 params at 3 values
each, that's 81 variants — runnable in ~2 minutes per part if FEA is
on the closed-form path, ~5 minutes per part with CalculiX.

Outputs a contact-sheet markdown + JSON, plus per-variant artifacts
under outputs/design_space/<run_id>/v_<index>/.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from itertools import product
from pathlib import Path
import json
import time
from typing import Optional


@dataclass
class Variant:
    index: int
    params: dict
    step_path: Optional[str] = None
    stl_path: Optional[str] = None
    bbox_mm: tuple = (0.0, 0.0, 0.0)
    volume_mm3: float = 0.0
    mass_g: float = 0.0
    fea_max_stress_mpa: Optional[float] = None
    fea_safety_factor: Optional[float] = None
    fea_passed: Optional[bool] = None
    dfm_issue_count: int = 0
    dfm_critical_count: int = 0
    machining_time_min: Optional[float] = None
    print_time_hr: Optional[float] = None
    cost_usd: Optional[float] = None
    pareto_rank: int = 0
    notes: str = ""


@dataclass
class DesignSpaceReport:
    ok: bool
    template: str
    n_variants: int
    n_evaluated: int
    n_passed_fea: int
    n_passed_all: int
    pareto_indices: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    out_dir: str = ""
    elapsed_s: float = 0.0


def _expand_grid(grid: dict[str, list]) -> list[dict]:
    """{'thickness_mm':[3,5,8], 'n_bolts':[4,6]} → 6 dicts."""
    keys = list(grid.keys())
    out = []
    for combo in product(*[grid[k] for k in keys]):
        out.append({k: v for k, v in zip(keys, combo)})
    return out


def _generate_variant(template: str, params: dict, out_dir: Path,
                       index: int) -> Variant:
    """Generate one CAD artifact via the cadquery_generator template."""
    from aria_os.generators.cadquery_generator import (
        _CQ_TEMPLATE_MAP, _find_template_fuzzy)
    v = Variant(index=index, params=params)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve template
    fn = _CQ_TEMPLATE_MAP.get(template)
    if fn is None:
        # try the fuzzy resolver
        try:
            fn, _ = _find_template_fuzzy(template, template, params)
        except Exception:
            fn = None
    if fn is None:
        v.notes = f"template '{template}' not found"
        return v

    code = fn(params)
    code_path = out_dir / "part.py"
    code_path.write_text(code, encoding="utf-8")

    # Run the cadquery code in a fresh subprocess to avoid OCP module
    # collision when iterating dozens of variants in one process.
    import subprocess
    step_path = out_dir / "part.step"
    stl_path = out_dir / "part.stl"
    runner = (
        f"import cadquery as cq\n"
        f"exec(open(r'{code_path}','r',encoding='utf-8').read())\n"
        f"cq.exporters.export(result, r'{step_path}')\n"
        f"cq.exporters.export(result, r'{stl_path}')\n"
    )
    try:
        r = subprocess.run(
            ["python", "-c", runner],
            capture_output=True, text=True, timeout=60)
        if step_path.is_file():
            v.step_path = str(step_path)
        if stl_path.is_file():
            v.stl_path = str(stl_path)
        # Extract bbox from cadquery's stdout if possible
        if "BBOX:" in r.stdout:
            for ln in r.stdout.splitlines():
                if ln.startswith("BBOX:"):
                    parts = ln[5:].split(",")
                    if len(parts) == 3:
                        v.bbox_mm = tuple(float(x) for x in parts)
                    break
        if not v.step_path:
            v.notes = (r.stderr or r.stdout)[:200]
    except subprocess.TimeoutExpired:
        v.notes = "cadquery generation timed out"
    except Exception as ex:
        v.notes = f"gen threw: {type(ex).__name__}: {ex}"
    return v


def _evaluate_variant(v: Variant, *, material: str, load_n: float,
                        target_sf: float) -> Variant:
    """Run FEA + DFM + cost on a generated variant."""
    if not v.stl_path or not Path(v.stl_path).is_file():
        return v
    # Volume + mass via trimesh
    try:
        import trimesh
        m = trimesh.load_mesh(v.stl_path)
        v.volume_mm3 = float(m.volume)
        # density lookup
        from aria_os.fea.materials import resolve as _resolve_mat
        mat = _resolve_mat(material)
        rho = mat.density_kg_m3 if mat else 2700.0
        v.mass_g = v.volume_mm3 * rho * 1e-6
    except Exception as ex:
        v.notes = (v.notes + f" | mass calc threw: {ex}")[:300]

    # FEA via auto_fea (closed-form path is fine for design space —
    # CCX would be ideal but cost-prohibitive at 81 variants).
    # The "primary" key is whichever tier won, but each tier has a
    # different result shape — handle all three explicitly.
    try:
        from aria_os.fea.auto_fea import auto_fea
        fea = auto_fea(v.stl_path, material=material, load_n=load_n,
                        target_sf=target_sf,
                        out_dir=str(Path(v.stl_path).parent / "fea"))
        # SW Simulation shape: {"ok":bool, "result":{"iterations":[{...}]}}
        sw = fea.get("sw_simulation") or {}
        sw_iter = ((sw.get("result") or {}).get("iterations") or [{}])[0] \
                    if sw.get("ok") else {}
        # CalculiX shape: {"available":bool, "passed":bool, "max_stress_mpa":..., "safety_factor":...}
        ccx = fea.get("calculix") or {}
        # Pick whichever has a stress reading
        if sw_iter.get("max_stress_mpa") is not None:
            v.fea_max_stress_mpa = sw_iter.get("max_stress_mpa")
            v.fea_safety_factor = sw_iter.get("safety_factor")
            v.fea_passed = (v.fea_safety_factor is not None and
                              v.fea_safety_factor >= target_sf)
        elif ccx.get("max_stress_mpa") is not None:
            v.fea_max_stress_mpa = ccx.get("max_stress_mpa")
            v.fea_safety_factor = ccx.get("safety_factor")
            v.fea_passed = bool(ccx.get("passed"))
        else:
            cf = fea.get("closed_form") or {}
            v.fea_passed = bool(cf.get("passed")) if cf.get("ok") else False
    except Exception as ex:
        v.notes = (v.notes + f" | fea threw: {ex}")[:300]

    # DFM — cnc rules
    try:
        from aria_os.verification.dfm import run_dfm_rules
        spec = {"material": material, **v.params}
        dfm = run_dfm_rules(spec, v.stl_path, "cnc")
        issues = (dfm.issues if hasattr(dfm, "issues") else dfm) or []
        v.dfm_issue_count = len(issues)
        v.dfm_critical_count = sum(1 for i in issues
                                     if getattr(i, "level", "") == "critical")
    except Exception:
        # DFM is optional — skip on failure
        pass

    # Cost / time
    try:
        from aria_os.agents.quote_tools import (
            extract_geometry_for_quote, estimate_machining_time,
            estimate_print_time_hr, get_material_rate)
        if v.step_path:
            geo = extract_geometry_for_quote(v.step_path)
            t_min = estimate_machining_time(geo, material=material)
            v.machining_time_min = float(t_min) if t_min else None
            mrate = get_material_rate(material)
            mat_cost = (v.mass_g / 1000.0) * mrate.get("usd_per_kg", 5.0)
            mach_cost = (v.machining_time_min or 0.0) * \
                          mrate.get("usd_per_min", 1.0)
            v.cost_usd = round(mat_cost + mach_cost, 2)
        # Print time (assume FDM)
        if v.stl_path and v.bbox_mm[2] > 0:
            v.print_time_hr = float(estimate_print_time_hr(
                v.bbox_mm[0], v.bbox_mm[1], v.bbox_mm[2],
                v.volume_mm3))
    except Exception:
        pass
    return v


def _pareto_rank(variants: list[Variant]) -> list[int]:
    """Return Pareto frontier indices on (mass, cost, -SF). Lower
    mass + lower cost + higher SF = better. We invert SF so all
    objectives are 'minimize'.
    """
    valid = [v for v in variants if v.fea_passed and
                v.mass_g > 0 and v.cost_usd is not None
                and v.fea_safety_factor is not None]
    pf: list[int] = []
    for i, vi in enumerate(valid):
        dominated = False
        for j, vj in enumerate(valid):
            if i == j:
                continue
            if (vj.mass_g <= vi.mass_g and vj.cost_usd <= vi.cost_usd
                    and vj.fea_safety_factor >= vi.fea_safety_factor
                    and (vj.mass_g < vi.mass_g
                         or vj.cost_usd < vi.cost_usd
                         or vj.fea_safety_factor > vi.fea_safety_factor)):
                dominated = True
                break
        if not dominated:
            pf.append(vi.index)
    return pf


def _generate_and_evaluate(args: tuple) -> Variant:
    """Pool worker — must be top-level for pickling."""
    template, params, v_dir_str, index, material, load_n, target_sf = args
    v_dir = Path(v_dir_str)
    v = _generate_variant(template, params, v_dir, index)
    v = _evaluate_variant(v, material=material, load_n=load_n,
                            target_sf=target_sf)
    return v


def explore_design_space(template: str,
                          grid: dict[str, list],
                          *,
                          material: str = "aluminum_6061",
                          load_n: float = 500.0,
                          target_sf: float = 2.0,
                          out_dir: str | Path | None = None,
                          max_variants: int = 50,
                          parallel: bool = True,
                          n_workers: int | None = None) -> DesignSpaceReport:
    """Sweep a grid of params; FEA + DFM + cost each; rank.

    Returns DesignSpaceReport with the Pareto frontier marked. With
    `parallel=True` (default), variants are evaluated via
    ProcessPoolExecutor — typically 5-8x speedup on a 4-core box.
    """
    t0 = time.time()
    combos = _expand_grid(grid)
    if len(combos) > max_variants:
        # Subsample evenly so the user gets coverage without
        # blowing past max_variants.
        step = max(1, len(combos) // max_variants)
        combos = combos[::step][:max_variants]
    out_dir = Path(out_dir or
                    f"outputs/design_space/{template}_{int(t0)}")
    out_dir.mkdir(parents=True, exist_ok=True)

    work_items = [
        (template, params, str(out_dir / f"v_{i:03d}"), i,
         material, load_n, target_sf)
        for i, params in enumerate(combos)]

    variants: list[Variant] = []
    if parallel and len(work_items) > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        import os
        n_workers = n_workers or max(1, (os.cpu_count() or 4) - 1)
        n_workers = min(n_workers, len(work_items))
        try:
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_generate_and_evaluate, w): w
                            for w in work_items}
                for f in as_completed(futures):
                    try:
                        v = f.result(timeout=300)
                    except Exception as ex:
                        # Recover the original index from the work item
                        wi = futures[f]
                        v = Variant(index=wi[3], params=wi[1],
                                     notes=f"worker raised: {type(ex).__name__}: {ex}")
                    variants.append(v)
                    print(f"[ds-par] v{v.index:03d} mass="
                            f"{v.mass_g:.1f}g sf={v.fea_safety_factor} "
                            f"cost=${v.cost_usd}")
        except Exception as ex:
            print(f"[ds] pool failed ({ex}) — falling back to serial")
            parallel = False
    if not parallel or not variants:
        for w in work_items:
            v = _generate_and_evaluate(w)
            variants.append(v)
            print(f"[ds] v{v.index:03d} mass={v.mass_g:.1f}g "
                    f"sf={v.fea_safety_factor} cost=${v.cost_usd}")
    # Variants come back out of order under parallel; restore order
    variants.sort(key=lambda v: v.index)

    pf_idx = _pareto_rank(variants)
    for idx in pf_idx:
        variants[idx].pareto_rank = 1

    n_eval = sum(1 for v in variants if v.step_path)
    n_pass_fea = sum(1 for v in variants if v.fea_passed)
    n_pass_all = sum(1 for v in variants if v.fea_passed
                       and v.dfm_critical_count == 0)

    report = DesignSpaceReport(
        ok=True, template=template,
        n_variants=len(combos), n_evaluated=n_eval,
        n_passed_fea=n_pass_fea, n_passed_all=n_pass_all,
        pareto_indices=pf_idx,
        variants=[asdict(v) for v in variants],
        out_dir=str(out_dir),
        elapsed_s=round(time.time() - t0, 2))

    rp = out_dir / "design_space_report.json"
    rp.write_text(
        json.dumps(asdict(report), indent=2, default=str),
        encoding="utf-8")
    # Also emit a markdown contact sheet
    _emit_contact_sheet(report, out_dir / "contact_sheet.md")
    return report


def _emit_contact_sheet(report: DesignSpaceReport, path: Path) -> None:
    lines = [f"# Design space exploration — {report.template}",
             "",
             f"_Generated in {report.elapsed_s:.1f}s_",
             "",
             f"- **{report.n_variants}** variants generated",
             f"- **{report.n_evaluated}** evaluated successfully",
             f"- **{report.n_passed_fea}** passed FEA at SF target",
             f"- **{report.n_passed_all}** passed FEA + DFM",
             f"- **{len(report.pareto_indices)}** on Pareto frontier",
             "",
             "## Pareto frontier (mass × cost × strength)",
             "",
             "| idx | params | mass(g) | SF | cost(\\$) | print(hr) | DFM crit |",
             "|-----|--------|---------|----|---------|-----------|----------|"]
    pf = set(report.pareto_indices)
    for v in report.variants:
        if v["index"] in pf:
            params_str = ", ".join(f"{k}={v_}" for k, v_ in v["params"].items())
            lines.append(
                f"| {v['index']} | {params_str} | "
                f"{(v['mass_g'] or 0):.1f} | "
                f"{v['fea_safety_factor']} | "
                f"{v['cost_usd']} | "
                f"{(v['print_time_hr'] or 0):.2f} | "
                f"{v['dfm_critical_count']} |")
    lines.append("\n## All variants")
    lines.append("")
    lines.append("| idx | params | mass(g) | SF | passed | cost(\\$) |")
    lines.append("|-----|--------|---------|----|--------|---------|")
    for v in report.variants:
        params_str = ", ".join(f"{k}={v_}" for k, v_ in v["params"].items())
        flag = "✓" if v["fea_passed"] else "✗"
        if v["index"] in pf:
            flag = "★ " + flag
        lines.append(
            f"| {v['index']} | {params_str} | "
            f"{(v['mass_g'] or 0):.1f} | "
            f"{v['fea_safety_factor']} | {flag} | "
            f"{v['cost_usd']} |")
    path.write_text("\n".join(lines), encoding="utf-8")


def _cli():
    import argparse
    ap = argparse.ArgumentParser(
        description="Sweep a parametric design space and Pareto-rank.")
    ap.add_argument("template",
                     help="cadquery template name, e.g. 'l_bracket'")
    ap.add_argument("--grid", required=True,
                     help='JSON dict of param→[values], e.g. '
                          "'{\"thickness_mm\":[3,5,8],\"n_bolts\":[2,4,6]}'")
    ap.add_argument("--material", default="aluminum_6061")
    ap.add_argument("--load-n", type=float, default=500.0)
    ap.add_argument("--target-sf", type=float, default=2.0)
    ap.add_argument("--max-variants", type=int, default=50)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--serial", action="store_true",
                     help="disable multi-process pool (debugging)")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    grid = json.loads(args.grid)
    rep = explore_design_space(
        args.template, grid,
        material=args.material, load_n=args.load_n,
        target_sf=args.target_sf,
        out_dir=args.out_dir, max_variants=args.max_variants,
        parallel=not args.serial, n_workers=args.workers)
    print(json.dumps({
        "ok": rep.ok, "n_variants": rep.n_variants,
        "n_evaluated": rep.n_evaluated,
        "n_passed_fea": rep.n_passed_fea,
        "n_passed_all": rep.n_passed_all,
        "pareto_count": len(rep.pareto_indices),
        "out_dir": rep.out_dir,
    }, indent=2))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = ["Variant", "DesignSpaceReport", "explore_design_space"]
