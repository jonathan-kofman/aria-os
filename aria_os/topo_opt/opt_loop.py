"""
FEA-driven topology optimization loop.

Pipeline per iter:
  1. Build a lattice SDF (octet or gyroid) inside the envelope with a
     spatially-varying thickness driven by the previous iter's stress
     field (iter 0 uses uniform thickness = t_max).
  2. March the SDF to an STL mesh.
  3. Run CalculiX static FEA on the STL (STL -> STEP via gmsh, then
     existing run_static_fea pipeline).
  4. Parse the .frd + the .inp node coords, interpolate onto a 3D grid
     to make a callable stress_field(x, y, z) -> MPa.
  5. Build the next density field with fgm_stress_driven_density.
  6. Record max_stress_mpa, safety_factor, mass_g and continue.

Convergence:
  * iter == max_iters, OR
  * safety_factor improved by < 10% between consecutive iters, OR
  * safety_factor >= target_safety_factor everywhere (we check against
    max von Mises — if that passes target, we are globally safe).

Graceful-degrade:
  Neither gmsh nor ccx are imported at module load. If either is missing
  at run time, the function short-circuits with {"available": False, ...}
  and still returns a well-formed iter list so callers don't crash.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Callable

import numpy as np


# ---------------------------------------------------------------------------
# FRD parsing — extend the existing max-VM parser to return per-node values
# ---------------------------------------------------------------------------

def _parse_frd_per_node_vm(frd_path: str | Path) -> dict[int, float]:
    """Scan a CalculiX .frd for the STRESS block, compute von Mises per
    node, return {node_id: vm_mpa}.

    Mirrors the pattern in aria_os.fea.calculix_stage.parse_max_von_mises
    but keeps every node instead of just the max.
    """
    out: dict[int, float] = {}
    try:
        lines = Path(frd_path).read_text(errors="replace").splitlines()
    except Exception:
        return out

    in_stress = False
    for line in lines:
        if line.startswith(" -4") and "STRESS" in line:
            in_stress = True
            continue
        if in_stress and line.startswith(" -3"):
            break
        if in_stress and line.startswith(" -1"):
            parts = line.split()
            if len(parts) >= 8:
                try:
                    node_id = int(parts[1])
                    sxx, syy, szz = (float(x) for x in parts[2:5])
                    sxy, syz, szx = (float(x) for x in parts[5:8])
                    vm = (((sxx - syy) ** 2 + (syy - szz) ** 2
                           + (szz - sxx) ** 2
                           + 6 * (sxy * sxy + syz * syz + szx * szx)) / 2) ** 0.5
                    out[node_id] = float(vm)
                except ValueError:
                    pass
    return out


def _parse_inp_nodes(inp_path: str | Path) -> dict[int, tuple[float, float, float]]:
    """Extract node_id -> (x,y,z) mapping from a CalculiX .inp file.

    Matches the writer in aria_os.fea.calculix_stage.msh_to_inp: the
    *NODE block starts with '*NODE, NSET=NALL' and each line is
    'id,x,y,z'. Stops at the next '*' keyword.
    """
    nodes: dict[int, tuple[float, float, float]] = {}
    try:
        text = Path(inp_path).read_text(errors="replace")
    except Exception:
        return nodes

    in_nodes = False
    for line in text.splitlines():
        ls = line.strip()
        if not ls:
            continue
        if ls.upper().startswith("*NODE"):
            in_nodes = True
            continue
        if in_nodes and ls.startswith("*"):
            break
        if in_nodes:
            parts = ls.split(",")
            if len(parts) >= 4:
                try:
                    nid = int(parts[0])
                    x, y, z = (float(p) for p in parts[1:4])
                    nodes[nid] = (x, y, z)
                except ValueError:
                    continue
    return nodes


def stress_field_from_ccx_frd(frd_path: str | Path,
                              bounds: tuple,
                              resolution: float = 2.0,
                              inp_path: str | Path | None = None
                              ) -> Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]:
    """Parse a CalculiX .frd, extract per-node von Mises, interpolate
    onto a regular grid, and return a callable f(x, y, z) -> vm_mpa
    that works with scalars and numpy arrays.

    Parameters
    ----------
    frd_path : path to the .frd output of CalculiX.
    bounds   : ((x0, y0, z0), (x1, y1, z1)) in mm.
    resolution : grid step (mm). 2mm default is a reasonable match to
                 the FEA mesh size.
    inp_path : optional path to sibling .inp for node coords. If None,
               defaults to the same stem as frd_path with .inp suffix.

    Returns
    -------
    callable(x, y, z) -> vm_mpa (numpy-broadcast compatible).

    If parsing fails (file missing, empty stress block, etc.), returns
    a zero field — callers should check for this via the returned
    metadata in run_topo_opt.
    """
    frd_path = Path(frd_path)
    if inp_path is None:
        inp_path = frd_path.with_suffix(".inp")
    inp_path = Path(inp_path)

    vm_map = _parse_frd_per_node_vm(frd_path)
    node_xyz = _parse_inp_nodes(inp_path)

    # Build coord + value arrays from the intersection of the two maps
    ids = [nid for nid in vm_map if nid in node_xyz]
    if not ids:
        def zero_field(x, y, z):
            return np.zeros_like(np.asarray(x, dtype=float))
        return zero_field

    coords = np.array([node_xyz[i] for i in ids], dtype=float)
    values = np.array([vm_map[i] for i in ids], dtype=float)

    (x0, y0, z0), (x1, y1, z1) = bounds
    xs = np.arange(x0, x1 + resolution, resolution)
    ys = np.arange(y0, y1 + resolution, resolution)
    zs = np.arange(z0, z1 + resolution, resolution)
    Xg, Yg, Zg = np.meshgrid(xs, ys, zs, indexing="ij")
    gridpts = np.stack([Xg.ravel(), Yg.ravel(), Zg.ravel()], axis=-1)

    # Interpolate. Try scipy.griddata (linear) for accuracy; fall back to
    # a simple inverse-distance weighted scheme so this module still
    # works without scipy.
    try:
        from scipy.interpolate import griddata  # type: ignore
        grid_vals = griddata(coords, values, gridpts, method="linear",
                             fill_value=float(values.mean()))
    except Exception:
        # Inverse-distance fallback (k=4 nearest). Slow for huge grids
        # but correct.
        grid_vals = np.empty(len(gridpts), dtype=float)
        for i, p in enumerate(gridpts):
            d = np.sqrt(((coords - p) ** 2).sum(axis=1))
            k = min(4, len(d))
            idx = np.argpartition(d, k - 1)[:k]
            w = 1.0 / np.maximum(d[idx], 1e-6)
            grid_vals[i] = float((values[idx] * w).sum() / w.sum())

    grid = grid_vals.reshape(Xg.shape)

    def sample(x, y, z):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        z = np.asarray(z, dtype=float)
        ix = np.clip(np.round((x - x0) / resolution).astype(int),
                     0, grid.shape[0] - 1)
        iy = np.clip(np.round((y - y0) / resolution).astype(int),
                     0, grid.shape[1] - 1)
        iz = np.clip(np.round((z - z0) / resolution).astype(int),
                     0, grid.shape[2] - 1)
        return grid[ix, iy, iz]

    return sample


# ---------------------------------------------------------------------------
# Bounding box helper — sample the envelope SDF to find a reasonable box
# ---------------------------------------------------------------------------

def _envelope_bounds(envelope_sdf, probe: float = 50.0,
                     res: float = 4.0) -> tuple:
    """Coarsely probe an SDF to find a bounding box of its interior.

    Used when the caller doesn't pass explicit bounds. We sample a
    cube of +-probe mm at res resolution, find inside voxels, pad by
    one cell. Cheap, robust for parts that fit in ~100mm.
    """
    xs = np.arange(-probe, probe + res, res)
    X, Y, Z = np.meshgrid(xs, xs, xs, indexing="ij")
    field = envelope_sdf(X, Y, Z)
    mask = field < 0
    if not mask.any():
        # Fallback: assume ~40mm cube
        return ((-20.0, -20.0, -20.0), (20.0, 20.0, 20.0))
    xi = X[mask]
    yi = Y[mask]
    zi = Z[mask]
    pad = res
    return ((float(xi.min()) - pad, float(yi.min()) - pad, float(zi.min()) - pad),
            (float(xi.max()) + pad, float(yi.max()) + pad, float(zi.max()) + pad))


# ---------------------------------------------------------------------------
# Lattice factory — thickness is a callable density field
# ---------------------------------------------------------------------------

def _build_graded_lattice(lattice_type: str, cell_size_mm: float,
                          density_field: Callable[..., np.ndarray] | None,
                          t_min: float, t_max: float,
                          envelope_sdf):
    """Return an SDF f(x,y,z) = intersection(envelope, lattice(thickness=rho(x,y,z))).

    density_field: callable returning local thickness in [t_min, t_max].
                   If None, uses uniform t_max (iter 0).
    """
    from aria_os.generators.sdf_generator import sdf_gyroid, op_intersection
    from aria_os.sdf.lattices import sdf_octet_truss

    if density_field is None:
        def density_field(x, y, z):  # noqa: E306
            return np.full_like(np.asarray(x, dtype=float), t_max)

    k = 2 * math.pi / cell_size_mm

    if lattice_type == "gyroid":
        def lattice(x, y, z):
            rho = np.clip(density_field(x, y, z), t_min, t_max)
            val = (np.sin(k * x) * np.cos(k * y)
                   + np.sin(k * y) * np.cos(k * z)
                   + np.sin(k * z) * np.cos(k * x))
            return np.abs(val) - rho / 2.0
        base = lattice
    elif lattice_type == "octet":
        # Octet truss thickness varies as beam_radius. Because
        # sdf_octet_truss bakes beam_radius in as a closure we rebuild
        # the strut geometry inline to let radius be a field.
        # For simplicity we scale an initial uniform-radius field by
        # rho / t_max — cheap and captures the density gradient.
        base_lat = sdf_octet_truss(cell_size=cell_size_mm,
                                   beam_radius=t_max / 2.0)

        def lattice(x, y, z):
            rho = np.clip(density_field(x, y, z), t_min, t_max)
            # shift distance field by (t_max/2 - rho/2) so thinner-rho
            # regions yield a larger d, sparser lattice.
            d = base_lat(x, y, z)
            return d + (t_max / 2.0 - rho / 2.0)
        base = lattice
    else:
        raise ValueError(
            f"lattice_type={lattice_type!r} not supported in v1; "
            f"choose 'octet' or 'gyroid'.")

    return op_intersection(envelope_sdf, base)


# ---------------------------------------------------------------------------
# STL -> STEP via gmsh (so we can reuse run_static_fea that wants STEP)
# ---------------------------------------------------------------------------

def _stl_to_step(stl_path: str | Path, step_path: str | Path) -> dict:
    """Convert an STL to a STEP via gmsh. Required because
    run_static_fea's existing pipeline takes a STEP.

    Returns {ok, step_path, error?}.
    """
    try:
        import gmsh  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"gmsh not installed: {exc}"}

    stl_path = str(Path(stl_path).resolve())
    step_path = str(Path(step_path).resolve())
    Path(step_path).parent.mkdir(parents=True, exist_ok=True)

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.merge(stl_path)
        # Create a surface + volume out of the merged triangulation
        gmsh.model.mesh.classifySurfaces(math.pi / 4, True, True, math.pi / 4)
        gmsh.model.mesh.createGeometry()
        surfs = gmsh.model.getEntities(2)
        loop = gmsh.model.geo.addSurfaceLoop([s[1] for s in surfs])
        gmsh.model.geo.addVolume([loop])
        gmsh.model.geo.synchronize()
        gmsh.write(step_path)
        return {"ok": True, "step_path": step_path}
    except Exception as exc:
        return {"ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        gmsh.finalize()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_topo_opt(*,
                 envelope_sdf,
                 load_case: dict,
                 material: str,
                 out_dir: str | Path,
                 max_iters: int = 3,
                 target_safety_factor: float = 2.0,
                 cell_size_mm: float = 8.0,
                 t_min: float = 0.3,
                 t_max: float = 2.0,
                 mesh_resolution_mm: float = 2.0,
                 lattice_type: str = "octet",
                 sdf_resolution_mm: float = 1.0,
                 bounds: tuple | None = None) -> dict:
    """Iterative stress-driven topology optimization.

    Parameters
    ----------
    envelope_sdf : callable SDF f(x,y,z) defining the max design volume.
    load_case    : {"load_n": float, "fixed_z_below_mm": float}. Pushes
                   the top of the part in -Z, fixes the bottom layer.
    material     : material key from aria_os.fea.calculix_stage.MATERIAL_PROPS.
    out_dir      : working dir — each iter gets a subfolder iter_NN.
    max_iters    : stop after this many loop iterations.
    target_safety_factor : stop when max-stress safety factor exceeds this.
    cell_size_mm : lattice unit-cell size.
    t_min, t_max : min/max beam or wall thickness (mm).
    mesh_resolution_mm : FEA tet size.
    lattice_type : 'octet' or 'gyroid' for v1.
    sdf_resolution_mm : marching-cubes resolution for the lattice mesh.
    bounds       : ((x0,y0,z0),(x1,y1,z1)) in mm. Auto-probed if None.

    Returns
    -------
    dict with keys:
      available : bool (False if gmsh or ccx missing)
      converged : bool
      iters     : list of per-iter dicts (see keys below)
      final_geometry_path : str | None
      final_density_field : callable | None  (not JSON-serializable)
      error     : str | None

    Each iter dict:
      iter, max_stress_mpa, safety_factor, mass_g,
      mesh_path, step_path, frd_path, n_nodes, n_elements, error
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Availability check (graceful degrade) ---
    try:
        from aria_os.fea.calculix_stage import (
            MATERIAL_PROPS, _find_ccx, _have_gmsh, run_static_fea,
        )
    except Exception as exc:
        return {"available": False, "converged": False, "iters": [],
                "final_geometry_path": None, "final_density_field": None,
                "error": f"fea module import failed: {exc}"}

    if material not in MATERIAL_PROPS:
        return {"available": False, "converged": False, "iters": [],
                "final_geometry_path": None, "final_density_field": None,
                "error": f"unknown material {material!r}"}
    mp = MATERIAL_PROPS[material]

    have_gmsh = _have_gmsh()
    have_ccx = _find_ccx() is not None

    if bounds is None:
        try:
            bounds = _envelope_bounds(envelope_sdf)
        except Exception as exc:
            return {"available": False, "converged": False, "iters": [],
                    "final_geometry_path": None,
                    "final_density_field": None,
                    "error": f"bounds probe failed: {exc}"}

    # Lattice type guard
    if lattice_type not in {"octet", "gyroid"}:
        return {"available": False, "converged": False, "iters": [],
                "final_geometry_path": None, "final_density_field": None,
                "error": f"lattice_type {lattice_type!r} not in "
                         "{'octet','gyroid'} — v1 only supports these."}

    load_n = float(load_case.get("load_n", 500.0))
    fixed_z_below_mm = float(load_case.get("fixed_z_below_mm", 2.0))

    iters: list[dict] = []
    density_field: Callable | None = None  # iter 0 uses uniform t_max
    prev_sf: float | None = None
    converged = False
    final_stl: str | None = None

    # If tools are missing, emit a single stub iter so callers see the
    # expected shape without crashing.
    if not have_gmsh or not have_ccx:
        stub = {"iter": 0, "max_stress_mpa": None, "safety_factor": None,
                "mass_g": None, "mesh_path": None, "step_path": None,
                "frd_path": None, "n_nodes": 0, "n_elements": 0,
                "error": ("gmsh not installed" if not have_gmsh
                          else "ccx not on PATH")}
        iters.append(stub)
        return {
            "available": False,
            "converged": False,
            "iters": iters,
            "final_geometry_path": None,
            "final_density_field": None,
            "error": stub["error"],
        }

    # --- Iteration loop ---
    for i in range(max_iters):
        iter_dir = out_dir / f"iter_{i:02d}"
        iter_dir.mkdir(parents=True, exist_ok=True)

        iter_rec: dict = {"iter": i, "error": None}

        # 1. Build graded lattice SDF
        try:
            lat_sdf = _build_graded_lattice(
                lattice_type, cell_size_mm, density_field,
                t_min, t_max, envelope_sdf)
        except Exception as exc:
            iter_rec["error"] = f"lattice build failed: {exc}"
            iters.append(iter_rec)
            break

        # 2. March to STL
        try:
            from aria_os.generators.sdf_generator import SDFScene
            scene = SDFScene(resolution=sdf_resolution_mm, padding=2.0)
            mesh_data = scene.to_mesh(lat_sdf, bounds)
            stl_path = iter_dir / "part.stl"
            scene.export_stl(mesh_data, stl_path)
            iter_rec["mesh_path"] = str(stl_path)
            final_stl = str(stl_path)
        except Exception as exc:
            iter_rec["error"] = f"meshing failed: {exc}"
            iters.append(iter_rec)
            break

        # 3. STL -> STEP for FEA
        step_path = iter_dir / "part.step"
        step_r = _stl_to_step(stl_path, step_path)
        if not step_r.get("ok"):
            iter_rec["error"] = f"stl->step failed: {step_r.get('error')}"
            iter_rec["step_path"] = None
            iters.append(iter_rec)
            break
        iter_rec["step_path"] = str(step_path)

        # 4. FEA
        fea_r = run_static_fea(
            step_path=step_path,
            material=material,
            load_n=load_n,
            out_dir=iter_dir / "fea",
            mesh_size_mm=mesh_resolution_mm,
            target_safety_factor=target_safety_factor,
        )
        if not fea_r.get("available") or fea_r.get("max_stress_mpa") is None:
            iter_rec["error"] = fea_r.get("error", "fea unavailable")
            iter_rec["frd_path"] = fea_r.get("frd_path")
            iters.append(iter_rec)
            break

        max_vm = float(fea_r["max_stress_mpa"])
        sf = float(fea_r["safety_factor"])
        iter_rec["max_stress_mpa"] = max_vm
        iter_rec["safety_factor"] = sf
        iter_rec["frd_path"] = fea_r.get("frd_path")
        iter_rec["n_nodes"] = fea_r.get("mesh", {}).get("n_nodes", 0)
        iter_rec["n_elements"] = fea_r.get("mesh", {}).get("n_elements", 0)

        # 5. Mass estimate (lattice SDF, sampled cheaply)
        try:
            from aria_os.sdf.analysis import compute_mass
            iter_rec["mass_g"] = compute_mass(
                lat_sdf, bounds,
                density_kg_m3=mp["density_kg_m3"],
                resolution=max(1.5, sdf_resolution_mm * 1.5))
        except Exception:
            iter_rec["mass_g"] = None

        iters.append(iter_rec)

        # 6. Convergence checks
        if sf >= target_safety_factor:
            converged = True
            break
        if prev_sf is not None and prev_sf > 0:
            if abs(sf - prev_sf) / prev_sf < 0.10:
                # safety factor isn't moving — call it converged
                converged = True
                break
        prev_sf = sf

        # 7. Build the next density field from the FEA stress field
        try:
            from aria_os.sdf.fgm import fgm_stress_driven_density
            stress_fn = stress_field_from_ccx_frd(
                fea_r["frd_path"], bounds,
                resolution=mesh_resolution_mm,
                inp_path=Path(iter_dir) / "fea" / "job.inp")
            density_field = fgm_stress_driven_density(
                stress_fn,
                yield_mpa=mp["yield_mpa"],
                t_min=t_min, t_max=t_max)
        except Exception as exc:
            iter_rec["error"] = f"density-field build failed: {exc}"
            break

    # --- Finalize ---
    report = {
        "available": True,
        "converged": converged,
        "iters": iters,
        "final_geometry_path": final_stl,
        # NOTE: callable — not JSON-serializable. Callers that want to
        # persist state should re-derive from the final frd_path.
        "final_density_field": density_field,
        "error": None,
    }

    # JSON-safe copy of the report (drops the callable)
    try:
        safe = {k: v for k, v in report.items()
                if k != "final_density_field"}
        (out_dir / "topo_opt_report.json").write_text(
            json.dumps(safe, indent=2), encoding="utf-8")
    except Exception:
        pass
    return report
