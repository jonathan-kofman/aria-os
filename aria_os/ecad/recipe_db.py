r"""recipe_db.py — Auto-learning cache of (intent → known-good KiCad/pcbnew
args) for the ARIA ECAD pipeline.

Mirrors:
  cad-plugins/solidworks/AriaSW/RecipeDb.cs
  cad-plugins/rhino/AriaPanel/RecipeDb.cs
  cad-plugins/fusion360/aria_panel/recipe_db.py
  aria_os/onshape/recipe_db.py

Storage: %LOCALAPPDATA%\AriaKicad\recipes.json (Windows) /
         ~/.cache/AriaKicad/recipes.json (Linux/macOS)

What we cache for KiCad:

  footprint_resolve:<value>|<package>   — winning lib:fp name from the
                                          lookup_footprint cascade. Avoids
                                          redoing the 4-step resolution
                                          for every BOM entry on every run.

  place_component_<package>             — known-good pcbnew.FootprintLoad
                                          variant (lib nickname vs full
                                          path, both names work in some
                                          installs and one in others).

  add_track_default                     — winning width / layer combo for
                                          power vs signal nets.

  add_zone_default                      — fill clearance / min thickness
                                          combo that doesn't ERC-error.

  add_via_default                       — drill / diameter that obey the
                                          board's design rules without
                                          DRC complaining.

Same auto-learning shape as the other plugins. Lookup before each pcbnew
call, record_success on every successful return.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional


_LOCK = threading.Lock()
_STORE: dict[str, dict] = {}
_PATH: Optional[Path] = None
_INITIALIZED = False


def _data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / "AriaKicad"
    return Path.home() / ".cache" / "AriaKicad"


def _bootstrap() -> dict[str, dict]:
    """Hand-curated defaults derived from KiCad library v9 patterns."""
    return {
        # Common BOM-value → footprint mappings for typical SMD parts.
        # The full lookup_footprint cascade still runs first; these only
        # serve as a warm cache that survives across runs and across
        # KiCad library updates.
        "footprint_resolve:R_0805|0805": {
            "lib": "Resistor_SMD",
            "fp":  "R_0805_2012Metric",
        },
        "footprint_resolve:C_0805|0805": {
            "lib": "Capacitor_SMD",
            "fp":  "C_0805_2012Metric",
        },
        "footprint_resolve:R_0603|0603": {
            "lib": "Resistor_SMD",
            "fp":  "R_0603_1608Metric",
        },
        "footprint_resolve:C_0603|0603": {
            "lib": "Capacitor_SMD",
            "fp":  "C_0603_1608Metric",
        },
        "footprint_resolve:LED|LED_0805": {
            "lib": "LED_SMD",
            "fp":  "LED_0805_2012Metric",
        },

        # FootprintLoad signature varies across KiCad versions. The two
        # common forms are recorded; whichever returns a non-None first
        # is recorded as the winning variant for this user's install.
        "place_component_signature": {
            "form": "lib_nickname",  # "FootprintLoad('Resistor_SMD', 'R_0805_2012Metric')"
            # alternative: "lib_path" — passes GetOSPath(lib_name) instead
        },

        # Default track widths in mm: 0.25mm signal, 0.5mm power.
        # Layer = F.Cu by default.
        "add_track_default": {
            "method":          "PCB_TRACK",
            "width_mm_signal": 0.25,
            "width_mm_power":  0.5,
            "layer":           "F.Cu",
        },

        # Default via geometry. 0.3mm drill / 0.6mm pad — fits standard
        # PCB fab DRC at minimum spec without going below 6/6 mil rules.
        "add_via_default": {
            "method":       "PCB_VIA",
            "drill_mm":     0.3,
            "diameter_mm":  0.6,
        },

        # Default zone fill — back copper ground pour with 0.2mm clearance
        # and 0.25mm min thickness. Works for most 2-layer boards.
        "add_zone_default": {
            "method":              "ZONE",
            "default_layer":       "B.Cu",
            "clearance_mm":        0.2,
            "min_thickness_mm":    0.25,
        },
    }


def init() -> None:
    global _PATH, _STORE, _INITIALIZED
    if _INITIALIZED:
        return
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        _PATH = d / "recipes.json"
        if _PATH.exists():
            try:
                _STORE = json.loads(_PATH.read_text("utf-8"))
            except Exception:
                _STORE = {}
        for k, v in _bootstrap().items():
            _STORE.setdefault(k, v)
        _save()
    except Exception as ex:
        try:
            print(f"AriaKicad RecipeDb.init failed: {ex}")
        except Exception:
            pass
    finally:
        _INITIALIZED = True


def lookup(intent: str) -> Optional[dict]:
    if not _INITIALIZED:
        init()
    with _LOCK:
        v = _STORE.get(intent)
        return dict(v) if v else None


def record_success(intent: str, args: dict) -> None:
    # Intent-vs-args invariant — same guard pattern shared with SW,
    # Rhino, Fusion RecipeDb implementations. Reject recipes whose args
    # contradict the intent so one quirky success can't poison the
    # cache and cause every future matching intent to replay the bad
    # combo. Cheap and prevents a class of "silently wrong forever"
    # failures we've seen on the SW side.
    if intent and isinstance(args, dict):
        intent_low = intent.lower()
        intent_blind = "blind" in intent_low
        intent_through = "through" in intent_low
        blind_val = args.get("blind")
        if intent_blind and isinstance(blind_val, bool) and not blind_val:
            try:
                print(f"AriaECAD RecipeDb: REJECTED '{intent}' — "
                      "intent says blind but args have blind=false.")
            except Exception:
                pass
            return
        if intent_through and isinstance(blind_val, bool) and blind_val:
            try:
                print(f"AriaECAD RecipeDb: REJECTED '{intent}' — "
                      "intent says through-all but args have blind=true.")
            except Exception:
                pass
            return
    if not _INITIALIZED:
        init()
    with _LOCK:
        _STORE[intent] = dict(args)
        _save()


def footprint_intent(value: str, package: Optional[str] = None) -> str:
    """Build the canonical intent key for a footprint resolution.

    Matches the (value, package) tuple that lookup_footprint takes,
    so resolution wins are persisted and replayed across runs.
    """
    pkg = package or ""
    return f"footprint_resolve:{value}|{pkg}"


def lookup_footprint_recipe(value: str, package: Optional[str] = None
                             ) -> Optional[dict]:
    """Convenience: lookup() with the canonical footprint intent key."""
    return lookup(footprint_intent(value, package))


def record_footprint_success(value: str, package: Optional[str], hit: dict) -> None:
    """Convenience: record_success() with the canonical footprint key."""
    intent = footprint_intent(value, package)
    record_success(intent, {
        "lib": hit.get("lib"),
        "fp":  hit.get("fp"),
    })


def _save() -> None:
    if _PATH is None:
        return
    try:
        _PATH.write_text(
            json.dumps(_STORE, indent=2, sort_keys=True),
            encoding="utf-8")
    except Exception:
        pass


def count() -> int:
    if not _INITIALIZED:
        init()
    with _LOCK:
        return len(_STORE)
