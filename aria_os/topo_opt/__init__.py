"""
aria_os.topo_opt — FEA-driven topology optimization loop.

The generative-design wedge that nTop sells: start from a design envelope
SDF, run CalculiX FEA to get per-voxel stress, then use
`fgm_stress_driven_density` to vary lattice thickness so material
flows to where it's needed. Iterate until converged.

High-level usage:

    from aria_os.sdf.primitives import sdf_rounded_box  # or any envelope SDF
    from aria_os.topo_opt import run_topo_opt

    env = sdf_rounded_box(center=(0,0,0), size=(40,40,40), radius=2)
    r = run_topo_opt(
        envelope_sdf=env,
        load_case={"load_n": 500, "fixed_z_below_mm": 2.0},
        material="aluminum_6061",
        out_dir="outputs/topo",
        max_iters=3,
    )

Graceful-degrade: if gmsh or ccx are missing, returns
{"available": False, ...} with stubbed iter list.
"""
from __future__ import annotations

from .opt_loop import run_topo_opt, stress_field_from_ccx_frd

__all__ = ["run_topo_opt", "stress_field_from_ccx_frd"]
