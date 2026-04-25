"""GD&T symbol library + ISO 1101 / ASME Y14.5 callout helpers.

The LLM planner emits GD&T as `gdtFrame` ops with characteristic
strings (e.g. `"position"`, `"perpendicularity"`). This module
turns those into the formal feature-control-frame string a drawing
viewer / printer expects:

    ⌖ Ø0.2 A | B | C
    ⏥ 0.05 — A
    ⫽ 0.1 — A

It also exposes the standard "what to call out for each feature
type" tables so the LLM can be reminded which characteristic is
appropriate for a flange face vs a bore vs a shaft journal.

Usage:
    from aria_os.engineering.gdt import (
        format_frame, recommend_for_feature, GDT_SYMBOLS)

    print(format_frame("position", 0.2, ["A", "B", "C"], dia=True))
    # → ⌖ Ø0.2 A | B | C

    print(recommend_for_feature("flange_back_face"))
    # → [{"characteristic": "flatness", "tolerance": 0.05, "datums": []},
    #    {"characteristic": "parallelism", "tolerance": 0.1,  "datums": ["A"]}]
"""
from __future__ import annotations

# ASME Y14.5 + ISO 1101 symbol set. The unicode glyphs render in any
# modern PDF / SVG viewer. When falling back to drawing tools that
# don't accept unicode (older DXF), use the ASCII alias.
GDT_SYMBOLS: dict[str, dict] = {
    "flatness":            {"glyph": "⏥", "ascii": "FLAT", "type": "form"},
    "straightness":        {"glyph": "─", "ascii": "STR",  "type": "form"},
    "circularity":         {"glyph": "○", "ascii": "CIRC", "type": "form"},
    "cylindricity":        {"glyph": "⌭", "ascii": "CYL",  "type": "form"},
    "perpendicularity":    {"glyph": "⊥", "ascii": "PERP", "type": "orientation"},
    "parallelism":         {"glyph": "⫽", "ascii": "PARA", "type": "orientation"},
    "angularity":          {"glyph": "∠", "ascii": "ANG",  "type": "orientation"},
    "position":            {"glyph": "⌖", "ascii": "POS",  "type": "location"},
    "concentricity":       {"glyph": "◎", "ascii": "CONC", "type": "location"},
    "symmetry":            {"glyph": "⌯", "ascii": "SYM",  "type": "location"},
    "profile_of_a_line":   {"glyph": "⌒", "ascii": "PRFL_LINE", "type": "profile"},
    "profile_of_a_surface": {"glyph": "⌓", "ascii": "PRFL_SURF", "type": "profile"},
    "circular_runout":     {"glyph": "↗", "ascii": "RUNOUT_C",  "type": "runout"},
    "total_runout":        {"glyph": "⌰", "ascii": "RUNOUT_T",  "type": "runout"},
}

# Modifiers that go with the tolerance value
GDT_MODIFIERS: dict[str, str] = {
    "diameter":      "Ø",
    "spherical_dia": "S Ø",
    "mmc":           "Ⓜ",  # max material condition
    "lmc":           "Ⓛ",  # least material condition
    "rfs":           "",   # regardless of feature size (default)
    "free_state":    "Ⓕ",
    "tangent_plane": "Ⓣ",
    "projected_zone": "Ⓟ",
    "statistical":   "ⓢ",
}


def format_frame(characteristic: str, tolerance: float,
                  datums: list[str] | None = None,
                  *, dia: bool = False,
                  modifier: str | None = None,
                  use_ascii: bool = False) -> str:
    """Format a feature-control frame string per ASME Y14.5.

    Args:
        characteristic: one of GDT_SYMBOLS keys
        tolerance:      tolerance zone size in mm
        datums:         ordered list of datum letters (primary, secondary,
                         tertiary). Empty for form characteristics.
        dia:            True if the tolerance zone is cylindrical (Ø prefix)
        modifier:       MMC/LMC/etc. from GDT_MODIFIERS
        use_ascii:      fall back to ASCII glyphs (DXF compatibility)
    """
    sym = GDT_SYMBOLS.get(characteristic.lower())
    if sym is None:
        raise ValueError(
            f"Unknown GD&T characteristic: {characteristic!r}")
    glyph = sym["ascii"] if use_ascii else sym["glyph"]
    parts = [glyph]
    # Ø prefix attaches to the tolerance number with no space — that's
    # what ASME Y14.5 + every drawing tool expects (e.g. "⌖ Ø0.2").
    tol_str = f"{tolerance:g}"
    if dia:
        tol_str = ("Ø" if not use_ascii else "DIA") + tol_str
    if modifier and modifier in GDT_MODIFIERS:
        suffix = GDT_MODIFIERS[modifier] if not use_ascii else modifier.upper()
        tol_str = tol_str + suffix
    parts.append(tol_str)
    if datums:
        parts.append("|".join(d.upper() for d in datums))
    return " ".join(parts)


