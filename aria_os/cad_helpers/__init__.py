"""CadQuery hardware helpers.

Wraps `cq_warehouse.thread` and `cq_gears` so generated CadQuery code
(or templates) can call one-line helpers instead of reproducing the
involute / thread math each time. Both upstream libraries are
optional — if not installed, helpers raise a clear ImportError.

Why centralise: the LLM tier doesn't reliably write correct involute
geometry from scratch. Treating "thread" and "gear" as black-box
operators that always produce the right geometry is a much cheaper
contract.

Usage from any CadQuery generator:

    from aria_os.cad_helpers import iso_thread, involute_gear

    # ISO M8x1.25 internal (tap) thread, 20mm long
    thread = iso_thread("M8x1.25", length=20, internal=True)

    # 24-tooth, module 2, 10mm-thick involute spur gear
    gear = involute_gear(module=2, n_teeth=24, thickness=10)

Each function returns a `cadquery.Workplane` whose `.val()` is the
solid Compound. Compose with the calling code's main `result` via
`.union(...)`, `.cut(...)`, etc.
"""
from __future__ import annotations

import re

# --- ISO / ANSI thread spec parsing -----------------------------------

_ISO_METRIC_RE = re.compile(r"^M(\d+(?:\.\d+)?)(?:[xX](\d+(?:\.\d+)?))?$")
_UN_RE = re.compile(r"^(\d+(?:/\d+)?)-(\d+)(?:-(UNC|UNF|UNEF))?$")
_NPT_RE = re.compile(r"^(\d+(?:/\d+)?)-NPT(F)?$")

# Coarse-pitch defaults (mm) for ISO metric where pitch isn't given.
_ISO_COARSE_PITCH = {
    1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0,
    8: 1.25, 10: 1.5, 12: 1.75, 16: 2.0, 20: 2.5, 24: 3.0, 30: 3.5,
}


def _parse_thread_spec(spec: str) -> dict:
    """Return {'family': 'ISO'|'UN'|'NPT', 'major_d': float (mm),
    'pitch_mm': float | None, 'series': str | None}."""
    s = spec.strip().upper()
    m = _ISO_METRIC_RE.match(s)
    if m:
        d = float(m.group(1))
        p = float(m.group(2)) if m.group(2) else _ISO_COARSE_PITCH.get(d)
        if p is None:
            # Round to nearest known coarse pitch
            p = _ISO_COARSE_PITCH[min(_ISO_COARSE_PITCH,
                                        key=lambda k: abs(k - d))]
        return {"family": "ISO", "major_d": d, "pitch_mm": p, "series": None}
    m = _UN_RE.match(s)
    if m:
        # "1/4" → 0.25 in → 6.35 mm
        size_str, tpi, series = m.groups()
        if "/" in size_str:
            num, den = size_str.split("/")
            d_in = float(num) / float(den)
        else:
            d_in = float(size_str) / 16  # # gauge → fraction-of-inch
        d_mm = d_in * 25.4
        pitch_mm = 25.4 / float(tpi)
        return {"family": "UN", "major_d": d_mm,
                 "pitch_mm": pitch_mm, "series": series or "UNC"}
    m = _NPT_RE.match(s)
    if m:
        size_str = m.group(1)
        if "/" in size_str:
            num, den = size_str.split("/")
            d_in = float(num) / float(den)
        else:
            d_in = float(size_str)
        return {"family": "NPT", "major_d": d_in * 25.4,
                 "pitch_mm": None, "series": "TAPER"}
    raise ValueError(f"Cannot parse thread spec: {spec!r}")


# --- Public helpers ---------------------------------------------------

def iso_thread(spec: str, length: float, *, internal: bool = False,
                hand: str = "right"):
    """Return a CadQuery solid for the named thread.

    Args:
        spec:     "M8x1.25", "M16", "1/4-20-UNC", "1/4-NPT"
        length:   thread length in mm
        internal: True for tap (female) threads, False for screw threads
        hand:     "right" or "left"
    """
    parsed = _parse_thread_spec(spec)
    try:
        from cq_warehouse.thread import IsoThread, AcmeThread
    except ImportError as exc:
        raise ImportError(
            "iso_thread() requires cq_warehouse. "
            "pip install cq_warehouse>=0.8.0") from exc
    if parsed["family"] == "ISO":
        return IsoThread(
            major_diameter=parsed["major_d"],
            pitch=parsed["pitch_mm"],
            length=float(length),
            external=not internal,
            hand=hand)
    # UN / NPT — cq_warehouse can usually accept these; fall back to ISO
    # if not, since the dimensional math is similar enough for a
    # cosmetic thread.
    return IsoThread(
        major_diameter=parsed["major_d"],
        pitch=parsed["pitch_mm"] or 1.0,
        length=float(length),
        external=not internal,
        hand=hand)


def involute_gear(module: float, n_teeth: int, thickness: float,
                   *, pressure_angle: float = 20.0,
                   helix_angle: float = 0.0,
                   bore_d: float = 0.0):
    """Return a CadQuery solid for an involute gear.

    Args:
        module:         gear module in mm (Z = OD / (N+2))
        n_teeth:        tooth count (≥4)
        thickness:      face width in mm
        pressure_angle: typically 20° (14.5 for legacy)
        helix_angle:    0 for spur, >0 for helical
        bore_d:         optional center bore diameter in mm
    """
    if n_teeth < 4:
        raise ValueError(f"involute_gear: n_teeth={n_teeth} is too few (min 4)")
    try:
        from cq_gears import SpurGear, HelicalGear
    except ImportError as exc:
        raise ImportError(
            "involute_gear() requires cq_gears. "
            "pip install cq_gears>=0.5.0") from exc
    if helix_angle and abs(helix_angle) > 0.01:
        gear = HelicalGear(
            module=float(module),
            teeth_number=int(n_teeth),
            width=float(thickness),
            pressure_angle=float(pressure_angle),
            helix_angle=float(helix_angle),
            bore_d=float(bore_d) if bore_d > 0 else None)
    else:
        gear = SpurGear(
            module=float(module),
            teeth_number=int(n_teeth),
            width=float(thickness),
            pressure_angle=float(pressure_angle),
            bore_d=float(bore_d) if bore_d > 0 else None)
    # cq_gears builds via .build() returning a CadQuery Workplane
    return gear.build()


__all__ = ["iso_thread", "involute_gear", "_parse_thread_spec"]
