"""
Standard parts library for ARIA-OS assemblies.

Replaces LLM-generated hardware (bolts, bearings, motors, couplings) with
deterministic parametric CAD components keyed by standard designation.

Usage:
    from aria_os.components import catalog

    # Fetch a component by designation
    spec = catalog.get("M6x20_12.9")  # returns ComponentSpec
    step_path = catalog.generate(spec, out_dir)  # writes STEP, returns path

    # Query by category
    for bolt in catalog.list_category("fasteners"):
        print(bolt.designation, bolt.mass_g)

Components expose named **mating features** (e.g. "shaft_axis", "top_face",
"bolt_circle") that the mating_solver uses to align parts automatically.
"""

from .catalog import (
    ComponentSpec,
    ComponentCatalog,
    MatingFeature,
    catalog,
    register_component,
    get_component,
    list_components,
)

__all__ = [
    "ComponentSpec",
    "ComponentCatalog",
    "MatingFeature",
    "catalog",
    "register_component",
    "get_component",
    "list_components",
]

# Register all built-in components at import time. Each submodule calls
# register_component() in its module-level code.
from . import fasteners       # noqa: F401 — registers bolts/nuts/washers
from . import bearings        # noqa: F401 — registers bearings
from . import motors          # noqa: F401 — registers NEMA steppers
from . import couplings       # noqa: F401 — registers couplings
from . import hardware        # noqa: F401 — registers dowels/rings
from . import linear_motion   # noqa: F401 — registers rails/ballscrews/pulleys
from . import bldc_motors     # noqa: F401 — registers BLDC outrunner motors
from . import propellers      # noqa: F401 — registers propellers
from . import drone_electronics  # noqa: F401 — registers ESCs/LiPos/standoffs/strap
