"""
aria_os.visual_qa — reusable visual verification framework.

Takes feature outputs (DXF, STL, JSON, HTTP endpoints) and produces
visual artifacts (PNG renders) plus structured pass/fail signals with
confidence scores. Modeled on ``aria_os/visual_verifier.py`` which does
the same thing for CAD output via vision LLMs.

Subcomponents:
    dxf_renderer  — render DXF flat-patterns to PNG via matplotlib
    stl_renderer  — thin wrapper around the existing STL view renderer
    dxf_verify    — deterministic checks against sheet-metal DXF outputs
    cli           — ``python -m aria_os.visual_qa <subcommand>``

All verifier functions in this package follow the never-raise contract:
they return a ``dict`` with at minimum an ``ok: bool`` key and an
``error`` key on failure.
"""

from .dxf_renderer import render_dxf
from .dxf_verify import verify_sheet_metal_dxf
from .stl_renderer import render_stl

__all__ = [
    "render_dxf",
    "render_stl",
    "verify_sheet_metal_dxf",
]
