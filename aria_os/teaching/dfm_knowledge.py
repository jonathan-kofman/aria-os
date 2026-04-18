"""
DEPRECATION SHIM
================
The DFM/material/geometry knowledge base has moved to `manufacturing_core.knowledge`.

This module is kept as a re-export so existing ariaOS imports keep working.
New code should import from `manufacturing_core.knowledge` directly.
"""
from manufacturing_core.knowledge import (
    DFM_TEACHINGS,
    MATERIAL_TEACHINGS,
    GEOMETRY_TEACHINGS,
    get_dfm_teaching,
    get_material_teaching,
    get_geometry_teaching,
    get_all_dfm_processes,
)

__all__ = [
    "DFM_TEACHINGS",
    "MATERIAL_TEACHINGS",
    "GEOMETRY_TEACHINGS",
    "get_dfm_teaching",
    "get_material_teaching",
    "get_geometry_teaching",
    "get_all_dfm_processes",
]
