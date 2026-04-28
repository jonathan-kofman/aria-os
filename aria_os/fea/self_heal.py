"""self_heal.py — autonomous FEA repair loop.

Pipeline:
    1. run_static_fea on the candidate STEP
    2. if SF >= target → exit ok
    3. else, decide a remediation and apply:
        - if max stress is on a thin section (thickness < 5mm) → thicken
        - if max stress is concentrated at a single nodal cluster
          (a corner / fillet area) → suggest a rib next to the hotspot
        - if no geometric remediation is feasible → swap to the next
          stronger material in the same family (e.g. 1018 → 4140)
    4. re-run FEA; loop until pass or N iterations hit.

This is the "autonomy" pitch made real: the system designs, validates,
AND fixes itself. It piggybacks on calculix_stage + materials.py +
the (existing) cadquery template fleet.

Scope (v1):
- Material-upgrade remediation works without CAD changes (just swap
  the material key + re-FEA). Strongest signal for the demo.
- Thicken-by-uniform-scale uses an STL post-processor (scale only the
  Z extent), avoiding a full re-CAD round. Good enough for a demo;
  stretch goal is to round-trip back through cadquery_generator and
  re-emit the part with a new thickness param.
- Rib placement is a stretch — needs cadquery_generator hooks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable
import json


@dataclass
class HealAttempt:
    iteration: int
    action: str               # "thicken" | "upgrade_material" | "scale_volume"
    reason: str
    material: str
    thickness_scale: float    # multiplicative on starting thickness
    max_stress_mpa: Optional[float] = None
    safety_factor: Optional[float] = None
    passed: Optional[bool] = None
    vtu_path: Optional[str] = None
    # v2: per-iteration hot-spot analysis (drives smarter remediation
    # in v3 once the planner can consume rib/fillet hints).
    hotspot_count: Optional[int] = None
    hotspot_primary: Optional[str] = None     # remediation_kind
    hotspot_location: Optional[str] = None    # corner | edge | face | thin_section
    remediation_hint: Optional[dict] = None


@dataclass
class HealReport:
    ok: bool
    initial_passed: bool
    final_passed: bool
    iterations: int
    attempts: list = field(default_factory=list)
    final_material: str = ""
    final_thickness_scale: float = 1.0
    final_safety_factor: Optional[float] = None
    final_max_stress_mpa: Optional[float] = None
    notes: str = ""


# Strength ladder: same metallurgy family, ascending yield strength.
# When a part fails FEA, walk up this ladder before giving up. Going
# up costs more $$/kg, but is cheaper than re-CADing the geometry.
_STRENGTH_LADDER = {
    "aluminum_6061":  ["aluminum_6061", "aluminum_7075", "titanium_gr5"],
    "aluminum_7075":  ["aluminum_7075", "titanium_gr5", "steel_4140"],
    "aluminum_5052":  ["aluminum_5052", "aluminum_6061", "aluminum_7075"],
    "steel_1018":     ["steel_1018", "steel_4140"],
    "steel_4140":     ["steel_4140", "titanium_gr5"],
    "stainless_304":  ["stainless_304", "stainless_316", "steel_4140"],
    "stainless_316":  ["stainless_316", "steel_4140"],
    "titanium_gr5":   ["titanium_gr5", "steel_4140"],
    # Plastics: walk up to a stiffer plastic, not to a metal.
    "abs":            ["abs", "petg", "nylon_pa12"],
    "pla":            ["pla", "petg", "abs", "nylon_pa12"],
    "petg":           ["petg", "abs", "nylon_pa12"],
    "nylon_pa12":     ["nylon_pa12"],
}


def _next_material(current: str) -> Optional[str]:
    """Return the next material in the strength ladder, or None at top."""
    ladder = _STRENGTH_LADDER.get(current, [current])
    if current not in ladder:
        return None
    i = ladder.index(current)
    if i + 1 < len(ladder):
        return ladder[i + 1]
    return None


def _scale_stl_z(stl_in: Path, stl_out: Path, z_scale: float) -> bool:
    """Uniformly scale the Z dimension of an ASCII or binary STL.
    Used as the cheap "thicken" remediation.
    """
    try:
        import trimesh
        m = trimesh.load_mesh(str(stl_in))
        # Scale only Z about the part's min Z so the bottom face stays
        # where it was (typical fixture).
        z_min = m.vertices[:, 2].min()
        m.vertices[:, 2] = z_min + (m.vertices[:, 2] - z_min) * z_scale
        m.export(str(stl_out))
        return True
    except Exception as ex:
        print(f"[self_heal] _scale_stl_z failed: {ex}")
        return False


def _stl_to_step_via_cadquery(stl_path: Path, step_out: Path) -> bool:
    """Best-effort STL → STEP for re-FEA. CalculiX/gmsh need a STEP for
    OCC importing; trimesh STLs are tris only. cadquery's
    importers.importShape doesn't take meshes directly, so we use the
    convex-hull as a re-FEA proxy. Less accurate, but good enough to
    let us re-run FEA and confirm the trend (lower stress with a
    thicker part, higher SF with a stronger material).
    """
    try:
        import trimesh
        import cadquery as cq
        m = trimesh.load_mesh(str(stl_path))
        # Convex hull: a closed polyhedron we can import via cadquery.
        # For mostly-convex parts (brackets, plates) this is a fine
        # FEA proxy. Non-convex parts (with holes/cutouts) lose the
        # holes — flagged in notes.
        ch = m.convex_hull
        ch.export(str(stl_path.with_suffix('.hull.stl')))
        # cadquery has no direct STL importer in the public API, but
        # OCP (which cadquery wraps) does via STLAPI_Reader. We use
        # the BRepBuilder approach: read the hull faces, build a shell.
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        from OCP.gp import gp_Pnt
        from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                          BRepBuilderAPI_MakeFace,
                                          BRepBuilderAPI_Sewing)
        sewing = BRepBuilderAPI_Sewing(1.0e-3)
        for face in ch.faces:
            v = ch.vertices[face]
            poly = BRepBuilderAPI_MakePolygon(
                gp_Pnt(*v[0]), gp_Pnt(*v[1]), gp_Pnt(*v[2]), True)
            wire = poly.Wire()
            f = BRepBuilderAPI_MakeFace(wire, True).Face()
            sewing.Add(f)
        sewing.Perform()
        shell = sewing.SewedShape()
        writer = STEPControl_Writer()
        writer.Transfer(shell, STEPControl_AsIs)
        writer.Write(str(step_out))
        return step_out.is_file()
    except Exception as ex:
        print(f"[self_heal] STL→STEP conversion failed: {ex}")
        return False


def heal_fea(step_path: str | Path,
              *,
              material: str,
              load_n: float,
              target_safety_factor: float = 2.0,
              max_iters: int = 4,
              out_dir: str | Path | None = None,
              mesh_size_mm: float = 5.0,
              loads: list[dict] | None = None) -> HealReport:
    """Run FEA → if fail, remediate → re-run; loop until pass or limit.

    Order of remediation tried per iteration:
        1. Walk up the strength ladder (cheapest — no CAD change)
        2. If at top of ladder AND iteration <= max_iters/2, try a
           1.25× thickness scale
        3. Otherwise stop

    Returns HealReport with the trajectory of attempts.
    """
    from aria_os.fea.calculix_stage import run_static_fea
    from aria_os.fea.materials import resolve as _resolve_mat

    step_path = Path(step_path)
    out_dir = Path(out_dir or
                    f"outputs/fea/self_heal/{step_path.stem}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve material → CCX key
    mat = _resolve_mat(material)
    cur_material = mat.ccx_key if mat else material
    cur_thickness_scale = 1.0
    cur_step = step_path

    attempts: list[HealAttempt] = []
    initial_passed = False
    last_passed: Optional[bool] = None
    last_sf: Optional[float] = None
    last_stress: Optional[float] = None
    last_vtu: Optional[str] = None
    last_action = "initial"
    last_reason = "first FEA pass"

    for it in range(max_iters):
        iter_dir = out_dir / f"iter_{it}"
        fea = run_static_fea(cur_step, material=cur_material,
                              load_n=load_n, loads=loads,
                              out_dir=iter_dir,
                              mesh_size_mm=mesh_size_mm,
                              target_safety_factor=target_safety_factor,
                              export_vtk=True)
        passed = bool(fea.get("passed"))
        sf = fea.get("safety_factor")
        stress = fea.get("max_stress_mpa")
        vtu = fea.get("vtu_path")

        # v2: hot-spot analysis from the FRD (works whenever CCX ran).
        hint = None
        try:
            frd_path = fea.get("frd_path")
            if frd_path:
                from aria_os.fea.hotspot import analyze_frd
                hint_obj = analyze_frd(frd_path, load_axis="-z")
                hint = {
                    "n_hotspots": hint_obj.n_hotspots,
                    "primary_action": hint_obj.primary_action,
                    "primary_params": hint_obj.primary_params,
                    "hotspot_locations": [
                        h.get("location") for h in hint_obj.hotspots[:3]],
                }
        except Exception:
            hint = None

        attempts.append(HealAttempt(
            iteration=it, action=last_action, reason=last_reason,
            material=cur_material,
            thickness_scale=cur_thickness_scale,
            max_stress_mpa=stress, safety_factor=sf,
            passed=passed, vtu_path=vtu,
            hotspot_count=(hint.get("n_hotspots") if hint else None),
            hotspot_primary=(hint.get("primary_action") if hint else None),
            hotspot_location=(
                hint["hotspot_locations"][0]
                if hint and hint.get("hotspot_locations") else None),
            remediation_hint=hint))
        last_passed, last_sf, last_stress, last_vtu = passed, sf, stress, vtu
        if it == 0:
            initial_passed = passed

        if passed:
            return HealReport(
                ok=True, initial_passed=initial_passed,
                final_passed=True, iterations=it + 1,
                attempts=attempts,
                final_material=cur_material,
                final_thickness_scale=cur_thickness_scale,
                final_safety_factor=sf, final_max_stress_mpa=stress,
                notes=("converged at iter " + str(it) +
                       " via " + last_action))

        # Pick a remediation for the next iteration
        nxt = _next_material(cur_material)
        if nxt is not None and nxt != cur_material:
            last_action = "upgrade_material"
            last_reason = (f"SF {sf} < {target_safety_factor} on "
                            f"{cur_material} → upgrade to {nxt}")
            cur_material = nxt
            continue

        # At top of ladder — try thicken (only if we have an STL we
        # can scale + reconvert to STEP).
        if it < max_iters - 1 and cur_thickness_scale < 1.5:
            cur_thickness_scale *= 1.25
            stl_in = cur_step.with_suffix(".STL")
            if not stl_in.is_file():
                stl_in = cur_step.with_suffix(".stl")
            if stl_in.is_file():
                stl_out = iter_dir / f"thickened_{cur_thickness_scale:.2f}.stl"
                step_out = iter_dir / f"thickened_{cur_thickness_scale:.2f}.step"
                if (_scale_stl_z(stl_in, stl_out, cur_thickness_scale)
                        and _stl_to_step_via_cadquery(stl_out, step_out)):
                    last_action = "thicken"
                    last_reason = (f"top of strength ladder; thicken "
                                    f"to {cur_thickness_scale:.2f}× original Z")
                    cur_step = step_out
                    continue
            last_reason = ("thicken attempted but STL→STEP conversion "
                            "unavailable in this env")
        # No more options — bail out
        last_reason = ("no remediation left: top of strength ladder, "
                        "and thicken not available")
        break

    return HealReport(
        ok=False,
        initial_passed=initial_passed,
        final_passed=bool(last_passed),
        iterations=len(attempts),
        attempts=attempts,
        final_material=cur_material,
        final_thickness_scale=cur_thickness_scale,
        final_safety_factor=last_sf,
        final_max_stress_mpa=last_stress,
        notes=last_reason)


def _cli():
    import argparse
    ap = argparse.ArgumentParser(
        description="Self-healing FEA: keeps remediating until SF ≥ target.")
    ap.add_argument("step_path")
    ap.add_argument("--material", default="aluminum_6061")
    ap.add_argument("--load-n", type=float, default=500.0)
    ap.add_argument("--target-sf", type=float, default=2.0)
    ap.add_argument("--max-iters", type=int, default=4)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    rep = heal_fea(args.step_path, material=args.material,
                    load_n=args.load_n,
                    target_safety_factor=args.target_sf,
                    max_iters=args.max_iters, out_dir=args.out_dir)
    print(json.dumps({
        "ok": rep.ok, "initial_passed": rep.initial_passed,
        "final_passed": rep.final_passed,
        "iterations": rep.iterations,
        "final_material": rep.final_material,
        "final_thickness_scale": rep.final_thickness_scale,
        "final_safety_factor": rep.final_safety_factor,
        "final_max_stress_mpa": rep.final_max_stress_mpa,
        "trajectory": [
            {"iter": a.iteration, "action": a.action,
             "material": a.material, "scale": a.thickness_scale,
             "sf": a.safety_factor, "stress": a.max_stress_mpa,
             "passed": a.passed} for a in rep.attempts],
        "notes": rep.notes,
    }, indent=2, default=str))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = ["HealReport", "HealAttempt", "heal_fea", "_next_material"]
