"""recipe_db.py — Shared recipe cache for AutoCAD bridge.

AutoCAD and KiCad both run as standalone Python listeners on separate ports.
They share the same recipe cache (aria_os.ecad.recipe_db) for dimension defaults,
parameter resolutions, and past successes.

This file simply re-exports the shared cache so AutoCAD can access it without
duplication.
"""
from aria_os.ecad import recipe_db as _shared_db

# Re-export all public functions from the shared cache
count = _shared_db.count
init = _shared_db.init
lookup = _shared_db.lookup
lookup_footprint_recipe = _shared_db.lookup_footprint_recipe
record_success = _shared_db.record_success

__all__ = [
    "count",
    "init",
    "lookup",
    "lookup_footprint_recipe",
    "record_success",
]
