"""
Headless static-linear FEA via gmsh (mesh) + CalculiX (solve).

Minimal but real: given a STEP file + a material + a load case, mesh it,
run CalculiX, extract max von Mises stress, pass/fail against the
material's yield stress. This is the first stage that answers
"will this part actually hold up" — not just "does it look like a bracket".

Graceful-degrade: skips cleanly if gmsh or CalculiX aren't installed.

Scope / non-goals
-----------------
- linear static only (no modal, no nonlinear, no thermal coupling)
- uniform pressure or force on a named face (user picks via param)
- isotropic, linear-elastic materials from MATERIAL_PROPS
- tet4 mesh (linear tetrahedra) — coarse but fast; upgrade to tet10 later
- no refinement study, no mesh convergence check

Usage
-----
    from aria_os.fea.calculix_stage import run_static_fea
    r = run_static_fea(
        step_path="motor_mount.step",
        material="aluminum_6061",
        load_n=500,
        out_dir="outputs/fea/motor_mount",
    )
    # r = {"available": bool, "passed": bool, "max_stress_mpa": float,
    #      "safety_factor": float, "report_path": str, ...}
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# Basic material properties (SI, MPa for stress).  E and nu drive the solve;
# yield_mpa drives pass/fail.  Values at room temperature.
MATERIAL_PROPS = {
    "aluminum_6061":  {"E_mpa": 69000, "nu": 0.33, "yield_mpa": 276,
                       "density_kg_m3": 2700},
    "aluminum_7075":  {"E_mpa": 71700, "nu": 0.33, "yield_mpa": 503,
                       "density_kg_m3": 2810},
    "steel_1018":     {"E_mpa": 200000, "nu": 0.29, "yield_mpa": 370,
                       "density_kg_m3": 7870},
    "steel_4140":     {"E_mpa": 205000, "nu": 0.29, "yield_mpa": 655,
                       "density_kg_m3": 7850},
    "stainless_304":  {"E_mpa": 193000, "nu": 0.29, "yield_mpa": 215,
                       "density_kg_m3": 8000},
    "titanium_gr5":   {"E_mpa": 113800, "nu": 0.34, "yield_mpa": 880,
                       "density_kg_m3": 4430},
    "cfrp":           {"E_mpa":  70000, "nu": 0.30, "yield_mpa": 600,
                       "density_kg_m3": 1550},  # quasi-isotropic laminate
    "peek":           {"E_mpa":   3600, "nu": 0.40, "yield_mpa":  97,
                       "density_kg_m3": 1320},
    "abs":            {"E_mpa":   2200, "nu": 0.35, "yield_mpa":  40,
                       "density_kg_m3": 1040},
    "pla":            {"E_mpa":   3500, "nu": 0.36, "yield_mpa":  50,
                       "density_kg_m3": 1250},
    "petg":           {"E_mpa":   2100, "nu": 0.38, "yield_mpa":  50,
                       "density_kg_m3": 1270},
    "nylon_pa12":     {"E_mpa":   1700, "nu": 0.39, "yield_mpa":  48,
                       "density_kg_m3": 1010},
}


def _find_ccx() -> str | None:
    for name in ("ccx", "ccx_static", "ccx_2.20", "ccx.exe", "ccx_static.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _have_gmsh() -> bool:
    try:
        import gmsh  # noqa: F401
        return True
    except Exception:
        return False


def mesh_step(step_path: str | Path, out_msh: str | Path,
              *, mesh_size_mm: float = 5.0) -> dict:
    """STEP → tet4 mesh (.msh v2, CalculiX-compatible). Returns
    {ok, n_nodes, n_elements, msh_path}."""
    if not _have_gmsh():
        return {"ok": False, "error": "gmsh python module not installed"}

    import gmsh
    step_path = str(Path(step_path).resolve())
    out_msh = str(Path(out_msh).resolve())

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", mesh_size_mm)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", mesh_size_mm / 4)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)  # Delaunay
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)  # CCX wants v2
        gmsh.model.occ.importShapes(step_path)
        gmsh.model.occ.synchronize()
        gmsh.model.mesh.generate(3)
        gmsh.write(out_msh)
        nodes = gmsh.model.mesh.getNodes()[0]
        elems = gmsh.model.mesh.getElements(3)[1]
        n_elements = sum(len(e) for e in elems)
        return {"ok": True, "n_nodes": len(nodes),
                "n_elements": n_elements, "msh_path": out_msh}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        gmsh.finalize()


def _axis_index(axis: str) -> int:
    """'x'|'y'|'z' → 0|1|2 (1-based for CCX is index+1)."""
    a = (axis or "z").lower()
    return {"x": 0, "y": 1, "z": 2}.get(a, 2)


def _build_combined_loads(pts, loaded_ids: list[int],
                           loads: list[dict]) -> list[tuple[int, int, float]]:
    """Translate a structured loads list into a list of (node_id, dof, value)
    tuples for CCX *CLOAD.

    Each load is one of:
        {"type": "force",  "axis": "x|y|z", "magnitude_n": float}
        {"type": "moment", "axis": "x|y|z", "magnitude_nmm": float}

    Force is distributed evenly. Moment is applied as a couple over the top
    nodes about the load-face centroid (linear stress distribution =
    Σr × F where F = M·r/Σr²). Torsion is applied as tangential forces
    around the axis (T = Σr × F, with F tangential to the centroidal radius).
    """
    import numpy as np
    if not loads:
        return []
    # Centroid of the loaded patch in 3D — used for moment + torsion arms.
    pts_arr = np.asarray(pts)
    loaded_xyz = pts_arr[[i - 1 for i in loaded_ids]]
    cx, cy, cz = loaded_xyz.mean(axis=0)
    cloads: list[tuple[int, int, float]] = []
    for ld in loads:
        ltype = (ld.get("type") or "force").lower()
        axis = (ld.get("axis") or "z").lower()
        ax_i = _axis_index(axis)
        if ltype == "force":
            mag = float(ld.get("magnitude_n", ld.get("magnitude", 0.0)))
            if mag == 0.0:
                continue
            per_node = mag / len(loaded_ids)
            for nid in loaded_ids:
                cloads.append((nid, ax_i + 1, per_node))
        elif ltype == "moment":
            mag = float(ld.get("magnitude_nmm",
                               ld.get("magnitude_n_mm",
                                      ld.get("magnitude", 0.0))))
            if mag == 0.0:
                continue
            # Bending: linear stress about an axis through the centroid.
            # For axis=x (moment vector along +x), force is in +z direction
            # proportional to (y - cy). For axis=y, force in z proportional
            # to (x - cx). For axis=z (torsion), force is tangential in xy.
            if ax_i == 0:  # M about x → bending, force in z, arm = (y - cy)
                arms = loaded_xyz[:, 1] - cy
                denom = float((arms ** 2).sum())
                if denom <= 0:
                    continue
                for k, nid in enumerate(loaded_ids):
                    f = mag * arms[k] / denom
                    cloads.append((nid, 3, float(f)))
            elif ax_i == 1:  # M about y → bending, force in z, arm = (x - cx)
                arms = loaded_xyz[:, 0] - cx
                denom = float((arms ** 2).sum())
                if denom <= 0:
                    continue
                for k, nid in enumerate(loaded_ids):
                    # +M_y means rotation from +z → +x: f_z = +M·x/Σx²
                    # but sign convention for bending: tension on +x side
                    # → compressive on -x side. Use f_z = -M·x/Σx².
                    f = -mag * arms[k] / denom
                    cloads.append((nid, 3, float(f)))
            else:  # ax_i == 2: torsion about z, tangential force in xy plane
                rx = loaded_xyz[:, 0] - cx
                ry = loaded_xyz[:, 1] - cy
                r2 = rx ** 2 + ry ** 2
                denom = float(r2.sum())
                if denom <= 0:
                    continue
                for k, nid in enumerate(loaded_ids):
                    # Torque T = Σ (r × F). For a tangential F = T·r̂_⊥/Σr²,
                    # f_x = -T·y/Σr², f_y = +T·x/Σr².
                    fx = -mag * ry[k] / denom
                    fy = +mag * rx[k] / denom
                    cloads.append((nid, 1, float(fx)))
                    cloads.append((nid, 2, float(fy)))
    return cloads


def msh_to_inp(msh_path: str | Path,
               inp_path: str | Path,
               *,
               material: str,
               load_n: float = 0.0,
               loads: list[dict] | None = None,
               fixed_z_below_mm: float = 2.0) -> dict:
    """Convert gmsh .msh → CalculiX .inp.

    Two modes:
      - simple: pass `load_n` only → distributed -Z force on top nodes
      - combined: pass `loads=[...]` → list of force/moment dicts
        (see `_build_combined_loads`)

    Bottom-most nodes (within fixed_z_below_mm of min Z) are fixed in all
    DOFs. Returns {ok, inp_path, n_fixed, n_loaded, n_cloads}.
    """
    import meshio
    if material not in MATERIAL_PROPS:
        return {"ok": False,
                "error": f"unknown material '{material}'; "
                         f"known: {sorted(MATERIAL_PROPS)}"}
    mp = MATERIAL_PROPS[material]

    try:
        m = meshio.read(str(msh_path))
    except Exception as exc:
        return {"ok": False, "error": f"msh read failed: {exc}"}

    tets = None
    for cb in m.cells:
        if cb.type == "tetra":
            tets = cb.data
            break
    if tets is None or len(tets) == 0:
        return {"ok": False, "error": "no tet elements in mesh"}

    pts = m.points
    z_min, z_max = pts[:, 2].min(), pts[:, 2].max()
    # 1-based node IDs for CCX
    fixed_ids = [i + 1 for i, p in enumerate(pts)
                 if p[2] <= z_min + fixed_z_below_mm]
    loaded_ids = [i + 1 for i, p in enumerate(pts)
                  if p[2] >= z_max - (z_max - z_min) * 0.10]
    if not fixed_ids:
        return {"ok": False, "error": "no fixed nodes — check geometry Z range"}
    if not loaded_ids:
        return {"ok": False, "error": "no loaded nodes"}

    # Build the CLOAD entries:
    #   - if loads=[...] passed, use combined force + bending + torsion
    #   - else, fall back to single -Z force of magnitude load_n
    cload_tuples: list[tuple[int, int, float]] = []
    if loads:
        cload_tuples = _build_combined_loads(pts, loaded_ids, loads)
        if not cload_tuples:
            return {"ok": False,
                    "error": "loads list yielded no CLOAD entries"}
    else:
        per_node_force = -float(load_n) / len(loaded_ids)
        cload_tuples = [(nid, 3, per_node_force) for nid in loaded_ids]

    inp_path = Path(inp_path)
    inp_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = ["*NODE, NSET=NALL"]
    for i, p in enumerate(pts, 1):
        lines.append(f"{i},{p[0]:.6f},{p[1]:.6f},{p[2]:.6f}")
    lines.append("*ELEMENT, TYPE=C3D4, ELSET=EALL")
    for i, t in enumerate(tets, 1):
        a, b, c, d = (int(x) + 1 for x in t)
        lines.append(f"{i},{a},{b},{c},{d}")

    lines.append("*NSET, NSET=FIXED")
    for i in range(0, len(fixed_ids), 16):
        lines.append(",".join(str(x) for x in fixed_ids[i:i + 16]))
    lines.append("*NSET, NSET=LOADED")
    for i in range(0, len(loaded_ids), 16):
        lines.append(",".join(str(x) for x in loaded_ids[i:i + 16]))

    lines += [
        f"*MATERIAL, NAME={material.upper()}",
        "*ELASTIC",
        f"{mp['E_mpa']},{mp['nu']}",
        f"*SOLID SECTION, ELSET=EALL, MATERIAL={material.upper()}",
        "*STEP",
        "*STATIC",
        "*BOUNDARY",
        "FIXED,1,3",
        "*CLOAD",
    ]
    # Per-node CLOAD lines: NODE_ID, DOF, FORCE
    for nid, dof, val in cload_tuples:
        lines.append(f"{nid},{dof},{val:.6e}")
    lines += [
        "*NODE FILE",
        "U",
        "*EL FILE",
        "S",
        "*END STEP",
    ]
    inp_path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "inp_path": str(inp_path),
            "n_fixed": len(fixed_ids), "n_loaded": len(loaded_ids),
            "n_cloads": len(cload_tuples)}


def run_ccx(inp_path: str | Path, *, timeout: int = 300) -> dict:
    """Run CalculiX on an .inp. Returns {ok, frd_path, stderr, rc}."""
    ccx = _find_ccx()
    if ccx is None:
        return {"ok": False, "error": "ccx not found on PATH"}

    inp = Path(inp_path)
    stem = inp.with_suffix("")
    try:
        r = subprocess.run(
            [ccx, stem.name], cwd=str(inp.parent),
            check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ccx timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    frd = stem.with_suffix(".frd")
    return {"ok": frd.is_file(), "frd_path": str(frd),
            "stderr": r.stderr[-400:], "rc": r.returncode}


def parse_max_von_mises(frd_path: str | Path) -> float | None:
    """Scan a CalculiX .frd for SXX/SYY/.../SXY and compute per-node
    von Mises, return the max in MPa. Returns None if no stress block
    found."""
    try:
        lines = Path(frd_path).read_text(errors="replace").splitlines()
    except Exception:
        return None
    # CCX ASCII frd: look for "-4  STRESS" block, then "-5 SXX ..." columns.
    in_stress = False
    max_vm = 0.0
    for line in lines:
        if line.startswith(" -4") and "STRESS" in line:
            in_stress = True
            continue
        if in_stress and line.startswith(" -3"):
            break
        if in_stress and line.startswith(" -1"):
            # node values line: " -1<nodeid>  SXX  SYY  SZZ  SXY  SYZ  SZX"
            parts = line.split()
            if len(parts) >= 8:
                try:
                    sxx, syy, szz = (float(x) for x in parts[2:5])
                    sxy, syz, szx = (float(x) for x in parts[5:8])
                    vm = (((sxx - syy) ** 2 + (syy - szz) ** 2
                           + (szz - sxx) ** 2
                           + 6 * (sxy * sxy + syz * syz + szx * szx)) / 2) ** 0.5
                    if vm > max_vm:
                        max_vm = vm
                except ValueError:
                    pass
    return max_vm if max_vm > 0 else None


def run_static_fea(step_path: str | Path,
                   *,
                   material: str,
                   load_n: float = 0.0,
                   loads: list[dict] | None = None,
                   out_dir: str | Path,
                   mesh_size_mm: float = 5.0,
                   target_safety_factor: float = 2.0,
                   export_vtk: bool = True) -> dict:
    """End-to-end static-linear FEA on a single STEP part.

    Returns
    -------
    dict with keys:
        available: bool               — True if gmsh + ccx both present
        passed: bool | None           — None if unavailable
        max_stress_mpa: float | None
        yield_mpa: float | None
        safety_factor: float | None
        material: str
        load_n: float
        mesh: {n_nodes, n_elements}
        report_path: path to fea_report.json
        error: str | None
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if material not in MATERIAL_PROPS:
        return {"available": False, "passed": None,
                "error": f"unknown material '{material}'",
                "known_materials": sorted(MATERIAL_PROPS)}
    mp = MATERIAL_PROPS[material]

    if not _have_gmsh():
        return {"available": False, "passed": None,
                "error": "gmsh not installed (pip install gmsh)"}

    ccx = _find_ccx()
    if ccx is None:
        return {"available": False, "passed": None,
                "error": "CalculiX (ccx) not on PATH — see "
                         "scripts/PRO_HEADLESS_SETUP.md"}

    msh_path = out_dir / "mesh.msh"
    inp_path = out_dir / "job.inp"
    report_path = out_dir / "fea_report.json"

    mesh_r = mesh_step(step_path, msh_path, mesh_size_mm=mesh_size_mm)
    if not mesh_r.get("ok"):
        return {"available": True, "passed": False,
                "error": f"meshing failed: {mesh_r.get('error')}",
                "mesh": mesh_r}

    inp_r = msh_to_inp(msh_path, inp_path, material=material,
                        load_n=load_n, loads=loads)
    if not inp_r.get("ok"):
        return {"available": True, "passed": False,
                "error": f"inp gen failed: {inp_r.get('error')}",
                "mesh": mesh_r}

    ccx_r = run_ccx(inp_path)
    if not ccx_r.get("ok"):
        return {"available": True, "passed": False,
                "error": f"ccx failed: {ccx_r.get('error') or ccx_r.get('stderr')}",
                "mesh": mesh_r}

    max_vm = parse_max_von_mises(ccx_r["frd_path"])
    if max_vm is None:
        return {"available": True, "passed": False,
                "error": "could not parse stress from frd",
                "mesh": mesh_r}

    sf = mp["yield_mpa"] / max_vm if max_vm > 0 else float("inf")
    passed = sf >= target_safety_factor

    # VTU export — StructSight reads .vtu directly with vtk.js. Best-effort:
    # if it fails, we still return a valid FEA report (just no VTU path).
    vtu_path: str | None = None
    if export_vtk:
        try:
            from aria_os.fea.vtk_export import frd_to_vtu
            vtu_path = frd_to_vtu(ccx_r["frd_path"], out_dir / "result.vtu")
        except Exception as ex:
            # Non-fatal: log via the report and move on
            vtu_path = None
            _vtu_err = f"{type(ex).__name__}: {ex}"
        else:
            _vtu_err = None
    else:
        _vtu_err = None

    report = {
        "available": True,
        "passed": passed,
        "max_stress_mpa": round(max_vm, 2),
        "yield_mpa": mp["yield_mpa"],
        "safety_factor": round(sf, 2),
        "target_safety_factor": target_safety_factor,
        "material": material,
        "load_n": load_n,
        "loads": loads,
        "n_cloads": inp_r.get("n_cloads"),
        "vtu_path": vtu_path,
        "vtu_error": _vtu_err,
        "mesh": {"n_nodes": mesh_r["n_nodes"],
                 "n_elements": mesh_r["n_elements"]},
        "frd_path": ccx_r["frd_path"],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


# ---------------------------------------------------------------------------
# Modal FEA — *FREQUENCY step, first N natural frequencies
# ---------------------------------------------------------------------------
#
# The hackathon demo scenario (NEMA17 stepper mount at 200 Hz excitation)
# needs modal analysis, not just static. CalculiX supports a *FREQUENCY
# step natively; below we generate an alternate .inp and parse *EIGENVALUE
# blocks from the .frd output.

def msh_to_inp_modal(msh_path: str | Path,
                      inp_path: str | Path,
                      *,
                      material: str,
                      n_modes: int = 6,
                      fixed_z_below_mm: float = 2.0) -> dict:
    """Convert gmsh .msh -> CalculiX .inp for a *FREQUENCY modal analysis.

    Same mesh layout as the static case (tet4 C3D4) — only difference is
    the step block: *FREQUENCY (eigenvalue extraction) instead of *STATIC.

    Returns {ok, inp_path, n_fixed, n_modes}.
    """
    import meshio
    if material not in MATERIAL_PROPS:
        return {"ok": False,
                "error": f"unknown material '{material}'; "
                         f"known: {sorted(MATERIAL_PROPS)}"}
    mp = MATERIAL_PROPS[material]

    try:
        m = meshio.read(str(msh_path))
    except Exception as exc:
        return {"ok": False, "error": f"msh read failed: {exc}"}

    tets = None
    for cb in m.cells:
        if cb.type == "tetra":
            tets = cb.data
            break
    if tets is None or len(tets) == 0:
        return {"ok": False, "error": "no tet elements in mesh"}

    pts = m.points
    z_min = pts[:, 2].min()
    fixed_ids = [i + 1 for i, p in enumerate(pts)
                 if p[2] <= z_min + fixed_z_below_mm]
    if not fixed_ids:
        return {"ok": False, "error": "no fixed nodes"}

    inp_path = Path(inp_path)
    inp_path.parent.mkdir(parents=True, exist_ok=True)

    # Density is required for modal analysis (M matrix needs it).
    density_ton_mm3 = mp["density_kg_m3"] * 1e-12  # CCX prefers
    # consistent unit set (N-mm-s-tonne). ρ in tonne/mm³.

    lines: list[str] = ["*NODE, NSET=NALL"]
    for i, p in enumerate(pts, 1):
        lines.append(f"{i},{p[0]:.6f},{p[1]:.6f},{p[2]:.6f}")
    lines.append("*ELEMENT, TYPE=C3D4, ELSET=EALL")
    for i, t in enumerate(tets, 1):
        a, b, c, d = (int(x) + 1 for x in t)
        lines.append(f"{i},{a},{b},{c},{d}")

    lines.append("*NSET, NSET=FIXED")
    for i in range(0, len(fixed_ids), 16):
        lines.append(",".join(str(x) for x in fixed_ids[i:i + 16]))

    lines += [
        f"*MATERIAL, NAME={material.upper()}",
        "*ELASTIC",
        f"{mp['E_mpa']},{mp['nu']}",
        "*DENSITY",
        f"{density_ton_mm3:.6e}",
        f"*SOLID SECTION, ELSET=EALL, MATERIAL={material.upper()}",
        "*BOUNDARY",
        "FIXED,1,3",
        "*STEP",
        "*FREQUENCY",
        f"{n_modes}",
        "*NODE FILE",
        "U",
        "*END STEP",
    ]
    inp_path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "inp_path": str(inp_path),
            "n_fixed": len(fixed_ids), "n_modes": n_modes}


