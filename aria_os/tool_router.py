"""
aria_os/tool_router.py

Routing hierarchy:
  CadQuery    - headless, fast; 16 known templates; LRE/nozzle hard-override
  Grasshopper - the 6 core ARIA structural parts (needs Rhino Compute)
  Fusion 360  - primary for lattice, generative design, sheet metal,
                additive/CAM setup, simulation, organic surfaces, assemblies
  Blender     - visualization / mesh repair ONLY (not engineering geometry)
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
    # Sheet metal (built-in, no extension)
    "sheet metal", "sheetmetal", "sheet-metal", "stamping",
    "flat pattern", "enclosure panel",
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

GRASSHOPPER_KEYWORDS = [
    "helical", "helix", "sweep", "loft", "ruled surface", "freeform",
    "spline", "cam ramp", "spiral", "twisted", "variable pitch",
    "surface", "nurbs",
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

# LRE / nozzle parts always route to CadQuery headless (no Grasshopper)
CADQUERY_KEYWORDS = [
    "nozzle", "rocket", "lre", "liquid rocket", "turbopump", "injector",
]

# Civil engineering plans route to AutoCAD/DXF generator
AUTOCAD_KEYWORDS = [
    "road plan", "street plan", "highway plan", "drainage plan", "storm sewer",
    "grading plan", "site plan", "utility plan", "civil plan", "civil engineering",
    "dxf", "autocad", "right of way", "row plan", "earthwork plan",
    "pavement design", "site civil", "land development plan",
    "storm drain plan", "culvert plan", "retaining wall plan",
]

# part_ids that should always use Blender (lattice/SDF-based generation)
BLENDER_PART_IDS = {
    "lattice", "gyroid_lattice", "aria_lattice", "sdf_lattice",
}


def select_cad_tool(goal: str, plan: dict[str, Any]) -> str:
    """
    Return one of: "autocad", "cadquery", "fusion", "grasshopper", "blender"

    Priority:
      1. autocad     - civil engineering plans (DXF output)
      2. cadquery    - LRE/nozzle hard-override
      3. grasshopper - 6 core ARIA structural parts
      4. fusion      - lattice, generative, sheet metal, additive, CAM, sim, sculpt
      5. blender     - visualization / mesh repair only
      6. cadquery    - default fallback
    """
    goal_lower = (goal or "").lower()
    part_id    = str(plan.get("part_id", ""))
    features   = plan.get("features", []) or []

    if any(kw in goal_lower for kw in AUTOCAD_KEYWORDS):
        return "autocad"
    if any(kw in goal_lower for kw in CADQUERY_KEYWORDS):
        return "cadquery"
    if part_id in BLENDER_PART_IDS:
        return "blender"   # lattice/gyroid always use Blender SDF pipeline
    if part_id in GRASSHOPPER_PART_IDS:
        return "grasshopper"
    if part_id in FUSION_PART_IDS:
        return "fusion"

    for f in features:
        if not isinstance(f, dict):
            continue
        if f.get("type") == "ramp" or "helical" in str(f.get("description", "")).lower():
            return "grasshopper"
        if f.get("type") in ("lattice", "generative", "sheet_metal"):
            return "fusion"

    if any(kw in goal_lower for kw in GRASSHOPPER_KEYWORDS):
        return "grasshopper"
    if any(kw in goal_lower for kw in FUSION_KEYWORDS):
        return "fusion"
    if any(kw in goal_lower for kw in BLENDER_KEYWORDS):
        return "blender"

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
        "fusion":      ["step", "stl"],
        "grasshopper": ["step", "stl"],
        "blender":     ["stl"],
        "zoo":         ["step"],
    }.get(tool, ["stl"])
