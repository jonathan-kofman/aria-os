r"""recipe_db.py — Auto-learning cache of (intent → known-good Fusion API
args) for the ARIA Fusion 360 add-in.

Mirrors:
  cad-plugins/solidworks/AriaSW/RecipeDb.cs
  cad-plugins/rhino/AriaPanel/RecipeDb.cs

Storage: %APPDATA%\AriaFusion\recipes.json (Windows) /
         ~/Library/Application Support/AriaFusion/recipes.json (macOS)

What we cache for Fusion (adsk.fusion.* native ops):
  extrude_solid_new       NewBodyFeatureOperation knobs
  extrude_solid_join      JoinFeatureOperation knobs
  extrude_solid_cut       CutFeatureOperation knobs
  extrude_solid_intersect IntersectFeatureOperation knobs

Tuning knobs persisted:
  extentDirection : "positive" / "negative" / "symmetric"
  flipDistance    : whether to negate the input distance
  allProfiles     : True if all profiles should be used (multi-profile
                    sketches sometimes need this)

The same auto-learning shape applies: every successful native API call
records the winning knobs so the next request hits the recipe.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional


_LOCK = threading.Lock()
_STORE: dict[str, dict] = {}
_PATH: Optional[Path] = None


def _data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "AriaFusion"
    # macOS / Linux
    return Path.home() / "Library" / "Application Support" / "AriaFusion"


def _bootstrap() -> dict[str, dict]:
    return {
        "extrude_solid_new": {
            "method": "extrudeFeatures.add (NewBody)",
            "extentDirection": "positive",
            "flipDistance": False,
            "allProfiles": False,
        },
        "extrude_solid_join": {
            "method": "extrudeFeatures.add (Join)",
            "extentDirection": "positive",
            "flipDistance": False,
            "allProfiles": False,
        },
        "extrude_solid_cut": {
            "method": "extrudeFeatures.add (Cut)",
            "extentDirection": "positive",
            "flipDistance": False,
            "allProfiles": False,
        },
        "extrude_solid_intersect": {
            "method": "extrudeFeatures.add (Intersect)",
            "extentDirection": "positive",
            "flipDistance": False,
            "allProfiles": False,
        },
    }


def init() -> None:
    """Load cache from disk, layer bootstrap recipes, persist."""
    global _PATH, _STORE
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        _PATH = d / "recipes.json"

        if _PATH.exists():
            try:
                _STORE = json.loads(_PATH.read_text("utf-8"))
            except Exception:
                _STORE = {}

        # Layer bootstrap (existing wins on collision — represents an
        # actual successful run on this user's Fusion install).
        for k, v in _bootstrap().items():
            _STORE.setdefault(k, v)
        _save()
    except Exception as ex:
        # Init failures are non-fatal — addin keeps working without cache.
        try:
            print(f"AriaFusion RecipeDb.init failed: {ex}")
        except Exception:
            pass


def lookup(intent: str) -> Optional[dict]:
    with _LOCK:
        v = _STORE.get(intent)
        return dict(v) if v else None


def record_success(intent: str, args: dict) -> None:
    with _LOCK:
        _STORE[intent] = dict(args)
        _save()


def _save() -> None:
    if _PATH is None:
        return
    try:
        _PATH.write_text(
            json.dumps(_STORE, indent=2, sort_keys=True),
            encoding="utf-8")
    except Exception as ex:
        try:
            print(f"AriaFusion RecipeDb._save failed: {ex}")
        except Exception:
            pass


def count() -> int:
    with _LOCK:
        return len(_STORE)
