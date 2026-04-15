"""
aria_os/tool_router.py

Routing hierarchy:
  CadQuery    - headless, fast; known templates; LRE/nozzle hard-override
  Rhino/GH    - complex NURBS, booleans on curved surfaces, sweeps, lofts,
                freeform geometry — any part that benefits from RhinoCommon
  Fusion 360  - lattice, generative design, sheet metal,
                additive/CAM setup, simulation
  Blender     - visualization / mesh repair ONLY (not engineering geometry)

Rhino Compute runs headless on localhost:8081. When available, it handles
any geometry that CadQuery templates can't cover — not just ARIA parts.
"""
from typing import Any

FUSION_KEYWORDS = [
    # Lattice / infill (Design Extension)
    "lattice", "gyroid", "octet", "octet truss", "honeycomb", "infill",
    "cellular", "volumetric", "gradient density", "energy absorber",
    "lightweight fill", "tpms", "conformal lattice", "arc weave",
    # Generative / topology (Design Extension)
    "generative", "topology optim", "topopt", "generative design",
    "minimum weight", "structural optim",
    # Sheet metal — REMOVED from this list because CadQuery now has
    # _cq_sheet_metal_panel + _cq_sheet_metal_box templates AND the
    # aria_os/sheet_metal_unfold.py module produces flat-pattern DXFs.
    # Routing sheet metal goals here was sending them to a Fusion path
    # that doesn't exist on Railway and never produced output. Re-add
    # specific sheet-metal keywords here only if a Fusion install
    # becomes available again. Reported by feature verification 2026-04-15.
    # Additive setup (Manufacturing Extension)
    "additive setup", "build prep", "print orientation",
    "support generation", "am setup", "slm setup", "dmls setup",
    # CAM / machining (Manufacturing Extension)
    "toolpath", "cam setup", "cnc program", "g-code",
    "machining strategy", "multi-axis", "adaptive clearing",
    "5-axis", "3+2 machining",
    # Assembly / motion
    "assembly", "motion study", "contact set",
    # Simulation (Design Extension)
    "fea", "stress analysis", "thermal sim", "simulate",
    "modal analysis", "buckling",
    # Organic / T-spline
    "t-spline", "sculpt", "ergonomic grip", "organic surface",
]

SDF_KEYWORDS = [
    "lattice", "gyroid", "schwarz", "tpms", "infill", "porous",
    "organic", "topology optim", "conformal", "variable density",
    "heat exchanger", "cellular", "foam", "sponge", "lightweight fill",
    "gradient density", "bone structure", "voronoi", "metamaterial",
]

GRASSHOPPER_KEYWORDS = [
    # Surface/NURBS operations
    "helical", "helix", "sweep", "loft", "ruled surface", "freeform",
    "spline", "cam ramp", "spiral", "twisted", "variable pitch",
    "surface", "nurbs", "b-spline", "bezier",
    # Boolean operations on complex geometry
    "boolean", "boolean difference", "boolean union", "boolean intersection",
    "shell", "hollow", "thick wall",
    # Rhino-specific geometry
    "brep", "polysurface", "fillet", "chamfer", "blend",
    "pipe", "revolve", "extrude along curve", "rail sweep",
    # Complex mechanical parts — NOTE: "impeller" and "involute" are NOT listed here
    # because CadQuery now has _cq_impeller and _cq_involute_gear templates.
    # Only list parts with NO CadQuery template (routes to Rhino Compute).
    "turbine blade", "propeller", "fan blade",
    "cam profile", "gear tooth",
    "manifold", "duct", "transition piece",
    "ergonomic", "contoured", "sculpted surface",
]

# Blender: visualization and mesh repair only
BLENDER_KEYWORDS = [
    "mesh repair", "cleanup", "decimate", "remesh", "render",
    "visualization", "sculpt mesh",
]

FUSION_PART_IDS = {
    "aria_energy_absorber", "aria_lattice_housing", "aria_assembly",
    "aria_sheet_metal_bracket", "aria_generative_housing",
}

GRASSHOPPER_PART_IDS = {
    "aria_cam_collar",
    "aria_spool",
    "aria_housing",
    "aria_ratchet_ring",
    "aria_brake_drum",
    "aria_rope_guide",
}