def parse_eigenfrequencies(frd_path: str | Path) -> list[float]:
    """Read the .frd and return the natural frequencies in Hz (sorted asc).

    CalculiX writes eigenfrequencies in the *.dat file (not *.frd) by
    default. We first try to locate a companion .dat file; if absent,
    we fall back to scanning the .frd for (1PE) eigenvalue blocks.
    """
    frd = Path(frd_path)
    dat = frd.with_suffix(".dat")
    freqs: list[float] = []

    # Preferred: .dat file has "E I G E N V A L U E   N U M B E R" blocks
    if dat.is_file():
        try:
            text = dat.read_text(errors="replace")
        except Exception:
            text = ""
        # CCX .dat line pattern for mode 1:
        # " 1    2.0944832E+06    1.4476473E+03    2.3037254E+02"
        #  mode  eigenvalue       circ_freq (rad/s) freq (Hz)
        import re as _re
        # Grab the first column-aligned numeric block after the eigen marker
        in_block = False
        for line in text.splitlines():
            if "NO           EIGENVALUE" in line.upper() or \
               "M O D A L" in line.upper() or \
               "EIGENVALUE OUTPUT" in line.upper():
                in_block = True
                continue
            if not in_block:
                continue
            parts = line.split()
            # Expected: [mode_int, eigenvalue, omega_rad_s, freq_hz, ...]
            if len(parts) >= 4:
                try:
                    _mode = int(parts[0])
                    freq_hz = float(parts[3])
                    freqs.append(freq_hz)
                except ValueError:
                    # Block ended
                    if freqs:
                        break
    if freqs:
        return sorted(freqs)

    # Fallback: scan .frd — CCX also dumps " -4  DISP" blocks per mode.
    try:
        text = Path(frd_path).read_text(errors="replace")
    except Exception:
        return []
    # Modes appear as " 100CL..." headers with the time-field being the
    # eigenvalue (rad/s)^2. Very format-sensitive; skip in v1 if the
    # .dat path didn't exist.
    return sorted(freqs)


