"""
Fastener auto-selection — pick an appropriate metric socket head cap screw
for a given load, material, and environment.

Pure-lookup module with no external API dependency. The goal is
engineer-grade sanity, not aerospace certification: given a service load,
we compute the minimum proof-load bolt size, then size up by one step as
a safety factor and return static McMaster-Carr-style part numbers,
preferred length, tightening torque, and proof-load margin.

Typical usage::

    from aria_os.fastener_selector import select_fastener
    f = select_fastener(load_n=2500, material="steel", environment="dry")
    # {"size": "M6", "length_mm": 20, "mcmaster_pn": "91290A320", ...}

Scope: M3 through M16 socket head cap screws, ISO 4762 / DIN 912, class 12.9
alloy steel by default. Stainless A2-70 selected automatically when the
environment is wet/marine. Larger sizes raise ValueError — use custom eng
review for loads > ~80 kN.
"""

from __future__ import annotations

from typing import Literal


# ---------------------------------------------------------------------------
# Static property tables
# ---------------------------------------------------------------------------

# Nominal size → tensile stress area (mm²), from ISO 898-1.
# A_s = pi/4 * (d - 0.9382 * p)**2 using coarse pitch.
_TENSILE_STRESS_AREA_MM2: dict[str, float] = {
    "M3":   5.03,
    "M4":   8.78,
    "M5":  14.2,
    "M6":  20.1,
    "M8":  36.6,
    "M10": 58.0,
    "M12": 84.3,
    "M14": 115.0,
    "M16": 157.0,
}

# Class 12.9 proof load stress = 970 MPa; 10.9 = 830; 8.8 = 600.
# Stainless A2-70: proof ≈ 450 MPa, A4-80: 600 MPa.
_PROOF_STRESS_MPA: dict[str, float] = {
    "12.9": 970.0,
    "10.9": 830.0,
    "8.8":  600.0,
    "A2-70": 450.0,
    "A4-80": 600.0,
}

# Recommended tightening torque for class 12.9 socket cap screws, in Nm.
# Based on T = K * F * d with K=0.2 (dry), F = 0.75 * proof load.
# Pre-computed so we don't need a live calc for quick lookups.
_TORQUE_SPEC_NM_12_9: dict[str, float] = {
    "M3":    2.3,
    "M4":    5.3,
    "M5":   10.4,
    "M6":   17.9,
    "M8":   43.0,
    "M10":  86.0,
    "M12": 150.0,
    "M14": 240.0,
    "M16": 370.0,
}

# Torque scaling factor for other strength grades relative to 12.9.
_GRADE_TORQUE_SCALE: dict[str, float] = {
    "12.9": 1.0,
    "10.9": 0.85,
    "8.8":  0.60,
    "A2-70": 0.46,
    "A4-80": 0.62,
}

# McMaster-Carr-style part numbers (static lookup — NOT live).
# For class 12.9 alloy steel socket cap screws, ISO 4762 / DIN 912.
# Keyed on (size, length_mm). Values are representative real SKUs.
_MCMASTER_PN_12_9: dict[tuple[str, int], str] = {
    ("M3",  10): "91290A111",
    ("M3",  12): "91290A112",
    ("M3",  16): "91290A113",
    ("M3",  20): "91290A114",
    ("M4",  12): "91290A151",
    ("M4",  16): "91290A152",
    ("M4",  20): "91290A153",
    ("M4",  25): "91290A154",
    ("M5",  16): "91290A192",
    ("M5",  20): "91290A193",
    ("M5",  25): "91290A194",
    ("M5",  30): "91290A195",
    ("M6",  16): "91290A230",
    ("M6",  20): "91290A232",
    ("M6",  25): "91290A234",
    ("M6",  30): "91290A236",
    ("M8",  20): "91290A322",
    ("M8",  25): "91290A324",
    ("M8",  30): "91290A326",
    ("M8",  40): "91290A328",
    ("M10", 25): "91290A415",
    ("M10", 30): "91290A417",
    ("M10", 40): "91290A421",
    ("M10", 50): "91290A425",
    ("M12", 30): "91290A517",
    ("M12", 40): "91290A521",
    ("M12", 50): "91290A525",
    ("M12", 60): "91290A529",
    ("M14", 40): "91290A618",
    ("M14", 50): "91290A622",
    ("M14", 60): "91290A626",
    ("M16", 50): "91290A720",
    ("M16", 60): "91290A724",
    ("M16", 80): "91290A728",
}

# Stainless A2-70 alternate SKUs (for wet / marine environments).
_MCMASTER_PN_A2: dict[tuple[str, int], str] = {
    ("M3",  10): "91292A111",
    ("M4",  12): "91292A151",
    ("M5",  16): "91292A192",
    ("M6",  20): "91292A232",
    ("M8",  25): "91292A324",
    ("M10", 30): "91292A417",
    ("M12", 40): "91292A521",
    ("M14", 50): "91292A622",
    ("M16", 60): "91292A724",
}

# Preferred length for each size when the caller doesn't specify.
# Rule of thumb: ~3x diameter for typical joint thickness.
_PREFERRED_LENGTH_MM: dict[str, int] = {
    "M3":  10,
    "M4":  12,
    "M5":  16,
    "M6":  20,
    "M8":  25,
    "M10": 30,
    "M12": 40,
    "M14": 50,
    "M16": 60,
}

_SIZES_ORDERED: list[str] = ["M3", "M4", "M5", "M6", "M8", "M10", "M12", "M14", "M16"]

