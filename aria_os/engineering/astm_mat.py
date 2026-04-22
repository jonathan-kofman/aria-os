"""Material grades + mechanical properties for engineering planning.

Every callout string is the FULL drawing-ready spec (e.g. "6061-T6 per
ASTM B221") so drawings emit proper material blocks.

Properties are SI: yield + UTS in MPa, density in kg/m³, elastic
modulus in GPa. Used by planners to size walls (stress-based), by FEA
to assign materials, by Quote to look up cost per kg, by DFM to apply
process-specific wall minima.
"""
from __future__ import annotations

# Process-specific wall thickness minimums in mm.
# Engineers expect these as defaults; a planner's wall thickness
# default should never go below these without explicit override.
CNC_WALL_MIN_MM = {
    "aluminum": 1.5,   "steel": 1.0,   "stainless": 1.2,
    "brass": 1.5,      "copper": 1.5,  "titanium": 1.5,
    "plastic": 2.0,    "acetal": 1.5,
}
FDM_WALL_MIN_MM = {
    "PLA": 1.2, "ABS": 1.5, "PETG": 1.5, "Nylon": 2.0,
    "TPU": 2.0, "ASA": 1.5, "PC": 2.0,
}

# Grade name (lowercase key) → full drawing callout + properties
_MATERIALS = {
    # --- Aluminium alloys (machined) ---
    "6061-t6": {
        "callout":       "AL 6061-T6 per ASTM B221",
        "yield_mpa":     276, "uts_mpa": 310, "elong_pct": 12,
        "density_kgm3":  2700, "e_gpa":    68.9,
        "process":       "CNC",    "family":    "aluminum",
        "cost_usd_kg":   6.0,      "machinability": 0.90,
    },
    "5052-h32": {
        "callout":       "AL 5052-H32 per ASTM B209",
        "yield_mpa":     193, "uts_mpa": 228, "elong_pct": 12,
        "density_kgm3":  2680, "e_gpa":    70.3,
        "process":       "sheet",  "family":    "aluminum",
        "cost_usd_kg":   5.5,      "machinability": 0.85,
    },
    "7075-t6": {
        "callout":       "AL 7075-T6 per ASTM B211",
        "yield_mpa":     503, "uts_mpa": 572, "elong_pct":  11,
        "density_kgm3":  2810, "e_gpa":    71.7,
        "process":       "CNC",    "family":    "aluminum",
        "cost_usd_kg":   11.0,     "machinability": 0.70,
    },
    # --- Stainless ---
    "304ss":    {
        "callout":       "SS 304 per ASTM A240",
        "yield_mpa":     215, "uts_mpa": 505, "elong_pct": 40,
        "density_kgm3":  8000, "e_gpa":    193,
        "process":       "CNC",    "family":    "stainless",
        "cost_usd_kg":   8.0,      "machinability": 0.45,
    },
    "316l":     {
        "callout":       "SS 316L per ASTM A240",
        "yield_mpa":     170, "uts_mpa": 485, "elong_pct": 40,
        "density_kgm3":  7990, "e_gpa":    193,
        "process":       "CNC",    "family":    "stainless",
        "cost_usd_kg":   10.0,     "machinability": 0.45,
    },
    # --- Carbon steel ---
    "a36":      {
        "callout":       "Steel A36 per ASTM A36",
        "yield_mpa":     250, "uts_mpa": 400, "elong_pct": 20,
        "density_kgm3":  7860, "e_gpa":    200,
        "process":       "CNC",    "family":    "steel",
        "cost_usd_kg":   1.5,      "machinability": 0.70,
    },
    "1018":     {
        "callout":       "Steel 1018 CD per ASTM A108",
        "yield_mpa":     370, "uts_mpa": 440, "elong_pct": 15,
        "density_kgm3":  7870, "e_gpa":    200,
        "process":       "CNC",    "family":    "steel",
        "cost_usd_kg":   1.8,      "machinability": 0.78,
    },
    # --- Pipe-flange forged ---
    "a105":     {
        "callout":       "Carbon Steel A105, normalized per ASTM A105",
        "yield_mpa":     250, "uts_mpa": 485, "elong_pct": 22,
        "density_kgm3":  7850, "e_gpa":    200,
        "process":       "forged", "family":    "steel",
        "cost_usd_kg":   2.5,      "machinability": 0.70,
    },
    "a182-f316": {
        "callout":       "SS A182 F316 per ASTM A182",
        "yield_mpa":     205, "uts_mpa": 515, "elong_pct": 30,
        "density_kgm3":  8000, "e_gpa":    193,
        "process":       "forged", "family":    "stainless",
        "cost_usd_kg":   11.0,     "machinability": 0.45,
    },
    # --- 3D-print plastics ---
    "pla":      {
        "callout":       "PLA per ASTM D6400 (FDM)",
        "yield_mpa":     60,  "uts_mpa": 65,  "elong_pct":  5,
        "density_kgm3":  1240, "e_gpa":    3.5,
        "process":       "FDM",    "family":    "plastic",
        "cost_usd_kg":   25.0,     "machinability": 0.0,
    },
    "abs":      {
        "callout":       "ABS per ASTM D3965 (FDM)",
        "yield_mpa":     40,  "uts_mpa": 45,  "elong_pct":  8,
        "density_kgm3":  1050, "e_gpa":    2.0,
        "process":       "FDM",    "family":    "plastic",
        "cost_usd_kg":   30.0,     "machinability": 0.0,
    },
    "petg":     {
        "callout":       "PETG (FDM)",
        "yield_mpa":     50,  "uts_mpa": 55,  "elong_pct":  6,
        "density_kgm3":  1270, "e_gpa":    2.1,
        "process":       "FDM",    "family":    "plastic",
        "cost_usd_kg":   30.0,     "machinability": 0.0,
    },
}

