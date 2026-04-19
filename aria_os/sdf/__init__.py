"""
aria_os.sdf — professional-grade SDF geometry kernel.

Expanded from aria_os/generators/sdf_generator.py with the primitives,
lattices, FGM, analysis, and export capabilities needed to compete with
nTop / Hyperganic / General Lattice on serious lattice + implicit work.

Submodules:
  primitives   — 20+ primitives (rounded box, ellipsoid, extruded, revolved,
                 hexagon, pyramid, chamfered box, ...) plus full 3-axis
                 rotation, mirror, taper, axis-symmetry
  lattices     — all 5 major TPMS (Gyroid, Schwarz-P, Schwarz-W, Diamond,
                 IWP, Neovius) plus strut lattices (BCC, FCC, octet-truss,
                 Kagome, honeycomb-2D) and stochastic beams
  operators    — displace, engrave text, morph, shell, round/chamfer by SDF
  fgm          — functionally-graded material (density gradients for AM)
  analysis     — volume, mass, CoG, min-feature check, overhang analysis
  export       — STL (existing), OBJ, 3MF, PLY

Backward compat:
  aria_os.generators.sdf_generator.* still works; new modules live here
  and re-export the classics.
"""
from __future__ import annotations

# Re-export the existing primitives + ops so callers of the new package
# have the full surface without needing two import roots.
from aria_os.generators.sdf_generator import (  # noqa: F401
    sdf_sphere, sdf_box, sdf_cylinder, sdf_torus, sdf_capsule, sdf_cone,
    sdf_gyroid, sdf_schwarz_p, sdf_diamond, sdf_lattice_cubic,
    op_union, op_intersection, op_difference,
    op_smooth_union, op_smooth_difference,
    op_offset, op_shell, op_twist, op_bend,
    op_repeat, op_scale, op_translate, op_rotate_z,
    SDFScene,
)

# New pro-grade additions
from .primitives import (  # noqa: F401
    sdf_ellipsoid, sdf_rounded_box, sdf_chamfered_box,
    sdf_hexagonal_prism, sdf_triangular_prism, sdf_pyramid,
    sdf_plane, sdf_half_space, sdf_line_segment,
    sdf_extrude_2d, sdf_revolve_profile,
    op_rotate_x, op_rotate_y, op_rotate_axis_angle, op_rotate_euler,
    op_mirror, op_taper, op_axial_symmetry,
)
from .lattices import (  # noqa: F401
    sdf_schwarz_w, sdf_iwp, sdf_neovius, sdf_frd,
    sdf_octet_truss, sdf_bcc_lattice, sdf_fcc_lattice,
    sdf_kagome_lattice, sdf_honeycomb_2d,
    sdf_stochastic_beams,
)
from .operators import (  # noqa: F401
    op_displace, op_morph, op_round_sdf, op_chamfer_sdf,
    op_engrave_text,
)
from .fgm import (  # noqa: F401
    fgm_radial_gradient, fgm_linear_gradient,
    fgm_stress_driven_density,
)
from .analysis import (  # noqa: F401
    compute_volume, compute_mass, compute_cog,
    compute_bbox, check_min_feature_size, check_overhangs,
    mesh_stats_full,
)
from .export import (  # noqa: F401
    export_obj, export_3mf, export_ply,
)
