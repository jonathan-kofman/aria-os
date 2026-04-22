"""
Native feature-tree planner.

Translates an ARIA goal + extracted spec into a stream of bridge
operations (newSketch / sketchCircle / extrude / circularPattern / fillet)
that the Fusion/Rhino add-in executes as REAL native features. The panel
dispatches each op through `bridge.executeFeature(kind, params)` so the
host CAD's browser tree fills in live — one genuine Sketch → Extrude →
Hole → Pattern entry per step, not a synthetic parallel tree.

This is the alternative to the write-CadQuery-and-import-STEP pipeline.
Existing mechanical / electrical / assembly modes are untouched; this
just adds a new `mode=native` code path.
"""
from .dispatcher import make_plan

__all__ = ["make_plan"]
