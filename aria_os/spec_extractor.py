"""
aria_os/spec_extractor.py

Structured spec extraction: converts a natural-language part description into
a typed dict of dimensional parameters before anything reaches a generator or
router.  Explicit dimensions in the description are NEVER passed as raw text to
downstream code — they are extracted here and stored in a canonical spec dict.

Returned dict keys (all optional; only present when the pattern matches):
    od_mm          : float  — outer diameter
    bore_mm        : float  — bore / inner diameter
    id_mm          : float  — alias for bore_mm (always same value)
    thickness_mm   : float  — axial thickness / height
    height_mm      : float  — alias for thickness_mm
    width_mm       : float  — planar width
    depth_mm       : float  — planar depth
    length_mm      : float  — total length
    diameter_mm    : float  — generic diameter (if not clearly OD or bore)
    n_teeth        : int    — tooth count
    n_bolts        : int    — bolt-hole count
    bolt_circle_r_mm : float — bolt-circle PCD/2
    bolt_dia_mm    : float  — individual bolt diameter
    wall_mm        : float  — wall thickness
    material       : str    — material hint ("aluminium", "steel", "titanium", …)
    part_type      : str    — inferred part type from keywords
"""
from __future__ import annotations

import re
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Part-type keyword map (longest match wins)
# ---------------------------------------------------------------------------

_PART_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("phone case",    "phone_case"),   # protective phone/device case
    ("iphone case",   "phone_case"),
    ("device case",   "phone_case"),
    ("protective case","phone_case"),
    ("drop proof case","phone_case"),
    ("drop-proof case","phone_case"),
    ("baseplate",      "base_plate"),   # skateboard/mounting baseplate
    ("base plate",    "base_plate"),   # flat mounting plate
    ("truck baseplate","base_plate"),
    ("mount plate",   "base_plate"),
    ("mounting plate","base_plate"),
    ("face plate",    "base_plate"),
    ("torch bracket", "base_plate"),   # welding torch mount is a flat plate with bore
    ("torch mount",   "base_plate"),
    ("arm link",      "hollow_rect"),  # structural arm link → hollow rectangular tube
    ("ratchet ring",  "ratchet_ring"),
    ("gear wheel",    "gear"),
    ("gear train",    "gear"),
    ("spur gear",     "gear"),
    ("clock gear",    "gear"),
    ("click wheel",   "gear"),
    ("escapement wheel","escape_wheel"),
    ("escape wheel",  "escape_wheel"),
    ("escapement",    "escape_wheel"),
    ("hour wheel",    "gear"),
    ("minute wheel",  "gear"),
    ("cannon pinion", "gear"),
    ("brake drum",    "brake_drum"),
    ("cam collar",    "cam_collar"),
    ("rope guide",    "rope_guide"),
    ("catch pawl",    "catch_pawl"),
    ("barrel drum",   "brake_drum"),
    ("barrel cap",    "spacer"),
    ("pallet fork",   "catch_pawl"),
    ("pendulum bob",  "flange"),
    ("pendulum rod",  "shaft"),
    ("dial ring",     "ratchet_ring"),
    ("hour hand",     "pin"),
    ("minute hand",   "pin"),
    ("seconds hand",  "pin"),
    ("ratchet",       "ratchet_ring"),
    ("pulley",        "pulley"),
    ("flange",        "flange"),
    ("spacer",        "spacer"),
    ("bracket",       "bracket"),
    ("housing",       "housing"),
    ("spool",         "spool"),
    ("link",          "hollow_rect"),  # generic link → hollow rect tube
    ("shaft",         "shaft"),
    ("collar",        "cam_collar"),
    ("pawl",          "catch_pawl"),
    ("nozzle",        "lre_nozzle"),
    ("rocket",        "lre_nozzle"),
    ("drum",          "brake_drum"),
    ("guide",         "rope_guide"),
    ("gear",          "gear"),
    ("pinion",        "gear"),
    ("ring",          "ratchet_ring"),
    ("cam",           "cam"),
    ("pin",           "pin"),
    ("pillar",        "spacer"),
]

# Sorted descending by keyword length so multi-word phrases always beat single words.
_PART_TYPE_KEYWORDS = sorted(_PART_TYPE_KEYWORDS, key=lambda t: len(t[0]), reverse=True)

