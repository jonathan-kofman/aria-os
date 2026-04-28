r"""auto_fea.py - one-shot end-to-end FEA on a CAD artifact.

Pipeline:
    1. Detect BCs from STL geometry (bc_detector.py)
    2. Try SW Simulation via the addin (POST /op runFea)
    3. Fall back to CalculiX (calculix_stage.run_static_fea) if SW unavailable
    4. Fall back to closed-form (verification.fea_gate) if CalculiX missing
    5. Always emit a unified report JSON + auto-detected BC summary

Usage::

    python -m aria_os.fea.auto_fea outputs/feature_matrix/cswe_compound_lever.STL \
        --material aluminum_6061 --load 500 --target-sf 2.0
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path


def _try_sw_simulation(step_path: Path, material: str, load_n: float,
                        out_dir: Path, bc_summary: dict) -> dict | None:
    """Hit POST localhost:7501/op runFea. Return the iteration result
    on success, None if SW addin not reachable.

    BC threading (closes audit Gap #2): we pass ALL detected restraints
    + loads + symmetry planes — not just the first. The SW addin then
    iterates fixtures so the analysis matches the auto-detected BCs.
    """
    restraints = bc_summary.get("restraints", []) or []
    loads = bc_summary.get("loads", []) or []
    syms = bc_summary.get("symmetries", []) or []
    payload = {
        "kind": "runFea",
        "params": {
            "iterations": [{
                "name": step_path.stem,
                "material": material,
                "load_n": load_n,
                # Backwards-compat single-face fields (older addin builds
                # only read these); newer builds read fixture_faces / load_faces.
                "fixture_face": (restraints[0]["face_id"]
                                 if restraints else None),
                "load_face": (loads[0]["face_id"] if loads else None),
                "fixture_faces": [r.get("face_id") for r in restraints
                                  if r.get("face_id") is not None],
                "load_faces": [l.get("face_id") for l in loads
                               if l.get("face_id") is not None],
                "symmetry_planes": [s.get("plane") for s in syms
                                    if s.get("plane")],
            }],
            "target_max_stress_mpa": 200.0,
            "export_dir": str(out_dir),
        },
    }
    try:
        req = urllib.request.Request(
            "http://localhost:7501/op",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp
    except urllib.error.URLError:
        return None
    except Exception as ex:
        return {"ok": False, "error": f"SW request threw: {ex}"}


def _try_calculix(step_path: Path, material: str, load_n: float,
                   out_dir: Path, target_sf: float,
                   loads: list[dict] | None = None) -> dict | None:
    """Run CalculiX static FEA via aria_os.fea.calculix_stage.

    If `loads` is given, runs the combined axial+bending+torsion case;
    otherwise falls back to the simple `load_n` -Z point load.
    """
    try:
        from aria_os.fea.calculix_stage import run_static_fea
    except Exception:
        return None
    try:
        return run_static_fea(step_path, material=material,
                               load_n=load_n, loads=loads,
                               out_dir=out_dir,
                               target_safety_factor=target_sf)
    except Exception as ex:
        return {"ok": False, "error": f"calculix threw: {ex}"}


def _try_closed_form(spec: dict, stl_path: Path, load_n: float) -> dict:
    """Fall back to the analytical cantilever check in fea_gate.py.

    The fea_gate `Issue` dataclass uses `severity` / `code` / `message`
    in this codebase (verification/dfm.py:Issue). Older copies of this
    module used `level` — guard with getattr() so we don't throw if the
    Issue shape evolves.
    """
    try:
        from aria_os.verification.fea_gate import run_fea
        issues = run_fea(spec, str(stl_path), {"point_n": load_n})
        return {"ok": True, "engine": "closed_form",
                "issues": [{
                    "severity": getattr(i, "severity",
                                          getattr(i, "level", "info")),
                    "code": getattr(i, "code", ""),
                    "message": getattr(i, "message", ""),
                } for i in issues],
                # Surface SF/stress if the underlying check parsed them
                # out of the message (closed-form prints "SF=X" / "MPa").
                "passed": all(
                    getattr(i, "severity",
                            getattr(i, "level", "info")) != "critical"
                    for i in issues),
        }
    except Exception as ex:
        return {"ok": False, "engine": "closed_form",
                "error": f"closed_form threw: {ex}"}


def auto_fea(stl_path: str | Path, *, material: str = "aluminum_6061",
              load_n: float = 500.0, target_sf: float = 2.0,
              loads: list[dict] | None = None,
              out_dir: str | Path | None = None,
              gravity_axis: str = "-Z") -> dict:
    """End-to-end FEA. Returns unified report dict.

    Order of preference: SW Simulation → CalculiX → closed-form.

    If `loads=[...]` is supplied, the CalculiX path applies combined
    axial+bending+torsion via `_build_combined_loads`. SW Simulation
    still gets the simple `load_n` (combined-case API on SW Sim is
    deferred — see FEA_PIPELINE_AUDIT.md "Gap 1, SW path remaining").
    """
    from aria_os.fea.bc_detector import detect_bcs
    from aria_os.fea.materials import resolve as _resolve_material

    stl_path = Path(stl_path)
    out_dir = Path(out_dir or f"outputs/fea/{stl_path.stem}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve material across all 3 tiers (closes Gap #3). If unknown,
    # we still pass the raw string downstream — each tier has its own
    # legacy fallback. The resolution dict is added to the report so
    # users can see what happened.
    mat_res = _resolve_material(material)
    sw_material = mat_res.sw_name if mat_res else material
    ccx_material = mat_res.ccx_key if mat_res else material
    cf_material = mat_res.cf_key if mat_res else material

    # 1. BC detection
    bcs = detect_bcs(stl_path, gravity_axis=gravity_axis)

    # 2. Try SW Simulation
    sw_result = None
    step_path = stl_path.with_suffix(".step")
    if step_path.is_file():
        sw_result = _try_sw_simulation(step_path, sw_material, load_n,
                                         out_dir, bcs)

    # 3. Try CalculiX if SW didn't work
    ccx_result = None
    if step_path.is_file() and (sw_result is None or
                                  not sw_result.get("ok")):
        ccx_result = _try_calculix(step_path, ccx_material, load_n,
                                     out_dir, target_sf, loads=loads)

    # 4. Always run closed-form as a sanity check.
    #     spec uses cf_material (resolved alias for closed-form's table).
    spec = {
        "material": cf_material,
        "thickness_mm": float(bcs["stats"]["bbox_mm"][2])
                          if bcs.get("stats") else 5.0,
        "width_mm": float(bcs["stats"]["bbox_mm"][1])
                      if bcs.get("stats") else 50.0,
        "span_mm": float(bcs["stats"]["bbox_mm"][0])
                    if bcs.get("stats") else 100.0,
        "length_mm": float(bcs["stats"]["bbox_mm"][0])
                       if bcs.get("stats") else 100.0,
    }
    cf_result = _try_closed_form(spec, stl_path, load_n)

    # 5. Pick the best result for the headline
    if sw_result and sw_result.get("ok"):
        engine = "sw-simulation"
        primary = sw_result
    elif ccx_result and ccx_result.get("available") and ccx_result.get("passed") is not None:
        engine = "calculix"
        primary = ccx_result
    else:
        engine = "closed_form"
        primary = cf_result

    report = {
        "stl_path": str(stl_path),
        "step_path": str(step_path) if step_path.is_file() else None,
        "material": material,
        "material_resolved": ({
            "canonical": mat_res.canonical,
            "sw_name": mat_res.sw_name,
            "ccx_key": mat_res.ccx_key,
            "cf_key": mat_res.cf_key,
            "yield_mpa": mat_res.yield_mpa,
            "e_gpa": mat_res.e_gpa,
            "density_kg_m3": mat_res.density_kg_m3,
        } if mat_res else None),
        "load_n": load_n,
        "target_safety_factor": target_sf,
        "boundary_conditions": bcs,
        "sw_simulation": sw_result,
        "calculix": ccx_result,
        "closed_form": cf_result,
        "engine": engine,
        "primary": primary,
    }
    report_path = out_dir / "auto_fea_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str),
                            encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# CLI:  python -m aria_os.fea.auto_fea <stl> [--material] [--load] [...]
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl_path")
    ap.add_argument("--material", default="aluminum_6061")
    ap.add_argument("--load", type=float, default=500.0,
                    help="load magnitude in N")
    ap.add_argument("--target-sf", type=float, default=2.0,
                    help="target safety factor")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--gravity", default="-Z",
                    help="gravity axis ('-Z', '+X', etc.)")
    ap.add_argument("--bending-nmm", type=float, default=0.0,
                    help="bending moment about Y axis (Nmm)")
    ap.add_argument("--torsion-nmm", type=float, default=0.0,
                    help="torsion moment about Z axis (Nmm)")
    args = ap.parse_args()

    # Build combined loads list if any moment supplied; force keeps
    # the simple --load contract. CalculiX path consumes them.
    loads_list: list[dict] | None = None
    if args.bending_nmm or args.torsion_nmm:
        loads_list = [
            {"type": "force", "axis": "z", "magnitude_n": -args.load},
        ]
        if args.bending_nmm:
            loads_list.append({"type": "moment", "axis": "y",
                                "magnitude_nmm": args.bending_nmm})
        if args.torsion_nmm:
            loads_list.append({"type": "moment", "axis": "z",
                                "magnitude_nmm": args.torsion_nmm})

    rep = auto_fea(args.stl_path, material=args.material,
                    load_n=args.load, target_sf=args.target_sf,
                    loads=loads_list,
                    out_dir=args.out_dir, gravity_axis=args.gravity)
    print(json.dumps({
        "engine": rep["engine"],
        "primary_passed": (rep["primary"].get("passed")
                            if isinstance(rep["primary"], dict)
                            else None),
        "max_stress_mpa": (rep["primary"].get("max_stress_mpa")
                            if isinstance(rep["primary"], dict)
                            else None),
        "safety_factor": (rep["primary"].get("safety_factor")
                           if isinstance(rep["primary"], dict)
                           else None),
        "boundary_conditions": {
            "restraints": len(rep["boundary_conditions"]
                                .get("restraints", [])),
            "loads": len(rep["boundary_conditions"].get("loads", [])),
            "symmetries": len(rep["boundary_conditions"]
                                .get("symmetries", [])),
        },
        "report_path": str(Path(args.out_dir or
                                  f"outputs/fea/{Path(args.stl_path).stem}")
                            / "auto_fea_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
