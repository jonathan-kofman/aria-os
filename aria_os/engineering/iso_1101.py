"""ISO 1101 / ASME Y14.5 GD&T symbols + rules for which to apply.

Engineers' mental cheat-sheet, in code form. Each symbol maps to:
  - the unicode glyph for drawing display
  - a tolerance zone description
  - the typical datum structure
  - the feature types it belongs on

Planners call `gdt_callout_for_feature(feature_type)` to get the
right GD&T to add to a drawing view, pre-filled with engineering
defaults (flatness 0.05 on sealing faces, perpendicularity 0.1 on
bolt hole axes to back face, etc.).
"""
from __future__ import annotations

GDT_SYMBOLS = {
    # Form — single feature, no datum
    "straightness":          {"glyph": "⏤", "kind": "form"},
    "flatness":              {"glyph": "⏥", "kind": "form"},
    "circularity":           {"glyph": "○", "kind": "form"},
    "cylindricity":          {"glyph": "⌭", "kind": "form"},
    # Profile — may or may not need datum
    "profile_line":          {"glyph": "⌒", "kind": "profile"},
    "profile_surface":       {"glyph": "⌓", "kind": "profile"},
    # Orientation — relative to datum
    "perpendicularity":      {"glyph": "⊥", "kind": "orientation"},
    "parallelism":           {"glyph": "∥", "kind": "orientation"},
    "angularity":            {"glyph": "∠", "kind": "orientation"},
    # Location — relative to datums
    "position":              {"glyph": "⌖", "kind": "location"},
    "concentricity":         {"glyph": "◎", "kind": "location"},
    "symmetry":              {"glyph": "⌯", "kind": "location"},
    # Runout — relative to rotation axis
    "runout_circular":       {"glyph": "↗", "kind": "runout"},
    "runout_total":          {"glyph": "⌰", "kind": "runout"},
}

# Feature type → list of (gdt_kind, tolerance_mm, datum_ref) triples
# with engineer-approved defaults.
_FEATURE_GDT = {
    # Flat face that seals / mates
    "sealing_face": [
        ("flatness",         0.05, ""),      # 0.05mm flatness typical
        ("surface_finish",   3.2,  ""),      # Ra 3.2 µm
    ],
    "mounting_face": [
        ("flatness",         0.10, ""),
    ],
    # Back face used as datum A
    "primary_datum_face": [
        ("flatness",         0.05, "A"),
    ],
    # Bolt holes in a pattern
    "bolt_hole_pattern": [
        ("position",         0.20, "A|B|C"),
        ("perpendicularity", 0.10, "A"),
    ],
    "single_bolt_hole": [
        ("perpendicularity", 0.10, "A"),
    ],
    # Cylindrical features
    "bore": [
        ("cylindricity",     0.05, ""),
        ("perpendicularity", 0.10, "A"),
    ],
    "outer_cylinder": [
        ("cylindricity",     0.10, ""),
        ("runout_total",     0.05, "A"),
    ],
    # Shaft features
    "shaft_journal": [
        ("cylindricity",     0.02, ""),
        ("runout_circular",  0.02, "A-B"),
    ],
    # Gear teeth
    "gear_pitch_cylinder": [
        ("runout_total",     0.03, "A"),
    ],
}


def gdt_callout_for_feature(feature_type: str) -> list[dict]:
    """Return the GD&T callouts to apply to the named feature.

    Each callout is a dict:
      {kind: str, glyph: str, tol_mm: float, datum: str, text: str}
    where `text` is drawing-ready (e.g. "⊥ ⌀0.1 A").
    """
    specs = _FEATURE_GDT.get(feature_type, [])
    out = []
    for kind, tol_mm, datum in specs:
        if kind == "surface_finish":
            out.append({
                "kind": kind, "glyph": "√", "tol_mm": tol_mm,
                "datum": datum,
                "text": f"Ra {tol_mm:g} µm",
            })
            continue
        sym = GDT_SYMBOLS.get(kind, {})
        glyph = sym.get("glyph", kind)
        datum_str = f" | {datum}" if datum else ""
        out.append({
            "kind": kind, "glyph": glyph, "tol_mm": tol_mm,
            "datum": datum,
            "text": f"{glyph} {tol_mm:g}{datum_str}",
        })
    return out
