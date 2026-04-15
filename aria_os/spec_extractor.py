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
    ("motor mount",   "flange"),       # circular plate + bolt holes + center bore
    ("servo mount",   "flange"),
    ("stepper mount", "flange"),
    ("nema mount",    "flange"),
    ("nema 17",       "flange"),   # NEMA 17 motor mount — auto-fills bolt circle
    ("nema 23",       "flange"),
    ("nema 34",       "flange"),
    ("nema17",        "flange"),
    ("nema23",        "flange"),
    ("nema34",        "flange"),
    ("adapter plate", "flange"),
    ("hex standoff",  "hex_standoff"),
    ("standoff",      "spacer"),
    ("cable clamp",   "clamp"),
    ("pipe clamp",    "clamp"),
    ("tube clamp",    "clamp"),
    ("c-clamp",       "clamp"),
    ("c clamp",       "clamp"),
    ("snap hook",     "snap_hook"),
    ("snap-fit hook", "snap_hook"),
    ("snap fit hook", "snap_hook"),
    ("snap clip",     "snap_hook"),
    ("snap-fit clip", "snap_hook"),
    ("snap fit",      "snap_hook"),
    ("threaded insert","thread_insert"),
    ("thread insert", "thread_insert"),
    ("knurled insert","thread_insert"),
    ("heat set insert","thread_insert"),
    ("heat-set insert","thread_insert"),
    ("door hinge",    "hinge"),
    ("butt hinge",    "hinge"),
    ("enclosure lid", "enclosure_lid"),
    ("box lid",       "enclosure_lid"),
    ("snap lid",      "enclosure_lid"),
    ("gusset plate",  "gusset"),
    ("corner brace",  "gusset"),
    ("corner bracket","gusset"),
    ("triangle brace","gusset"),
    ("spoked wheel",  "spoked_wheel"),
    ("handwheel",     "spoked_wheel"),
    ("hand wheel",    "spoked_wheel"),
    ("steering wheel","spoked_wheel"),
    ("t-slot plate",  "t_slot_plate"),
    ("t slot plate",  "t_slot_plate"),
    ("fixture plate", "t_slot_plate"),
    ("tooling plate", "t_slot_plate"),
    ("spring clip",   "spring_clip"),
    ("retaining clip","spring_clip"),
    ("circlip",       "spring_clip"),
    ("clamp",         "clamp"),
    ("fixture",       "bracket"),
    ("holder",        "bracket"),
    ("manifold",      "housing"),
    ("cover",         "flat_plate"),
    ("lid",           "enclosure_lid"),
    ("platform",      "flat_plate"),
    ("l-bracket",     "l_bracket"),
    ("l bracket",     "l_bracket"),
    ("angle bracket", "l_bracket"),
    ("heat sink",     "heat_sink"),
    ("heatsink",      "heat_sink"),
    ("phone stand",   "phone_stand"),
    ("tablet stand",  "phone_stand"),
    ("gopro mount adapter", "gopro_mount"),
    ("gopro mount",   "gopro_mount"),
    ("action camera mount", "gopro_mount"),
    ("camera mount adapter", "gopro_mount"),
    ("arm link",      "hollow_rect"),  # structural arm link → hollow rectangular tube
    ("involute gear", "involute_gear"),
    ("involute spur gear", "involute_gear"),
    ("cam profile",   "cam_profile"),
    ("eccentric cam", "cam_profile"),
    ("cam disc",      "cam_profile"),
    ("bellows",       "bellows"),
    ("corrugated bellows", "bellows"),
    ("compression spring", "compression_spring"),
    ("coil spring",   "compression_spring"),
    ("helical spring","compression_spring"),
    ("keyway shaft",  "keyway_shaft"),
    ("keyed shaft",   "keyway_shaft"),
    ("dovetail joint","dovetail_joint"),
    ("dovetail slide","dovetail_joint"),
    ("dovetail rail", "dovetail_joint"),
    ("dovetail",      "dovetail_joint"),
    ("slot plate",    "slot_plate"),
    ("slotted plate", "slot_plate"),
    ("pcb enclosure", "pcb_enclosure"),
    ("pcb box",       "pcb_enclosure"),
    ("electronics enclosure", "pcb_enclosure"),
    ("electronics box","pcb_enclosure"),
    ("pillow block",  "bearing_pillow_block"),
    ("plummer block", "bearing_pillow_block"),
    ("bearing pillow block", "bearing_pillow_block"),
    ("cable gland",   "cable_gland"),
    ("strain relief", "cable_gland"),
    ("cord grip",     "cable_gland"),
    # Expansion (April 2026) — fasteners, drivetrain, pipe fittings, misc
    ("hex head bolt", "hex_bolt"),
    ("hex bolt",      "hex_bolt"),
    ("hex nut",       "hex_nut"),
    ("socket cap screw", "socket_cap_screw"),
    ("socket head cap screw", "socket_cap_screw"),
    ("cap screw",     "socket_cap_screw"),
    ("set screw",     "set_screw"),
    ("grub screw",    "set_screw"),
    ("timing pulley", "timing_pulley"),
    ("gt2 pulley",    "timing_pulley"),
    ("htd pulley",    "timing_pulley"),
    ("belt pulley",   "timing_pulley"),
    ("chain sprocket","sprocket"),
    ("rack gear",     "rack"),
    ("gear rack",     "rack"),
    ("linear rack",   "rack"),
    ("pipe elbow",    "pipe_elbow"),
    ("pipe bend",     "pipe_elbow"),
    ("pipe tee",      "pipe_tee"),
    ("tee fitting",   "pipe_tee"),
    ("pipe reducer",  "pipe_reducer"),
    ("concentric reducer", "pipe_reducer"),
    ("ring gear",     "ring_gear"),
    ("internal gear", "ring_gear"),
    ("annular gear",  "ring_gear"),
    ("hex standoff",  "hex_standoff"),
    ("pcb standoff",  "hex_standoff"),
    ("t-slot nut",    "t_nut"),
    ("t slot nut",    "t_nut"),
    ("t nut",         "t_nut"),
    ("thrust washer", "thrust_washer"),
    ("thrust bearing","thrust_washer"),
    ("retaining ring","retaining_ring"),
    ("snap ring",     "retaining_ring"),
    ("e-clip",        "retaining_ring"),
    ("linear bushing","linear_bushing"),
    ("plain bushing", "linear_bushing"),
    ("sliding bushing","linear_bushing"),
    ("pipe cap",      "pipe_cap"),
    ("end cap",       "pipe_cap"),
    ("cross dowel",   "cross_dowel"),
    ("barrel nut",    "cross_dowel"),
    ("bevel gear",    "bevel_gear"),
    ("miter gear",    "bevel_gear"),
    ("worm gear",     "worm_gear"),
    ("worm wheel",    "worm_gear"),
    ("worm shaft",    "worm"),
    ("timing belt",   "timing_belt"),
    ("gt2 belt",      "timing_belt"),
    ("htd belt",      "timing_belt"),
    ("jaw coupling",  "jaw_coupling"),
    ("spider coupling","jaw_coupling"),
    ("valve body",    "valve_body"),
    ("globe valve",   "valve_body"),
    ("ball valve",    "valve_body"),
    ("extension spring","extension_spring"),
    ("tension spring","extension_spring"),
    ("torsion spring","torsion_spring"),
    ("wave spring",   "wave_spring"),
    ("disc spring",   "wave_spring"),
    ("pipe cross",    "pipe_cross"),
    ("cross fitting", "pipe_cross"),
    ("orifice plate", "orifice_plate"),
    ("blind rivet",   "rivet"),
    ("pop rivet",     "rivet"),
    ("ratchet ring",  "ratchet_ring"),
    ("gear carrier",  "flange"),     # planetary carrier = disc with pin holes
    ("planet carrier","flange"),
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
    ("hinge",         "hinge"),
    ("handle",        "handle"),
    ("grip",          "handle"),
    ("knob",          "handle"),
    ("gusset",        "gusset"),
    ("insert",        "thread_insert"),
    ("retainer",      "spring_clip"),
    ("bolt",          "hex_bolt"),
    ("nut",           "hex_nut"),
    ("screw",         "socket_cap_screw"),
    ("sprocket",      "sprocket"),
    ("rack",          "rack"),
    ("elbow",         "pipe_elbow"),
    ("tee",           "pipe_tee"),
    ("reducer",       "pipe_reducer"),
    ("rivet",         "rivet"),
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
                    val = float(m.group(1))
                    if val > 0:
                        return val
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

    # --- Thickness (wall / sheet thickness — small dimension) ---
    # Note: previously this regex group also matched "tall"/"height" and
    # mirrored the value into both thickness_mm AND height_mm. That broke
    # phrases like "60mm tall, 8mm thick" — _find returned the first match
    # (8) and dropped the height entirely. Now thickness and height are
    # extracted independently. Reported by feature verification 2026-04-15.
    thick = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+thick(?:ness)?",
        r"thickness\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if thick:
        spec["thickness_mm"] = thick

    # --- Height / tallness — separate from thickness ---
    height = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+tall",
        r"(\d+(?:\.\d+)?)\s*mm\s+high(?:t)?",
        r"(\d+(?:\.\d+)?)\s*mm\s+height\b",
        r"height\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if height:
        spec["height_mm"] = height
    elif thick is not None:
        # Backwards compat: older templates expect height_mm to fall back
        # to thickness when no explicit height is present (e.g. flat plates).
        spec["height_mm"] = thick

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
        r"(\d+(?:\.\d+)?)\s*mm\s+module",          # "1.5mm module"
        r"module\s*[=:\s]\s*(\d+(?:\.\d+)?)\s*mm", # "module 1.5mm", "module=1.5mm"
        r"module\s*[=:]\s*(\d+(?:\.\d+)?)",         # "module=1.5" (no unit)
        r"\bmodule\s+(\d+(?:\.\d+)?)\b",            # "module 1.5" (plain space, no unit)
        r"\bm\s*=\s*(\d+(?:\.\d+)?)\s*mm",         # "m=1.5mm"
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
        r"(\d+)\s*[xX]\s+\w+\s+\w*\s*holes?\b",   # "3x planet pin holes"
        r"(\d+)\s*[xX]\s+\w+\s+holes?\b",          # "3x mounting holes"
    ])
    if n_bolts and "n_bolts" not in spec:
        spec["n_bolts"] = n_bolts

    # "bolt circle 100mm radius" — value IS already a radius
    _bc_rad = re.search(r"bolt\s+circle\s+(\d+(?:\.\d+)?)\s*mm\s+radius\b", text, re.I)
    if _bc_rad:
        spec.setdefault("bolt_circle_r_mm", float(_bc_rad.group(1)))

    # "bolts at 16mm" / "holes at 16mm" — value IS a radius (distance from center)
    if "bolt_circle_r_mm" not in spec:
        _at_rad = re.search(r"(?:bolt|hole)s?\s+at\s+(\d+(?:\.\d+)?)\s*mm", text, re.I)
        if _at_rad:
            spec["bolt_circle_r_mm"] = float(_at_rad.group(1))

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
        r"[mM](\d+)\s+bolt",             # M8 bolt → 8.0
        r"[mM](\d+)\s+standoff",         # M4 standoff → 4.0
        r"[mM](\d+)\s+screw",            # M3 screw → 3.0
        r"[mM](\d+)\s+thread",           # M5 thread → 5.0
        r"[mM](\d+)\s+fastener",         # M6 fastener → 6.0
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

    # --- Fin / feature dimensions (heat sinks, etc.) ---
    fin_h = _find([
        r"fins?\s+(\d+(?:\.\d+)?)\s*mm\s+tall",
        r"(\d+(?:\.\d+)?)\s*mm\s+tall\s+fins?",
        r"fin\s+height\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if fin_h:
        spec["fin_height_mm"] = fin_h

    fin_t = _find([
        r"fins?\s+(\d+(?:\.\d+)?)\s*mm\s+thick",
        r"(\d+(?:\.\d+)?)\s*mm\s+thick\s+fins?",
        r"fin\s+thickness\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if fin_t:
        spec["fin_thickness_mm"] = fin_t

    fin_spacing = _find([
        r"(\d+(?:\.\d+)?)\s*mm\s+spacing",
        r"spacing\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
    ])
    if fin_spacing:
        spec["fin_spacing_mm"] = fin_spacing

    n_fins = _find([
        r"(\d+)\s+(?:parallel\s+)?fins?",
    ])
    if n_fins:
        spec["n_fins"] = int(n_fins)

    # --- Blades / vanes (impellers, fans, turbines, props) ---
    n_blades = _find_int([
        r"(\d+)[- ]bladed?",                                         # "6-blade", "6-bladed"
        r"(\d+)[- ]vaned?",                                          # "8-vane"
        r"(\d+)\s+(?:backward[- ]curved?|forward[- ]swept?|radial|swept|curved)\s+(?:curved\s+)?(?:blades?|vanes?)",  # "6 backward-curved blades"
        r"(\d+)\s+(?:blades?|vanes?)",                               # "6 blades", "8 vanes"
        r"(?:blades?|vanes?)\s*[=:]\s*(\d+)",                       # "blades=6"
    ])
    if n_blades:
        spec["n_blades"] = n_blades

    # Blade sweep direction — qualitative, drives template choice of angle/orientation
    _SWEEP_KEYWORDS: list[tuple[str, str]] = [
        ("backward-curved",   "backward_curved"),
        ("backward curved",   "backward_curved"),
        ("backward-swept",    "backward_swept"),
        ("backward swept",    "backward_swept"),
        ("forward-curved",    "forward_curved"),
        ("forward curved",    "forward_curved"),
        ("forward-swept",     "forward_swept"),
        ("forward swept",     "forward_swept"),
        ("backward",          "backward"),
        ("forward",           "forward"),
        ("radial",            "radial"),
    ]
    goal_lower = text.lower()
    for _kw, _val in _SWEEP_KEYWORDS:
        if _kw in goal_lower:
            spec["blade_sweep"] = _val
            break

    # --- Spokes / arms (wheels, hand-wheels, spider hubs) ---
    n_spokes = _find_int([
        r"(\d+)[- ]spokes?",
        r"(\d+)\s+spokes?",
        r"(\d+)[- ]armed?\b",                 # "5-arm", "6-armed"
        r"(\d+)\s+arms?\b(?!\s*length)",       # "5 arms" but not "arm length"
    ])
    if n_spokes:
        spec["n_spokes"] = n_spokes

    # --- Blade sweep angle (numeric) ---
    sweep_angle = _find([
        r"sweep\s*(?:angle\s*)?[=:]?\s*(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)",
        r"(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)\s+sweep",
        r"backward[- ]curved?\s+(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)",
        r"forward[- ]swept?\s+(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)",
    ])
    if sweep_angle:
        spec["blade_angle_deg"] = sweep_angle

    # --- Angle ---
    angle = _find([
        r"angled?\s+(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)",
        r"(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)\s+angle",
        r"at\s+(\d+(?:\.\d+)?)\s*(?:deg|degrees?|°)",
    ])
    if angle:
        spec["angle_deg"] = angle

    # --- NEMA motor frame sizes (auto-populate bolt circle + count) ---
    # NEMA 17 = 42mm frame, 4 bolts, 31mm PCD
    # NEMA 23 = 57mm frame, 4 bolts, 47mm PCD
    # NEMA 34 = 86mm frame, 4 bolts, 69.6mm PCD
    _NEMA_SIZES = {
        "nema 17": (42.0, 31.0, 4),
        "nema17":  (42.0, 31.0, 4),
        "nema 23": (57.0, 47.0, 4),
        "nema23":  (57.0, 47.0, 4),
        "nema 34": (86.0, 69.6, 4),
        "nema34":  (86.0, 69.6, 4),
        "nema 8":  (20.3, 16.0, 4),
        "nema8":   (20.3, 16.0, 4),
    }
    for _nema_kw, (_nema_frame, _nema_pcd, _nema_bolts) in _NEMA_SIZES.items():
        if _nema_kw in lower:
            spec.setdefault("od_mm",           _nema_frame)
            spec.setdefault("bolt_circle_r_mm", _nema_pcd / 2.0)
            spec.setdefault("n_bolts",          _nema_bolts)
            spec.setdefault("bolt_dia_mm",      3.0)   # M3 standard on NEMA 17/23
            break

    # --- Standoff / spacer — extract M-size thread even when "hex" precedes "standoff" ---
    # "M4 hex standoff" → the [mM](\d+)\s+standoff pattern misses it because "hex" is
    # in the middle. Fall back to bare [mM]N if part_type is a standoff/spacer.
    if spec.get("part_type") in ("spacer", "hex_standoff") and "bolt_dia_mm" not in spec:
        _msize = re.search(r"\b[mM](\d+)\b", text)
        if _msize:
            spec["bolt_dia_mm"] = float(_msize.group(1))

    # If part_type is spacer/standoff and bolt_dia_mm is extracted but bore_mm isn't,
    # use bolt_dia_mm as the thread bore.
    if spec.get("part_type") in ("spacer", "standoff", "hex_standoff") and \
            "bore_mm" not in spec and "bolt_dia_mm" in spec:
        spec["bore_mm"] = spec["bolt_dia_mm"]

    # --- Gusset / corner brace legs ---
    # "80mm legs", "80mm leg" → equal legs
    # "80x60mm" already captured by box2 notation, but gusset template uses leg_a/b keys
    if spec.get("part_type") in ("gusset",):
        _leg_equal = _find([
            r"(\d+(?:\.\d+)?)\s*mm\s+legs?",
            r"legs?\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm",
        ])
        if _leg_equal:
            spec.setdefault("leg_a_mm", _leg_equal)
            spec.setdefault("leg_b_mm", _leg_equal)
        # If box2 notation gave width/depth, map those to leg_a/b as well
        if "width_mm" in spec and "leg_a_mm" not in spec:
            spec["leg_a_mm"] = spec["width_mm"]
        if "depth_mm" in spec and "leg_b_mm" not in spec:
            spec["leg_b_mm"] = spec["depth_mm"]

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
