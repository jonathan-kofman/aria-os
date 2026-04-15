"""
aria-os-export/core — Multi-CAD abstraction layer.

Sits ABOVE the existing aria_os/generators/ pipeline without modifying it.
Provides an Intermediate Geometry Language (IGL) and a set of backend drivers
that consume IGL documents and produce STEP/STL via different CAD systems.

The existing CadQuery / Rhino / Grasshopper / Blender pipelines under
aria_os/generators/ are unchanged. This layer is opt-in:

    ARIA_GENERATION_MODE=cadquery   # default, existing behavior
    ARIA_GENERATION_MODE=igl        # route through core/ drivers

If IGL mode fails or is disabled, the pipeline falls back to the existing
CadQuery pipeline exactly as before.
"""

__version__ = "0.1.0"
