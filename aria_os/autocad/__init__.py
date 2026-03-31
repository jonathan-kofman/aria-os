"""
aria_os.autocad — headless AutoCAD/DXF generation for civil engineering.

Disciplines: transportation, drainage, grading, utilities, site, structural, survey
Standards:   AASHTO 7th Ed. + all 50 US states + DC DOT overrides

Quick start
-----------
>>> from aria_os.autocad import generate_civil_dxf
>>> path = generate_civil_dxf("drainage plan", state="TX", discipline="drainage")
>>> print(path)
outputs/cad/dxf/tx_drainage.dxf

>>> from aria_os.autocad import generate_all_disciplines
>>> paths = generate_all_disciplines("CA")   # generates all 5 disciplines for California
"""

from aria_os.autocad.dxf_exporter import generate_civil_dxf, generate_all_disciplines
from aria_os.autocad.layer_manager import LAYER_DEFS, DISCIPLINE_LAYERS, get_layer
from aria_os.autocad.standards_library import (
    get_standard,
    list_standards,
    get_pipe_design,
    check_road_geometry,
    check_ada_compliance,
)

__all__ = [
    # DXF generation
    "generate_civil_dxf",
    "generate_all_disciplines",
    # Layer management
    "LAYER_DEFS",
    "DISCIPLINE_LAYERS",
    "get_layer",
    # Standards
    "get_standard",
    "list_standards",
    "get_pipe_design",
    "check_road_geometry",
    "check_ada_compliance",
]