# Per-feature recommendations — for each common feature type, what
# GD&T callouts a working machinist expects. These are what the LLM
# should emit by default unless overridden by the user.
_FEATURE_GDT_DEFAULTS: dict[str, list[dict]] = {
    # Flange-style flat sealing/mating face — datum A.
    "flange_back_face": [
        {"characteristic": "flatness",       "tolerance": 0.05},
        {"characteristic": "parallelism",    "tolerance": 0.1, "datums": ["A"]},
    ],
    # Center bore on a flange / bearing housing
    "flange_bore": [
        {"characteristic": "cylindricity",     "tolerance": 0.05},
        {"characteristic": "perpendicularity", "tolerance": 0.1, "datums": ["A"]},
    ],
    # Bolt hole pattern
    "bolt_holes": [
        {"characteristic": "position", "tolerance": 0.2,
         "datums": ["A", "B", "C"], "dia": True},
    ],
    # Shaft journal (bearing surface)
    "shaft_journal": [
        {"characteristic": "cylindricity",  "tolerance": 0.005},
        {"characteristic": "total_runout",  "tolerance": 0.02, "datums": ["A"]},
    ],
    # Gear pitch cylinder
    "gear_pitch_cylinder": [
        {"characteristic": "total_runout", "tolerance": 0.03, "datums": ["A"]},
    ],
    # Sheet metal edge (typically just a profile tolerance)
    "sheet_edge": [
        {"characteristic": "profile_of_a_line", "tolerance": 0.5,
         "datums": ["A", "B"]},
    ],
    # Generic outer cylindrical surface (shaft body, post, pin)
    "outer_cylinder": [
        {"characteristic": "cylindricity", "tolerance": 0.02},
    ],
    # Generic top face (mounting / sealing)
    "mating_face": [
        {"characteristic": "flatness", "tolerance": 0.1},
    ],
}


def recommend_for_feature(feature_type: str) -> list[dict]:
    """Return a list of GD&T callout dicts the LLM planner should
    emit by default for the given feature type. Empty list if the
    feature is not in the recommendation table — caller can leave it
    untoleranced or fall back to the general ISO 2768-m default."""
    return list(_FEATURE_GDT_DEFAULTS.get(feature_type, []))


def gdt_summary_for_prompt() -> str:
    """Compact LLM-prompt block summarizing the GD&T vocabulary +
    recommendations. Used by lean_engineering_prompt when DRAWING /
    GD&T family matches."""
    out = ["GD&T (ASME Y14.5 / ISO 1101) — ALWAYS use the unicode glyph:"]
    for k, v in GDT_SYMBOLS.items():
        out.append(f"  {v['glyph']} {k}")
    out.append("")
    out.append("Standard callouts to apply by default:")
    for ftype, callouts in _FEATURE_GDT_DEFAULTS.items():
        first = callouts[0]
        cal = format_frame(first["characteristic"], first["tolerance"],
                            first.get("datums"),
                            dia=first.get("dia", False))
        out.append(f"  {ftype}: {cal}")
    out.append("")
    out.append("Datum convention: primary-secondary-tertiary = A-B-C. "
                "Mark datums with a square frame ⊞-A.")
    return "\n".join(out)


__all__ = [
    "GDT_SYMBOLS", "GDT_MODIFIERS",
    "format_frame", "recommend_for_feature",
    "gdt_summary_for_prompt",
]
