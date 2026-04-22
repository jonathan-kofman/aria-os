"""ISO / ANSI hardware lookup.

Converts engineering shorthand like "M6 clearance hole", "M6 tap", or
"1/4-20 clearance" into the actual drill sizes engineers use. Respects
the same convention used in ISO 273 (metric) and H8/H9 fits.

The default behaviour when a prompt says "M6 holes" without qualifying
(close/medium/loose / clearance/tap/counterbore) is:
  - clearance hole, close fit (ISO 273-H12 close)
  - 6.5mm for M6, 4.5mm for M4, 3.3mm for M3, 8.5mm for M8, etc.

That matches what a machinist would do when given a drawing calling
out "4x M6 mounting holes."
"""
from __future__ import annotations

import re

# ISO 273 clearance holes (close fit — H12). Key = nominal thread size,
# value = clearance hole diameter in mm.
ISO_CLEARANCE_CLOSE = {
    "M1.6": 1.8,  "M2":  2.4, "M2.5": 2.9,  "M3":  3.4,
    "M4":   4.5,  "M5":  5.5, "M6":   6.6,  "M8":  9.0,
    "M10": 11.0,  "M12": 13.5, "M16": 17.5, "M20": 22.0,
    "M24": 26.0,
}

# Medium fit — slightly larger, common for field-assembled parts
ISO_CLEARANCE_MEDIUM = {
    "M1.6": 2.0,  "M2":  2.6, "M2.5": 3.1,  "M3":  3.6,
    "M4":   4.8,  "M5":  5.8, "M6":   7.0,  "M8":  10.0,
    "M10": 12.0,  "M12": 14.5, "M16": 18.5, "M20": 24.0,
    "M24": 28.0,
}

# Tap drill sizes for metric coarse threads
ISO_TAP_DRILL = {
    "M1.6": 1.25, "M2":  1.6, "M2.5": 2.05, "M3":  2.5,
    "M4":   3.3,  "M5":  4.2, "M6":   5.0,  "M8":  6.8,
    "M10": 8.5,   "M12": 10.2, "M16": 14.0, "M20": 17.5,
    "M24": 21.0,
}

# Counterbore dimensions (Ø x depth) for socket head cap screws (SHCS)
# per ISO 4762. Used for countersunk fasteners.
ISO_COUNTERBORE_SHCS = {
    "M3":  (6.5,  3.4),  # (Ø_cbore, depth)
    "M4":  (8.0,  4.6),
    "M5":  (10.0, 5.7),
    "M6":  (11.0, 6.8),
    "M8":  (15.0, 9.0),
    "M10": (18.0, 11.0),
    "M12": (20.0, 13.0),
}


def parse_bolt_spec(text: str) -> dict | None:
    """Extract thread spec from a prompt fragment like 'M6 clearance'
    or '4x M8 SHCS'. Returns {thread: 'M6', fit: 'close'|'medium',
    kind: 'clearance'|'tap'|'counterbore'} or None."""
    t = text.lower()
    m = re.search(r"\b([m]\s*\d+(?:\.\d+)?)\b", t)
    if not m:
        return None
    thread = m.group(1).upper().replace(" ", "")
    # Fit modifier
    if any(k in t for k in ("loose", "free fit", "loose fit")):
        fit = "loose"  # not explicit in our tables — use medium + 0.5
    elif "medium" in t:
        fit = "medium"
    else:
        fit = "close"
    # Hole kind
    if any(k in t for k in ("tap", "tapped", "threaded")):
        kind = "tap"
    elif any(k in t for k in ("counterbore", "c'bore", "cbore", "cb ",
                                "socket head", "shcs", "button head", "bhcs")):
        kind = "counterbore"
    elif any(k in t for k in ("countersink", "csk", "flat head", "fhcs")):
        kind = "countersink"
    else:
        kind = "clearance"
    return {"thread": thread, "fit": fit, "kind": kind}


def clearance_hole_mm(thread: str, fit: str = "close") -> float | None:
    """Return the clearance hole diameter in mm for the given thread."""
    t = thread.upper().replace(" ", "")
    table = ISO_CLEARANCE_MEDIUM if fit == "medium" else ISO_CLEARANCE_CLOSE
    return table.get(t)


def tap_drill_mm(thread: str) -> float | None:
    """Return the tap drill diameter (for a tapped/threaded hole)."""
    return ISO_TAP_DRILL.get(thread.upper().replace(" ", ""))


def counterbore_mm(thread: str) -> tuple[float, float] | None:
    """Return (cbore_dia, cbore_depth) in mm for SHCS counterbores."""
    return ISO_COUNTERBORE_SHCS.get(thread.upper().replace(" ", ""))


def resolve_bolt_hole(spec: dict, prompt: str) -> dict:
    """Given a parsed spec (may have bolt_dia_mm) and the raw prompt
    text, return the ACTUAL hole dimensions to use.

    Prefers:
      1. Explicit thread call-out in the prompt (M6, 1/4-20) →
         look up clearance hole size from ISO table
      2. spec.bolt_dia_mm if present → use directly (advanced users)
      3. Default M6 → 6.6mm

    Returns {hole_dia_mm, thread, fit, kind, source: 'iso'|'spec'|'default'}.
    """
    parsed = parse_bolt_spec(prompt or "")
    if parsed:
        if parsed["kind"] == "clearance":
            hole = clearance_hole_mm(parsed["thread"], parsed["fit"])
        elif parsed["kind"] == "tap":
            hole = tap_drill_mm(parsed["thread"])
        elif parsed["kind"] == "counterbore":
            cb = counterbore_mm(parsed["thread"])
            hole = clearance_hole_mm(parsed["thread"], "close")
            return {
                "hole_dia_mm": hole or 6.6,
                "cbore_dia_mm": cb[0] if cb else None,
                "cbore_depth_mm": cb[1] if cb else None,
                "thread": parsed["thread"],
                "fit": parsed["fit"],
                "kind": "counterbore",
                "source": "iso",
            }
        else:
            hole = clearance_hole_mm(parsed["thread"], "close")
        if hole is not None:
            return {
                "hole_dia_mm": hole,
                "thread": parsed["thread"],
                "fit": parsed["fit"],
                "kind": parsed["kind"],
                "source": "iso",
            }
    # Fall back to spec value — user knows what they want
    if spec.get("bolt_dia_mm"):
        return {
            "hole_dia_mm": float(spec["bolt_dia_mm"]),
            "thread": "",
            "fit": "",
            "kind": "clearance",
            "source": "spec",
        }
    # Default: M6 close clearance
    return {
        "hole_dia_mm": 6.6,
        "thread": "M6",
        "fit": "close",
        "kind": "clearance",
        "source": "default",
    }