_MATERIAL_KEYWORDS: list[tuple[str, str]] = [
    ("6061",      "aluminium_6061"),
    ("7075",      "aluminium_7075"),
    ("stainless", "stainless_steel"),
    ("aluminium", "aluminium"),
    ("aluminum",  "aluminium"),
    ("steel",     "steel"),
    ("titanium",  "titanium"),
    ("nylon",     "nylon"),
    ("pla",       "pla"),
    ("petg",      "petg"),
    ("carbon",    "carbon_fibre"),
]


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_spec(description: str) -> dict[str, Any]:
    """
    Parse dimensional parameters from a natural-language description string.

    Parameters
    ----------
    description : str
        Free-text description such as:
            "ARIA ratchet ring, 213mm OD, 185mm bore, 21mm thick, 24 teeth"

    Returns
    -------
    dict[str, Any]
        Structured spec; only keys with found values are included.
        Always includes ``part_type`` if a keyword is recognised.

    Examples
    --------
    >>> extract_spec("ratchet ring 213mm OD 185mm bore 21mm thick 24 teeth")
    {'od_mm': 213.0, 'bore_mm': 185.0, 'thickness_mm': 21.0, 'n_teeth': 24,
     'part_type': 'ratchet_ring'}
    """
    spec: dict[str, Any] = {}
    text = description.strip()
    lower = text.lower()

    # --- Part type (longest match first, word-boundary aware) ---
    for kw, ptype in _PART_TYPE_KEYWORDS:
        # Use word boundaries so "cam" doesn't match inside "cam_collar" etc.
        if re.search(r"\b" + re.escape(kw) + r"\b", lower):
            spec["part_type"] = ptype
            break

    # --- Material ---
    for kw, mat in _MATERIAL_KEYWORDS:
        if kw in lower:
            spec["material"] = mat
            break

    def _find(patterns: list[str]) -> Optional[float]:
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    return float(m.group(1))
                except (TypeError, ValueError):
                    pass
        return None

    def _find_int(patterns: list[str]) -> Optional[int]:
        v = _find(patterns)
        return int(v) if v is not None else None

    # --- Outer diameter ---
    od = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+od\b",
        r"\bod\s*[=:\s]\s*(\d+(?:\.\d+)?)\s*mm",   # "OD 50mm", "OD: 50mm", "OD=50mm"
        r"\bod\s*[=:]\s*(\d+(?:\.\d+)?)",           # "OD: 50" (no unit)
        r"(\d+(?:\.\d+)?)\s*mm\s+outer\s+diameter",
        r"(\d+(?:\.\d+)?)\s*mm\s+outer\b",          # "50mm outer"
        r"outer\s+diameter\s*[=:]?\s*(\d+(?:\.\d+)?)\s*mm",
        r"outer\s+dia(?:meter)?\s+(\d+(?:\.\d+)?)\s*mm",   # "outer dia 50mm"
        r"(\d+(?:\.\d+)?)\s*mm\s+diameter(?!\s*bore)",
        r"diameter\s+of\s+(\d+(?:\.\d+)?)\s*mm",    # "diameter of 50mm"
    ])
    if od:
        spec["od_mm"] = od

    # --- Bore / inner diameter ---
    bore = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+(?:\w+\s+)?bore\b",  # "120mm center bore", "120mm bore"
        r"\bbore\s*[=:\s]\s*(\d+(?:\.\d+)?)\s*mm",  # "bore 50mm", "bore: 50mm"
        r"\bbore\s*[=:]\s*(\d+(?:\.\d+)?)",          # "bore: 50" (no unit)
        r"(\d+(?:\.\d+)?)\s*mm\s+id\b",
        r"\bid\s*[=:]\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*mm\s+inner\s+diameter",
        r"inner\s+diameter\s*[=:]?\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if bore:
        spec["bore_mm"] = bore
        spec["id_mm"]   = bore

    # --- Thickness / height ---
    thick = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+thick(?:ness)?",
        r"thickness\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        r"(\d+(?:\.\d+)?)\s*mm\s+tall",
        r"(\d+(?:\.\d+)?)\s*mm\s+high(?:t)?",
        r"(\d+(?:\.\d+)?)\s*mm\s+height\b",         # "120mm height"
        r"height\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if thick:
        spec["thickness_mm"] = thick
        spec["height_mm"]    = thick

    # --- Width ---
    width = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+wide",
        r"width\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        r"(\d+(?:\.\d+)?)\s*mm\s+width",
    ])
    if width:
        spec["width_mm"] = width

    # --- Depth ---
    depth = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+deep",
        r"depth\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        r"(\d+(?:\.\d+)?)\s*mm\s+depth",
    ])
    if depth:
        spec["depth_mm"] = depth

    # --- Length ---
    length = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+long",
        r"length\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        r"(\d+(?:\.\d+)?)\s*mm\s+length",
    ])
    if length:
        spec["length_mm"] = length

    # --- Generic diameter (only if OD not found) ---
    if "od_mm" not in spec:
        dia = _find([
            r"(\d+(?:\.\d+)?)\s*mm\s+diameter",
            r"diameter\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
            r"(\d+(?:\.\d+)?)\s*mm\s+dia\b",
        ])
        if dia:
            spec["diameter_mm"] = dia

    # --- Gear module (metric) ---
    module = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+module",      # "1.5mm module"
        r"module\s*[=:\s]\s*(\d+(?:\.\d+)?)\s*mm",  # "module 1.5mm", "module=1.5mm"
        r"module\s*[=:]\s*(\d+(?:\.\d+)?)",    # "module=1.5" (no unit)
        r"\bm\s*=\s*(\d+(?:\.\d+)?)\s*mm",    # "m=1.5mm"
    ])
    if module:
        spec["module_mm"] = module

    # --- Teeth ---
    n_teeth = _find_int([
        r"(\d+)\s+teeth",
        r"(\d+)-tooth",
        r"teeth\s*[=:]\s*(\d+)",
        r"tooth\s+count\s*[=:]\s*(\d+)",
    ])
    if n_teeth:
        spec["n_teeth"] = n_teeth

    # --- WxHxD box notation ---
    # Matches: "50x100x200mm", "50 x 100 x 200 mm", "160.8mm x 78.1mm x 12mm"
    # Always overrides single-value prose extractions.
    _box_m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*mm",
        text, re.I,
    )
    if _box_m:
        spec["width_mm"]  = float(_box_m.group(1))
        spec["height_mm"] = float(_box_m.group(2))
        spec["depth_mm"]  = float(_box_m.group(3))

    # --- 2D WxH box notation (e.g. "200x200mm" square plate, "100x60mm" rectangle) ---
    # Only runs when the 3D pattern didn't already fire
    if "width_mm" not in spec or "depth_mm" not in spec:
        _box2_m = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*mm(?!\s*[xX×])",
            text, re.I,
        )
        if _box2_m:
            spec.setdefault("width_mm",  float(_box2_m.group(1)))
            spec.setdefault("depth_mm",  float(_box2_m.group(2)))

    # --- Radius → diameter (only when OD not yet found) ---
    if "od_mm" not in spec and "diameter_mm" not in spec:
        _rad = _find([
            r"radius\s*(?:of\s*)?[=:]?\s*(\d+(?:\.\d+)?)\s*mm",
            r"(\d+(?:\.\d+)?)\s*mm\s+radius\b",
            r"\br\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        ])
        if _rad:
            spec["diameter_mm"] = round(_rad * 2.0, 4)

    # --- Bolt holes ---
    # Combined "NxMsize" shorthand: "4xM8", "4 x M8" → n_bolts=4, bolt_dia=8
    _bolt_combo = re.search(r"(\d+)\s*[xX×]\s*[mM](\d+)", text, re.I)
    if _bolt_combo:
        spec.setdefault("n_bolts", int(_bolt_combo.group(1)))
        spec.setdefault("bolt_dia_mm", float(_bolt_combo.group(2)))

    n_bolts = _find_int([
        r"(\d+)\s*[xX]\s*[mM]\d+\s+bolt",
        r"(\d+)\s+[mM]\d+\s+bolt",                 # "4 M8 bolt"
        r"(\d+)\s+bolt[s\s]",
        r"(\d+)-bolt",
        r"bolt[s]?\s*[=:]\s*(\d+)",
        r"(\d+)\s+holes?\b",                        # "4 holes"
    ])
    if n_bolts and "n_bolts" not in spec:
        spec["n_bolts"] = n_bolts

    # "bolt circle 100mm radius" — value IS already a radius
    _bc_rad = re.search(r"bolt\s+circle\s+(\d+(?:\.\d+)?)\s*mm\s+radius\b", text, re.I)
    if _bc_rad:
        spec.setdefault("bolt_circle_r_mm", float(_bc_rad.group(1)))

    if "bolt_circle_r_mm" not in spec:
        bolt_pcd = _find([
            r"pcd\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
            r"bolt\s+circle\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
            r"bolt\s+circle\s+(\d+(?:\.\d+)?)\s*mm",  # "bolt circle 100mm" (diameter)
            r"(\d+(?:\.\d+)?)\s*mm\s+pcd",
            r"(\d+(?:\.\d+)?)\s*mm\s+bolt\s+circle",
        ])
        if bolt_pcd:
            spec["bolt_circle_r_mm"] = bolt_pcd / 2.0

    # --- Square bolt pattern (e.g. "160mm square" → bolts at corners of 160mm square)
    # Corner-to-centre radius = side/2 * sqrt(2)
    # Reject "Nmm square" when followed by a number (e.g. "86mm square 10mm deep" = NEMA pocket)
    _sq_bolt = re.search(
        r"(\d+(?:\.\d+)?)\s*mm\s+(?:bolt\s+)?square\b(?!\s+\d)",
        text, re.I,
    )
    if _sq_bolt and "bolt_circle_r_mm" not in spec:
        side = float(_sq_bolt.group(1))
        import math as _math
        spec["bolt_circle_r_mm"] = round(side / 2.0 * _math.sqrt(2), 2)
        spec["bolt_square_mm"]   = side   # keep raw side length for LLM context

    bolt_dia = _find([
        r"[mM](\d+)\s+bolt",        # M8 bolt → 8.0
        r"(\d+(?:\.\d+)?)\s*mm\s+bolt\s+diameter",
    ])
    if bolt_dia:
        spec["bolt_dia_mm"] = bolt_dia

    # --- Wall thickness ---
    wall = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+wall",
        r"wall\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        r"wall\s+thickness\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if wall:
        spec["wall_mm"] = wall

    return spec


def merge_spec_into_plan(spec: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """
    Merge extracted spec into plan.params without overwriting existing explicit values.
    Returns the updated plan dict (mutates in place and returns).
    """
    params = plan.setdefault("params", {})
    for key, val in spec.items():
        if key not in params or params[key] is None:
            params[key] = val
    return plan
