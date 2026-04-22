"""ISO 2768 general tolerances — linear + angular.

Every drawing dim that doesn't call out a specific tolerance falls
under the general class: f (fine), m (medium), c (coarse), v (very
coarse). Most mechanical shop drawings use `m`.
"""
from __future__ import annotations

# ISO 2768-1 linear tolerances (mm) by nominal size range
# Class keys are lowercase single-letter.
_LINEAR_2768 = {
    # (min_mm, max_mm):  {class: ±tol_mm}
    (0.5,     3):    {"f": 0.05, "m": 0.10, "c": 0.20, "v": None},
    (3,       6):    {"f": 0.05, "m": 0.10, "c": 0.30, "v": 0.50},
    (6,       30):   {"f": 0.10, "m": 0.20, "c": 0.50, "v": 1.00},
    (30,     120):   {"f": 0.15, "m": 0.30, "c": 0.80, "v": 1.50},
    (120,    400):   {"f": 0.20, "m": 0.50, "c": 1.20, "v": 2.50},
    (400,   1000):   {"f": 0.30, "m": 0.80, "c": 2.00, "v": 4.00},
    (1000,  2000):   {"f": 0.50, "m": 1.20, "c": 3.00, "v": 6.00},
}

# Angular tolerance in degrees by angle's shorter leg
_ANGULAR_2768 = {
    (0,    10):  {"f": 1.0,   "m": 1.0,   "c": 1.5,   "v": 3.0},
    (10,  50):   {"f": 0.5,   "m": 0.5,   "c": 1.0,   "v": 2.0},
    (50, 120):   {"f": 0.33,  "m": 0.33,  "c": 0.5,   "v": 1.0},
    (120, 400):  {"f": 0.17,  "m": 0.17,  "c": 0.25,  "v": 0.5},
    (400, None): {"f": 0.083, "m": 0.083, "c": 0.17,  "v": 0.33},
}

DEFAULT_CLASS = "m"
CLASS_LABELS = {
    "f": "ISO 2768-f (fine)",
    "m": "ISO 2768-m (medium)",
    "c": "ISO 2768-c (coarse)",
    "v": "ISO 2768-v (very coarse)",
}


def tolerance_band_mm(nominal_mm: float,
                       tolerance_class: str = DEFAULT_CLASS) -> float | None:
    """Return the ±tolerance band in mm for the given nominal size.
    Returns None if the size is outside the ISO 2768 table range."""
    v = abs(float(nominal_mm))
    cls = tolerance_class.lower()
    for (lo, hi), bands in _LINEAR_2768.items():
        if lo <= v < hi:
            return bands.get(cls)
    return None


def class_label(tolerance_class: str = DEFAULT_CLASS) -> str:
    return CLASS_LABELS.get(tolerance_class.lower(),
                              CLASS_LABELS[DEFAULT_CLASS])


def tolerance_callout(nominal_mm: float,
                       tolerance_class: str = DEFAULT_CLASS) -> str:
    """Drawing-ready callout like '±0.2' for a dim under ISO 2768-m."""
    band = tolerance_band_mm(nominal_mm, tolerance_class)
    return f"±{band}" if band is not None else "(per drawing)"