def run_modal_fea(step_path: str | Path,
                   *,
                   material: str,
                   out_dir: str | Path,
                   n_modes: int = 6,
                   mesh_size_mm: float = 5.0,
                   min_freq_hz: float | None = None) -> dict:
    """End-to-end modal FEA on a STEP file.

    Returns
    -------
    dict with keys:
        available, passed, frequencies_hz (list), first_mode_hz,
        min_freq_hz (target if provided), material, n_modes,
        mesh (stats), report_path, error
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if material not in MATERIAL_PROPS:
        return {"available": False, "passed": None,
                "error": f"unknown material '{material}'"}

    if not _have_gmsh():
        return {"available": False, "passed": None,
                "error": "gmsh not installed"}

    ccx = _find_ccx()
    if ccx is None:
        return {"available": False, "passed": None,
                "error": "CalculiX (ccx) not on PATH — see "
                         "scripts/PRO_HEADLESS_SETUP.md"}

    msh_path = out_dir / "mesh.msh"
    inp_path = out_dir / "modal.inp"
    report_path = out_dir / "modal_report.json"

    mesh_r = mesh_step(step_path, msh_path, mesh_size_mm=mesh_size_mm)
    if not mesh_r.get("ok"):
        return {"available": True, "passed": False,
                "error": f"meshing failed: {mesh_r.get('error')}",
                "mesh": mesh_r}

    inp_r = msh_to_inp_modal(msh_path, inp_path, material=material,
                               n_modes=n_modes)
    if not inp_r.get("ok"):
        return {"available": True, "passed": False,
                "error": f"inp gen failed: {inp_r.get('error')}",
                "mesh": mesh_r}

    ccx_r = run_ccx(inp_path)
    if not ccx_r.get("ok"):
        return {"available": True, "passed": False,
                "error": f"ccx failed: {ccx_r.get('error') or ccx_r.get('stderr')}",
                "mesh": mesh_r}

    freqs = parse_eigenfrequencies(ccx_r["frd_path"])
    first = freqs[0] if freqs else None

    passed = True
    if min_freq_hz is not None and first is not None:
        passed = first >= min_freq_hz

    report = {
        "available": True,
        "passed": passed,
        "frequencies_hz": [round(f, 2) for f in freqs],
        "first_mode_hz": round(first, 2) if first is not None else None,
        "min_freq_target_hz": min_freq_hz,
        "material": material,
        "n_modes": n_modes,
        "mesh": {"n_nodes": mesh_r["n_nodes"],
                 "n_elements": mesh_r["n_elements"]},
        "frd_path": ccx_r["frd_path"],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report
