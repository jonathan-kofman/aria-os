"""ISO 1302 surface roughness callouts.

Ra (arithmetic mean roughness) values engineers expect on drawings.
Used to pick sensible defaults per process and feature type, and to
emit the correct callout symbol on drawings.
"""
from __future__ import annotations

# Typical Ra achievable by process (µm / micrometre)
Ra_BY_PROCESS = {
    "CNC_milling":        3.2,   # stock finish, no polish
    "CNC_turning":        1.6,
    "CNC_grinding":       0.8,
    "CNC_polished":       0.2,
    "FDM":               12.5,   # layer lines visible
    "SLA":                3.2,
    "SLS":                6.3,
    "casting":            6.3,
    "forging":           12.5,
    "sheet_metal":        3.2,   # cold-rolled baseline
    "laser_cut_edge":     6.3,
    "waterjet":           6.3,
}

# Feature-specific finish requirements (mm ↓ Ra)
Ra_BY_FEATURE = {
    "sealing_face":       3.2,   # flange gasket face
    "bearing_surface":    1.6,   # rotating fit
    "sliding_surface":    0.8,   # linear bearing
    "mating_surface":     3.2,   # bolted joint
    "threaded_hole":      3.2,
    "cosmetic_exterior":  1.6,
    "as_machined":        3.2,
    "general":            3.2,
}


def surface_finish_default(process: str | None = None,
                            feature: str | None = None) -> float:
    """Pick a reasonable default Ra in µm. Feature wins over process."""
    if feature:
        f = feature.lower()
        if f in Ra_BY_FEATURE:
            return Ra_BY_FEATURE[f]
    if process:
        p = process.replace(" ", "_")
        if p in Ra_BY_PROCESS:
            return Ra_BY_PROCESS[p]
    return 3.2   # general machining default


def finish_callout(ra_um: float) -> str:
    """Drawing-ready surface finish callout per ISO 1302."""
    return f"Ra {ra_um:g} µm"
