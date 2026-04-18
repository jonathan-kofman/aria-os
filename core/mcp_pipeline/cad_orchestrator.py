"""
MCP-vs-CadQuery routing — the single entry point that ariaOS calls.

`generate_via_mcp(goal)` runs the MCP pipeline. If `mode=auto` and MCP fails,
falls back to the existing CadQuery pipeline transparently.

Designed to be called from `aria_os/orchestrator.py` with one conditional
branch (`if mode == "mcp": run_mcp_pipeline(goal)`). All existing CadQuery
code remains the default and unchanged.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from .mcp_client import MCPCADClient, MCPGenerationResult
from .server_configs import get_available_mcp_servers, get_preferred_server

logger = logging.getLogger(__name__)


def get_generation_mode() -> str:
    """Resolve the active generation mode from env. Defaults to 'cadquery'."""
    return os.environ.get("ARIA_GENERATION_MODE", "cadquery").lower()


def generate_via_mcp(
    goal: str,
    *,
    material: str | None = None,
    target_process: str | None = None,
    extra_constraints: str | None = None,
    preferred_server: str | None = None,
) -> MCPGenerationResult:
    """
    Run the MCP CAD pipeline once. Returns an MCPGenerationResult with success
    flag, output paths, and conversation log. Does NOT fall back to CadQuery —
    use run_with_fallback() for that.
    """
    available = get_available_mcp_servers()
    if not available:
        return MCPGenerationResult(
            success=False,
            error=("No MCP CAD servers reachable. Set ONSHAPE_ACCESS_KEY + "
                   "ONSHAPE_SECRET_KEY for Onshape, install freecad-mcp, or "
                   "set RHINO_MCP_URL for Rhino."),
        )

    if preferred_server:
        chosen = next((s for s in available if s["name"] == preferred_server), None)
        if chosen is None:
            return MCPGenerationResult(
                success=False,
                error=f"Requested MCP server '{preferred_server}' not available. "
                      f"Available: {[s['name'] for s in available]}",
            )
        servers_to_use = [chosen]
    else:
        # Use the preferred-order pick (Onshape > FreeCAD > Rhino)
        chosen = get_preferred_server(available)
        servers_to_use = [chosen] if chosen else available

    client = MCPCADClient(mcp_servers=servers_to_use)
    return client.generate_part_sync(
        goal=goal,
        material=material,
        target_process=target_process,
        extra_constraints=extra_constraints,
    )


def run_with_fallback(
    goal: str,
    *,
    cadquery_fn: Callable[..., Any],
    cadquery_kwargs: dict[str, Any] | None = None,
    material: str | None = None,
    target_process: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """
    Single entry point used by orchestrator.py.

    - mode="cadquery"  -> calls cadquery_fn directly (existing pipeline, unchanged)
    - mode="mcp"       -> tries MCP only; returns failure if MCP unavailable
    - mode="auto"      -> tries MCP first, falls back to cadquery_fn on failure

    Always returns a dict with at minimum {"backend": "...", "success": bool, ...}.
    The cadquery_fn return value is preserved when CadQuery wins.
    """
    mode = (mode or get_generation_mode()).lower()
    cadquery_kwargs = cadquery_kwargs or {}

    if mode == "cadquery":
        result = cadquery_fn(**cadquery_kwargs)
        return _normalize_cadquery_result(result, backend="cadquery")

    if mode in ("mcp", "auto"):
        mcp_result = generate_via_mcp(
            goal=goal, material=material, target_process=target_process,
        )
        if mcp_result.success:
            return {
                "backend": f"mcp:{mcp_result.server_used}",
                "success": True,
                "step_path": mcp_result.step_path,
                "stl_path": mcp_result.stl_path,
                "native_path": mcp_result.native_path,
                "n_tool_calls": mcp_result.n_tool_calls,
                "elapsed_s": mcp_result.elapsed_s,
                "model": mcp_result.model_used,
                "conversation_log": mcp_result.conversation_log,
            }
        # MCP failed
        if mode == "mcp":
            # No fallback requested — return failure
            return {
                "backend": "mcp",
                "success": False,
                "error": mcp_result.error,
                "elapsed_s": mcp_result.elapsed_s,
            }
        # mode == "auto" — fall back to CadQuery
        logger.warning(
            "MCP generation failed (%s) — falling back to CadQuery", mcp_result.error
        )
        result = cadquery_fn(**cadquery_kwargs)
        out = _normalize_cadquery_result(result, backend="cadquery")
        out["mcp_attempt"] = mcp_result.to_dict()
        return out

    # Unknown mode
    return {
        "backend": "unknown",
        "success": False,
        "error": f"Unknown ARIA_GENERATION_MODE='{mode}'. "
                 "Valid: cadquery | mcp | auto",
    }


def _normalize_cadquery_result(result: Any, *, backend: str) -> dict[str, Any]:
    """Coerce whatever the CadQuery generator returns into a uniform dict."""
    if isinstance(result, dict):
        out = dict(result)
        out.setdefault("backend", backend)
        out.setdefault(
            "success",
            bool(out.get("step_path") or out.get("stl_path")),
        )
        return out
    return {"backend": backend, "success": bool(result), "raw": result}
