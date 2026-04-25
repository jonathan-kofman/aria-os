"""Sheet metal bend allowance table.

The K-factor maps the position of the neutral axis from the inner
surface of a bend (0.0 = inside surface, 0.5 = midplane, 1.0 = outside).
Sheet metal flat patterns calculate flat-blank length using:

    bend_allowance = (π/2 × angle_rad) × (R + K × t)

where R = inside bend radius, t = sheet thickness.

Pick the right K-factor for the material + thickness or your laser-
cut blank ends up the wrong size.

Source: ASM Metals Handbook, sheet metal forming chapter, plus
SheetMetal.Me bend allowance reference. K-factors are conservative
defaults for typical hand-formed parts; tighter tolerances need
empirical calibration on the actual brake.
"""
from __future__ import annotations

# K-factor by (material_family, thickness_mm range).
# Each entry: ((min_t, max_t), K_factor)
_K_FACTOR_TABLE: dict[str, list[tuple[tuple[float, float], float]]] = {
    "aluminum_soft": [   # 1100, 3003, 5052
        ((0.0, 1.0),  0.33),
        ((1.0, 3.0),  0.40),
        ((3.0, 6.0),  0.42),
        ((6.0, 99.0), 0.45),
    ],
    "aluminum_hard": [   # 6061-T6, 7075-T6
        ((0.0, 1.0),  0.42),
        ((1.0, 3.0),  0.45),
        ((3.0, 6.0),  0.48),
        ((6.0, 99.0), 0.50),
    ],
    "steel_mild": [      # 1008, 1010, 1018, A36
        ((0.0, 1.0),  0.38),
        ((1.0, 3.0),  0.42),
        ((3.0, 6.0),  0.44),
        ((6.0, 99.0), 0.46),
    ],
    "stainless": [       # 304, 316
        ((0.0, 1.0),  0.40),
        ((1.0, 3.0),  0.43),
        ((3.0, 6.0),  0.45),
        ((6.0, 99.0), 0.47),
    ],
    "copper_brass": [
        ((0.0, 1.0),  0.36),
        ((1.0, 3.0),  0.40),
        ((3.0, 6.0),  0.42),
        ((6.0, 99.0), 0.45),
    ],
}

# Minimum inside bend radius (mm) by material + thickness.
# Bending tighter risks cracking on the outside fibre.
# Rule of thumb:  R_min = c × t  where c depends on material.
_MIN_BEND_RADIUS_C: dict[str, float] = {
    "aluminum_soft":   1.0,
    "aluminum_hard":   2.5,   # 6061-T6 cracks at <2.5×t
    "steel_mild":      1.0,
    "stainless":       1.5,
    "copper_brass":    1.0,
}

# Bend relief (the slot cut at each end of an interior bend so the
# sheet doesn't tear). Typical: width = 1.5×t, length = R + 0.5×t,
# placed at each end of the bend line.
def relief_size(thickness_mm: float, bend_radius_mm: float
                ) -> tuple[float, float]:
    """Return (width_mm, length_mm) for the bend-relief slot."""
    t = float(thickness_mm)
    r = float(bend_radius_mm)
    return (1.5 * t, r + 0.5 * t)


# Material-name normalization: accept loose strings like "AL", "6061",
# "1018 steel", and resolve to the family key.
_NAME_TO_FAMILY: list[tuple[tuple[str, ...], str]] = [
    (("6061", "7075", "2024", "hardened aluminum"), "aluminum_hard"),
    (("1100", "3003", "5052", "soft aluminum"),     "aluminum_soft"),
    (("aluminum", "aluminium"),                      "aluminum_soft"),
    # Copper/brass/bronze BEFORE stainless so "brass" doesn't get
    # eaten by "ss" substring inside "stainless"'s alias list.
    (("copper", "brass", "bronze"),                  "copper_brass"),
    (("304", "316", "stainless"),                    "stainless"),
    (("a36", "1008", "1010", "1018", "1020",
      "mild steel", "carbon steel"),                 "steel_mild"),
    (("steel",),                                     "steel_mild"),
]


def material_family(material: str) -> str:
    """Normalize loose material names to the table's family key."""
    s = (material or "").lower().strip()
    for keywords, fam in _NAME_TO_FAMILY:
        if any(k in s for k in keywords):
            return fam
    return "steel_mild"


def k_factor(material: str, thickness_mm: float) -> float:
    """Return the K-factor for a given material + sheet gauge."""
    fam = material_family(material)
    table = _K_FACTOR_TABLE[fam]
    t = float(thickness_mm)
    for (lo, hi), k in table:
        if lo <= t < hi:
            return k
    return table[-1][1]


def min_bend_radius(material: str, thickness_mm: float) -> float:
    """Return the minimum inside bend radius (mm) for the material +
    sheet gauge. Bending tighter risks outer-fibre cracking."""
    fam = material_family(material)
    return _MIN_BEND_RADIUS_C.get(fam, 1.0) * float(thickness_mm)


def bend_allowance(angle_deg: float, inner_radius_mm: float,
                    thickness_mm: float, k: float) -> float:
    """Standard bend allowance formula. Returns the developed length
    of the bend region — add to the flat lengths of the two arms to
    get the flat-blank length."""
    import math
    return (math.pi * angle_deg / 180.0) * (
        float(inner_radius_mm) + k * float(thickness_mm))


def bend_table_summary_for_prompt() -> str:
    """Format the most common rows as a compact LLM-prompt block.
    Used by lean_engineering_prompt when SHEET METAL family matches."""
    lines = ["K-factor (≈ neutral-axis position from inside surface):"]
    for fam, rows in _K_FACTOR_TABLE.items():
        compact = ", ".join(f"{lo}-{hi}mm K={k:.2f}" for (lo, hi), k in rows)
        lines.append(f"  {fam}: {compact}")
    lines.append("Min inside bend radius = c × t:")
    for fam, c in _MIN_BEND_RADIUS_C.items():
        lines.append(f"  {fam}: c={c:g}")
    lines.append("Default bend angle 90°. Bend allowance = "
                  "(π·angle/180) × (R + K·t).")
    return "\n".join(lines)


__all__ = [
    "k_factor", "min_bend_radius", "bend_allowance",
    "material_family", "relief_size",
    "bend_table_summary_for_prompt",
]
