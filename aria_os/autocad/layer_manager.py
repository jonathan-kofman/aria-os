"""
layer_manager.py — AutoCAD layer definitions for all civil engineering disciplines.

Follows NCS (National CAD Standard) and common DOT layer naming conventions.
Colors use standard AutoCAD color numbers.
"""
from __future__ import annotations

# color constants
_WHITE   = 7
_RED     = 1
_YELLOW  = 2
_GREEN   = 3
_CYAN    = 4
_BLUE    = 5
_MAGENTA = 6
_GREY    = 8
_ORANGE  = 30
_BROWN   = 34
_LTGREEN = 92
_LTBLUE  = 150
_PINK    = 200

# LAYER_DEFS: layer_name → {color, linetype, lineweight, description}
LAYER_DEFS: dict[str, dict] = {
    # ── Transportation ────────────────────────────────────────────────────────
    "ROAD-CL":          {"color": _RED,     "linetype": "CENTER",     "lineweight": 0.35, "description": "Road centerline"},
    "ROAD-EOP":         {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Edge of pavement"},
    "ROAD-SHLDR":       {"color": _GREY,    "linetype": "DASHED",     "lineweight": 0.25, "description": "Shoulder edge"},
    "ROAD-CURB":        {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Curb and gutter"},
    "ROAD-SIDEWALK":    {"color": _GREY,    "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Sidewalk"},
    "ROAD-XSEC":        {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Road cross-section"},
    "ROAD-TURN-LANE":   {"color": _ORANGE,  "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Turn lane"},
    "ROAD-BIKE-LANE":   {"color": _GREEN,   "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Bike lane"},
    "ROAD-DIM":         {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Road dimensions"},
    "ROAD-STRIPING":    {"color": _WHITE,   "linetype": "DASHED",     "lineweight": 0.25, "description": "Pavement striping"},
    "ROAD-MEDIAN":      {"color": _LTGREEN, "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Median"},
    "ROAD-STATION":     {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Station labels"},

    # ── Drainage ──────────────────────────────────────────────────────────────
    "DRAIN-PIPE-STORM":    {"color": _CYAN,  "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Storm sewer pipe"},
    "DRAIN-PIPE-SANITARY": {"color": _BROWN, "linetype": "DASHED",     "lineweight": 0.50, "description": "Sanitary sewer pipe"},
    "DRAIN-INLET":         {"color": _CYAN,  "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Storm inlet"},
    "DRAIN-MH":            {"color": _CYAN,  "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Manhole"},
    "DRAIN-CULVERT":       {"color": _CYAN,  "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Culvert"},
    "DRAIN-CHANNEL":       {"color": _LTBLUE,"linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Open channel/swale"},
    "DRAIN-POND":          {"color": _BLUE,  "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Detention/retention pond"},
    "DRAIN-SWALE":         {"color": _LTBLUE,"linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Drainage swale"},
    "DRAIN-CONTOUR-EXIST": {"color": _GREY,  "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Existing drainage contour"},
    "DRAIN-CONTOUR-PROP":  {"color": _CYAN,  "linetype": "DASHED",     "lineweight": 0.25, "description": "Proposed drainage contour"},
    "DRAIN-FLOWLINE":      {"color": _BLUE,  "linetype": "CENTER",     "lineweight": 0.25, "description": "Flow direction"},
    "DRAIN-LABEL":         {"color": _CYAN,  "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Drainage labels"},

    # ── Grading ───────────────────────────────────────────────────────────────
    "GRADE-EXIST-CONTOUR": {"color": _GREY,    "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Existing contour"},
    "GRADE-EXIST-INDEX":   {"color": _GREY,    "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Existing index contour"},
    "GRADE-PROP-CONTOUR":  {"color": _GREEN,   "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Proposed contour"},
    "GRADE-PROP-INDEX":    {"color": _GREEN,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Proposed index contour"},
    "GRADE-SLOPE":         {"color": _ORANGE,  "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Slope arrow"},
    "GRADE-RETWALL":       {"color": _BROWN,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Retaining wall"},
    "GRADE-BERM":          {"color": _GREEN,   "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Berm"},
    "GRADE-SWALE":         {"color": _LTGREEN, "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Grading swale"},
    "GRADE-FILL":          {"color": _YELLOW,  "linetype": "DASHED",     "lineweight": 0.18, "description": "Fill area"},
    "GRADE-CUT":           {"color": _RED,     "linetype": "DASHED",     "lineweight": 0.18, "description": "Cut area"},
    "GRADE-SPOT-ELEV":     {"color": _GREEN,   "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Spot elevation"},

    # ── Utilities ─────────────────────────────────────────────────────────────
    "UTIL-WATER-MAIN":     {"color": _BLUE,    "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Water main"},
    "UTIL-WATER-SERVICE":  {"color": _BLUE,    "linetype": "DASHED",     "lineweight": 0.25, "description": "Water service"},
    "UTIL-SEWER-MAIN":     {"color": _BROWN,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Sewer main"},
    "UTIL-GAS-MAIN":       {"color": _YELLOW,  "linetype": "DASHED2",    "lineweight": 0.50, "description": "Gas main"},
    "UTIL-ELEC-DUCTBANK":  {"color": _MAGENTA, "linetype": "DASHED",     "lineweight": 0.50, "description": "Electrical duct bank"},
    "UTIL-FIBER":          {"color": _ORANGE,  "linetype": "DASHED",     "lineweight": 0.25, "description": "Fiber optic"},
    "UTIL-STORM-MAIN":     {"color": _CYAN,    "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Storm main (utility)"},
    "UTIL-LABEL":          {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Utility labels"},
    "UTIL-XING":           {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Utility crossing"},

    # ── Structural ────────────────────────────────────────────────────────────
    "STRUC-FOOTING":       {"color": _BROWN,   "linetype": "HIDDEN",     "lineweight": 0.50, "description": "Footing"},
    "STRUC-COLUMN":        {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.70, "description": "Column"},
    "STRUC-BEAM":          {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Beam"},
    "STRUC-SLAB":          {"color": _GREY,    "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Slab"},
    "STRUC-RETWALL":       {"color": _BROWN,   "linetype": "CONTINUOUS", "lineweight": 0.70, "description": "Structural retaining wall"},
    "STRUC-BRIDGE-DECK":   {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Bridge deck"},
    "STRUC-PIER":          {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.70, "description": "Bridge pier"},
    "STRUC-ABUTMENT":      {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Bridge abutment"},

    # ── Survey ────────────────────────────────────────────────────────────────
    "SURV-BOUNDARY":       {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.70, "description": "Property boundary"},
    "SURV-EASEMENT":       {"color": _ORANGE,  "linetype": "DASHED",     "lineweight": 0.35, "description": "Easement"},
    "SURV-ROW":            {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Right-of-way"},
    "SURV-TOPO":           {"color": _GREY,    "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Topographic data"},
    "SURV-CONTROL":        {"color": _MAGENTA, "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Survey control point"},
    "SURV-MONUMENT":       {"color": _MAGENTA, "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Survey monument"},
    "SURV-SETBACK":        {"color": _ORANGE,  "linetype": "DASHED",     "lineweight": 0.25, "description": "Building setback"},

    # ── Site ──────────────────────────────────────────────────────────────────
    "SITE-BLDG":           {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.70, "description": "Building footprint"},
    "SITE-PARKING":        {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Parking stall"},
    "SITE-CURB":           {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Site curb"},
    "SITE-SIDEWALK":       {"color": _GREY,    "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Site sidewalk"},
    "SITE-ADA-RAMP":       {"color": _GREEN,   "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "ADA curb ramp"},
    "SITE-LANDSCAPE":      {"color": _LTGREEN, "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Landscaping"},
    "SITE-FENCE":          {"color": _BROWN,   "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Fence"},
    "SITE-SIGN":           {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Sign"},
    "SITE-LIGHT":          {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.25, "description": "Site lighting"},

    # ── Annotation ────────────────────────────────────────────────────────────
    "ANNO-DIM":            {"color": _YELLOW,  "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Dimensions"},
    "ANNO-TEXT":           {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "General text"},
    "ANNO-LEADER":         {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.18, "description": "Leader/callout"},
    "ANNO-SECTION":        {"color": _RED,     "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "Section cut line"},
    "ANNO-TITLEBLOCK":     {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.50, "description": "Title block"},
    "ANNO-NORTH":          {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.35, "description": "North arrow"},
    "ANNO-MATCHLINE":      {"color": _RED,     "linetype": "PHANTOM",    "lineweight": 0.50, "description": "Match line"},
    "DEFPOINTS":           {"color": _WHITE,   "linetype": "CONTINUOUS", "lineweight": 0.00, "description": "Non-printing points"},
}

# Discipline → default layers used
DISCIPLINE_LAYERS: dict[str, list[str]] = {
    "transportation": [k for k in LAYER_DEFS if k.startswith("ROAD-")],
    "drainage":       [k for k in LAYER_DEFS if k.startswith("DRAIN-")],
    "grading":        [k for k in LAYER_DEFS if k.startswith("GRADE-")],
    "utilities":      [k for k in LAYER_DEFS if k.startswith("UTIL-")],
    "structural":     [k for k in LAYER_DEFS if k.startswith("STRUC-")],
    "survey":         [k for k in LAYER_DEFS if k.startswith("SURV-")],
    "site":           [k for k in LAYER_DEFS if k.startswith("SITE-")],
    "annotation":     [k for k in LAYER_DEFS if k.startswith("ANNO-")],
}


def get_layer(discipline: str, element: str) -> str:
    """
    Return best-matching layer name for a discipline + element keyword.
    Falls back to ANNO-TEXT if no match found.
    """
    d = discipline.upper().replace(" ", "-")
    e = element.upper().replace(" ", "-")
    candidate = f"{d}-{e}"
    if candidate in LAYER_DEFS:
        return candidate
    # prefix match on discipline
    disc_layers = DISCIPLINE_LAYERS.get(discipline.lower(), [])
    for lyr in disc_layers:
        if e in lyr:
            return lyr
    # fuzzy: any layer containing element keyword
    for lyr in LAYER_DEFS:
        if e in lyr:
            return lyr
    return "ANNO-TEXT"
