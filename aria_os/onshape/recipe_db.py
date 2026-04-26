r"""recipe_db.py — Auto-learning cache of (intent → known-good Onshape
REST/FeatureScript args) for the ARIA Onshape executor.

Mirrors:
  cad-plugins/solidworks/AriaSW/RecipeDb.cs
  cad-plugins/rhino/AriaPanel/RecipeDb.cs
  cad-plugins/fusion360/aria_panel/recipe_db.py
  cad-plugins/onshape/aria-connector/recipe-db.js (browser-side mirror)

Storage: %LOCALAPPDATA%\AriaOnshape\recipes.json (Windows) /
         ~/.cache/AriaOnshape/recipes.json (Linux/macOS)

What we cache for Onshape (BTMFeature-134 / featureType="extrude"):
  endBound          : "BLIND" / "THROUGH_ALL" / "UP_TO_NEXT" / "UP_TO_FACE"
  oppositeDirection : bool — flip the extrude direction
  bodyType          : "SOLID" / "SURFACE"

Same auto-learning shape as the other plugins. Lookup before each
add_feature call; record_success on every 200-OK reply so the next
request hits the recipe before any handcrafted fallback runs.
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
        return Path(base) / "AriaOnshape"
    return Path.home() / ".cache" / "AriaOnshape"


def _bootstrap() -> dict[str, dict]:
    return {
        "extrude_solid_new": {
            "method":            "feature/extrude",
            "endBound":          "BLIND",
            "oppositeDirection": False,
            "bodyType":          "SOLID",
            "operationType":     "NEW",
        },
        "extrude_solid_join": {
            "method":            "feature/extrude",
            "endBound":          "BLIND",
            "oppositeDirection": False,
            "bodyType":          "SOLID",
            "operationType":     "ADD",
        },
        "extrude_solid_cut": {
            "method":            "feature/extrude",
            "endBound":          "BLIND",
            "oppositeDirection": False,
            "bodyType":          "SOLID",
            "operationType":     "REMOVE",
        },
        "extrude_solid_intersect": {
            "method":            "feature/extrude",
            "endBound":          "BLIND",
            "oppositeDirection": False,
            "bodyType":          "SOLID",
            "operationType":     "INTERSECT",
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
            print(f"AriaOnshape RecipeDb.init failed: {ex}")
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
    if not _INITIALIZED:
        init()
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
    except Exception:
        pass


def count() -> int:
    if not _INITIALIZED:
        init()
    with _LOCK:
        return len(_STORE)
