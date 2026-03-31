from dataclasses import dataclass
from typing import Optional, Literal, List, Dict


@dataclass
class LatticeParams:
    # Pattern selection
    pattern: Literal["arc_weave", "honeycomb", "octet_truss"]

    # Form factor
    form: Literal["volumetric", "conformal", "skin_core"]

    # Bounding geometry
    width_mm: float = 100.0
    height_mm: float = 100.0
    depth_mm: float = 10.0

    # Unit cell
    cell_size_mm: float = 10.0

    # Strut geometry
    strut_diameter_mm: float = 1.5

    # Skin (for skin_core form)
    skin_thickness_mm: float = 2.0

    # Frame (border around panel)
    frame_thickness_mm: float = 5.0

    # Interlaced weave controls (arc_weave only)
    interlaced: bool = False
    weave_offset_mm: float = 0.0

    # Process
    process: Literal["fdm", "dmls", "both"] = "both"

    # STEP/STL output paths (injected by generator)
    step_path: Optional[str] = None
    stl_path: Optional[str] = None
    part_name: str = "lattice_panel"


@dataclass
class LatticeResult:
    params: LatticeParams
    step_path: str
    stl_path: str
    bbox_mm: Dict[str, float]  # {x, y, z}
    cell_count: int
    strut_count: int
    min_feature_mm: float
    estimated_weight_g: float
    process_warnings: List[str]
    passed_process_check: bool
    summary: str