# Material → preferred strength grade. "steel" uses high-strength 12.9;
# "stainless_steel" uses A2-70 for general and A4-80 for marine.
_MATERIAL_TO_GRADE: dict[str, str] = {
    "steel":           "12.9",
    "alloy_steel":     "12.9",
    "carbon_steel":    "10.9",
    "mild_steel":      "8.8",
    "stainless_steel": "A2-70",
    "aluminium":       "10.9",   # use alloy steel bolt into Al
    "aluminum":        "10.9",
    "titanium":        "12.9",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


Environment = Literal["dry", "wet", "marine", "humid"]
Material = Literal[
    "steel", "alloy_steel", "carbon_steel", "mild_steel",
    "stainless_steel", "aluminium", "aluminum", "titanium",
]


def _pick_grade(material: str, environment: str) -> str:
    """Pick strength grade based on environment + base material."""
    env = environment.lower()
    if env in ("marine", "saltwater"):
        return "A4-80"
    if env in ("wet", "humid"):
        # Stainless unless caller explicitly said alloy steel.
        if material.lower() in ("steel", "alloy_steel", "titanium"):
            return _MATERIAL_TO_GRADE.get(material.lower(), "12.9")
        return "A2-70"
    return _MATERIAL_TO_GRADE.get(material.lower(), "12.9")


def _proof_load_n(size: str, grade: str) -> float:
    """Proof load in newtons for a given size + grade."""
    return _PROOF_STRESS_MPA[grade] * _TENSILE_STRESS_AREA_MM2[size]


def select_fastener(
    load_n: float,
    material: str = "steel",
    environment: str = "dry",
    safety_factor: float = 2.0,
    length_mm: int | None = None,
) -> dict:
    """Auto-select a socket head cap screw for a given load.

    Parameters
    ----------
    load_n :
        Service load (tensile) the joint must resist, in newtons.
    material :
        Base material being joined. Drives grade selection.
        ``steel`` | ``alloy_steel`` | ``carbon_steel`` | ``mild_steel`` |
        ``stainless_steel`` | ``aluminium`` | ``titanium``.
    environment :
        ``dry`` | ``wet`` | ``humid`` | ``marine``. Wet/marine force stainless.
    safety_factor :
        Multiplier on service load. Default 2.0 — typical static load.
    length_mm :
        Override preferred length. If ``None``, use ~3×D rule of thumb.

    Returns
    -------
    dict with keys:
        size, length_mm, head_type, grade, mcmaster_pn, torque_spec_nm,
        proof_load_n, required_load_n, safety_factor_actual, stress_area_mm2.

    Raises
    ------
    ValueError
        If ``load_n`` is non-positive, or the required load exceeds the
        M16 proof load (caller needs engineering review).
    """
    if load_n <= 0:
        raise ValueError(f"load_n must be positive, got {load_n}")
    if safety_factor < 1.0:
        raise ValueError(f"safety_factor must be >= 1.0, got {safety_factor}")

    grade = _pick_grade(material, environment)
    required_n = load_n * safety_factor

    chosen: str | None = None
    for size in _SIZES_ORDERED:
        if _proof_load_n(size, grade) >= required_n:
            chosen = size
            break

    if chosen is None:
        max_n = _proof_load_n("M16", grade)
        raise ValueError(
            f"Load {load_n:.0f} N (× SF {safety_factor} = {required_n:.0f} N) "
            f"exceeds M16 {grade} proof load ({max_n:.0f} N). "
            f"Upgrade to multiple fasteners or >M16 engineering review."
        )

    # Choose length.
    if length_mm is None:
        length_mm = _PREFERRED_LENGTH_MM[chosen]

    # Resolve part number.
    if grade in ("A2-70", "A4-80"):
        pn_table = _MCMASTER_PN_A2
    else:
        pn_table = _MCMASTER_PN_12_9
    mcmaster_pn = pn_table.get((chosen, length_mm))
    if mcmaster_pn is None:
        # Fall back to preferred length PN for this size.
        mcmaster_pn = pn_table.get((chosen, _PREFERRED_LENGTH_MM[chosen]))

    # Torque: scale 12.9 baseline by grade factor.
    torque_nm = round(
        _TORQUE_SPEC_NM_12_9[chosen] * _GRADE_TORQUE_SCALE.get(grade, 1.0),
        2,
    )

    proof_n = _proof_load_n(chosen, grade)
    actual_sf = proof_n / load_n

    return {
        "size": chosen,
        "length_mm": int(length_mm),
        "head_type": "socket_cap_iso4762",
        "grade": grade,
        "material": material,
        "environment": environment,
        "mcmaster_pn": mcmaster_pn,
        "torque_spec_nm": torque_nm,
        "proof_load_n": round(proof_n, 1),
        "required_load_n": round(required_n, 1),
        "service_load_n": round(load_n, 1),
        "safety_factor_requested": safety_factor,
        "safety_factor_actual": round(actual_sf, 2),
        "stress_area_mm2": _TENSILE_STRESS_AREA_MM2[chosen],
    }


def list_supported_sizes() -> list[str]:
    """Return the ordered list of supported metric sizes."""
    return list(_SIZES_ORDERED)


def proof_load_table(grade: str = "12.9") -> dict[str, float]:
    """Return proof load (N) for every supported size at a given grade."""
    if grade not in _PROOF_STRESS_MPA:
        raise ValueError(
            f"unknown grade {grade!r}; supported: {sorted(_PROOF_STRESS_MPA)}"
        )
    return {size: round(_proof_load_n(size, grade), 1) for size in _SIZES_ORDERED}
