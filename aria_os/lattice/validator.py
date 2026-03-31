from __future__ import annotations

from .params import LatticeParams


MIN_FEATURE = {
    "fdm": {"strut": 1.5, "wall": 0.8, "hole": 2.0},
    "dmls": {"strut": 0.8, "wall": 0.4, "hole": 1.0},
}


def validate_params(params: LatticeParams) -> list[str]:
    """
    Check params against process limits.
    Returns list of warnings (empty = all clear).
    """
    warnings: list[str] = []

    processes = ["fdm", "dmls"] if params.process == "both" else [params.process]

    for proc in processes:
        limits = MIN_FEATURE[proc]

        if params.strut_diameter_mm < limits["strut"]:
            warnings.append(
                f"[{proc.upper()}] Strut {params.strut_diameter_mm}mm "
                f"< minimum {limits['strut']}mm — will not print reliably"
            )

        if params.skin_thickness_mm < limits["wall"]:
            warnings.append(
                f"[{proc.upper()}] Skin {params.skin_thickness_mm}mm "
                f"< minimum wall {limits['wall']}mm"
            )

        if params.frame_thickness_mm < limits["wall"] * 3:
            warnings.append(
                f"[{proc.upper()}] Frame {params.frame_thickness_mm}mm "
                f"may be too thin for structural integrity"
            )

        # Cell size vs strut ratio
        if params.cell_size_mm < params.strut_diameter_mm * 3:
            warnings.append(
                f"Cell size {params.cell_size_mm}mm is less than 3x "
                f"strut diameter — cells will merge into solid"
            )

    # Overhang warning for FDM
    if params.process in ["fdm", "both"]:
        if params.pattern == "arc_weave":
            warnings.append(
                "[FDM] Arc weave has overhanging arcs — print at "
                "45deg orientation or use supports"
            )
        if params.pattern == "octet_truss":
            warnings.append(
                "[FDM] Octet truss has diagonal struts — "
                "print orientation matters for overhang angles"
            )

    return warnings


def estimate_weight(params: LatticeParams, volume_fraction: float) -> float:
    """
    Estimate part weight given lattice volume fraction.
    volume_fraction: 0-1, fraction of bounding box that is solid
    """
    densities = {
        "fdm": 1.24,  # PLA g/cc
        "dmls": 7.85,  # Steel g/cc
    }
    proc = "fdm" if params.process == "fdm" else "dmls"
    density = densities[proc]

    total_vol_cc = (params.width_mm * params.height_mm * params.depth_mm) / 1000.0
    solid_vol_cc = total_vol_cc * volume_fraction
    return solid_vol_cc * density