# Common aliases → canonical key
_ALIASES = {
    "6061":          "6061-t6",
    "al 6061":       "6061-t6",
    "aluminium 6061":"6061-t6",
    "aluminum 6061": "6061-t6",
    "aluminium_6061":"6061-t6",
    "aluminum":      "6061-t6",
    "al":            "6061-t6",
    "aluminium":     "6061-t6",
    "7075":          "7075-t6",
    "5052":          "5052-h32",
    "stainless":     "316l",
    "ss":            "316l",
    "304":           "304ss",
    "316":           "316l",
    "steel":         "1018",
    "mild steel":    "a36",
    "carbon steel":  "a105",
    "titanium":      "6061-t6",  # fallback (no Ti in table yet)
}


def material_properties(name: str) -> dict:
    """Look up material properties. Accepts various aliases. Returns a
    dict with yield_mpa, uts_mpa, density_kgm3, e_gpa, etc. + callout
    string ready to drop into a drawing's material block."""
    if not name:
        return _MATERIALS["6061-t6"]   # default
    key = name.lower().strip()
    if key in _MATERIALS:
        return _MATERIALS[key]
    if key in _ALIASES:
        return _MATERIALS[_ALIASES[key]]
    # Partial match: "6061-t6 extruded" → "6061-t6"
    for mat_key in _MATERIALS:
        if mat_key in key or key in mat_key:
            return _MATERIALS[mat_key]
    # Unknown — default to 6061 but flag
    return {**_MATERIALS["6061-t6"], "unrecognized_input": name}


def resolve_material(spec: dict, prompt: str = "") -> dict:
    """Pick the material given a spec dict (may have 'material' key)
    and/or the raw prompt text. Returns the full properties dict."""
    name = (spec or {}).get("material") or ""
    if not name and prompt:
        # Scan for known aliases in the prompt
        p = prompt.lower()
        for alias in sorted(_ALIASES, key=len, reverse=True):
            if alias in p:
                return _MATERIALS[_ALIASES[alias]]
        for mat_key in _MATERIALS:
            if mat_key in p:
                return _MATERIALS[mat_key]
    return material_properties(name)