# Parts that always route to CadQuery headless regardless of other keywords.
# Include everything CadQuery has a dedicated template for — prevents them from
# being mis-routed to Grasshopper/Compute via keyword overlap.
CADQUERY_KEYWORDS = [
    "nozzle", "rocket", "lre", "liquid rocket", "turbopump", "injector",
    "impeller", "centrifugal fan", "axial fan",     # _cq_impeller
    "involute gear", "involute spur", "spur gear",  # _cq_involute_gear
]

# Civil engineering plans route to AutoCAD/DXF generator
AUTOCAD_KEYWORDS = [
    "road plan", "street plan", "highway plan", "drainage plan", "storm sewer",
    "grading plan", "site plan", "utility plan", "civil plan", "civil engineering",
    "dxf", "autocad", "right of way", "row plan", "earthwork plan",
    "pavement design", "site civil", "land development plan",
    "storm drain plan", "culvert plan", "retaining wall plan",
]

# part_ids that should always use Blender (visualization/mesh repair)
BLENDER_PART_IDS: set[str] = set()  # lattice/SDF now handled by sdf_generator


def _is_compute_available() -> bool:
    """Check if Rhino Compute is running."""
    try:
        from .compute_client import ComputeClient
        return ComputeClient().is_available()
    except Exception:
        return False


def select_cad_tool(goal: str, plan: dict[str, Any]) -> str:
    """
    Return one of: "autocad", "cadquery", "fusion", "grasshopper", "blender"

    Priority:
      1. autocad     - civil engineering plans (DXF output)
      2. cadquery    - LRE/nozzle hard-override
      3. blender     - lattice/SDF parts
      4. grasshopper - keyword match OR known GH parts OR no CQ template (when Compute is up)
      5. fusion      - lattice, generative, sheet metal, additive, CAM, sim, sculpt
      6. cadquery    - default fallback
    """
    goal_lower = (goal or "").lower()
    part_id    = str(plan.get("part_id", ""))
    features   = plan.get("features", []) or []

    # Hard overrides — domain-specific
    if any(kw in goal_lower for kw in AUTOCAD_KEYWORDS):
        return "autocad"
    if any(kw in goal_lower for kw in CADQUERY_KEYWORDS):
        return "cadquery"
    if part_id in BLENDER_PART_IDS:
        return "blender"

    # Known Grasshopper parts (always route, Compute or CQ fallback)
    if part_id in GRASSHOPPER_PART_IDS:
        return "grasshopper"
    if part_id in FUSION_PART_IDS:
        return "fusion360"

    # Feature-based routing
    for f in features:
        if not isinstance(f, dict):
            continue
        if f.get("type") == "ramp" or "helical" in str(f.get("description", "")).lower():
            return "grasshopper"
        if f.get("type") == "sheet_metal":
            return "cadquery"   # CQ now has sheet_metal templates + unfold module
        if f.get("type") in ("lattice", "generative"):
            return "fusion360"

    # Keyword-based routing
    if any(kw in goal_lower for kw in SDF_KEYWORDS):
        return "sdf"
    if any(kw in goal_lower for kw in GRASSHOPPER_KEYWORDS):
        return "grasshopper"
    if any(kw in goal_lower for kw in FUSION_KEYWORDS):
        return "fusion360"
    if any(kw in goal_lower for kw in BLENDER_KEYWORDS):
        return "blender"

    # No keyword match — if CadQuery has no template and Compute is running,
    # prefer Grasshopper/Compute over LLM-generated CadQuery code
    if _is_compute_available():
        try:
            from .generators.cadquery_generator import _find_template_fn
            has_template = _find_template_fn(part_id) is not None
        except Exception:
            has_template = False
        if not has_template:
            return "grasshopper"

    return "cadquery"


def is_zoo_backend_available() -> bool:
    """Check if Zoo.dev (KittyCAD) text-to-CAD is available as a backend."""
    try:
        from .zoo_bridge import is_zoo_available
        return is_zoo_available()
    except Exception:
        return False


def get_output_formats(tool: str) -> list[str]:
    """Return expected output file extensions for each tool."""
    return {
        "cadquery":    ["step", "stl"],
        "fusion360":   ["step", "stl"],
        "grasshopper": ["step", "stl"],
        "blender":     ["stl"],
        "zoo":         ["step"],
    }.get(tool, ["stl"])
