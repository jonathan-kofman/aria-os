"""
MCP-based CAD generation pipeline for ARIA-OS.

Alternative to the CadQuery-code-generation pipeline. The LLM has a live
conversation with a real CAD tool (Onshape / FreeCAD / Rhino) through
Model Context Protocol, iterating on geometry one operation at a time
instead of emitting a single CadQuery script.

Activated by env var:
    ARIA_GENERATION_MODE=mcp        # use MCP exclusively
    ARIA_GENERATION_MODE=auto       # try MCP first, fall back to CadQuery
    ARIA_GENERATION_MODE=cadquery   # default — existing pipeline only

Existing CadQuery pipeline is unchanged. Falls back to it on any MCP failure.
"""
from .mcp_client import MCPCADClient, MCPGenerationResult
from .server_configs import (
    get_available_mcp_servers,
    get_onshape_server_config,
    get_freecad_server_config,
    get_rhino_server_config,
    is_mcp_available,
)
from .cad_orchestrator import generate_via_mcp, run_with_fallback

__all__ = [
    "MCPCADClient",
    "MCPGenerationResult",
    "get_available_mcp_servers",
    "get_onshape_server_config",
    "get_freecad_server_config",
    "get_rhino_server_config",
    "is_mcp_available",
    "generate_via_mcp",
    "run_with_fallback",
]
