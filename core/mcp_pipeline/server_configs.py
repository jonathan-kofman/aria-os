"""
MCP server configuration — discovers which CAD MCP servers are reachable.

Each server config is a dict shaped for the Anthropic MCP API:
    {"type": "url", "url": "...", "name": "...", "env": {...}}

Returns None when a server isn't installed/available so callers can probe
without raising.
"""
from __future__ import annotations

import os
import shutil
from typing import Any


# ---------------------------------------------------------------------------
# Onshape
# ---------------------------------------------------------------------------

def get_onshape_server_config() -> dict[str, Any] | None:
    """Onshape MCP server config — needs ONSHAPE_ACCESS_KEY + ONSHAPE_SECRET_KEY.

    Two ways to run the MCP server:
      - stdio (default): ONSHAPE_MCP_COMMAND launches a local subprocess
      - http/sse: ONSHAPE_MCP_URL points to an already-running server
    """
    access = os.environ.get("ONSHAPE_ACCESS_KEY", "").strip()
    secret = os.environ.get("ONSHAPE_SECRET_KEY", "").strip()
    if not access or not secret:
        return None

    env = {"ONSHAPE_ACCESS_KEY": access, "ONSHAPE_SECRET_KEY": secret}

    # HTTP/SSE preferred when explicit URL is set
    url = os.environ.get("ONSHAPE_MCP_URL", "").strip()
    if url:
        return {"name": "onshape", "transport": "sse", "url": url, "env": env}

    # Otherwise default to stdio subprocess
    cmd = os.environ.get("ONSHAPE_MCP_COMMAND", "onshape-mcp").strip()
    return {
        "name": "onshape",
        "transport": "stdio",
        "command": cmd,
        "args": [],
        "env": env,
    }


# ---------------------------------------------------------------------------
# FreeCAD
# ---------------------------------------------------------------------------

def get_freecad_server_config() -> dict[str, Any] | None:
    """FreeCAD MCP server — requires `freecad-mcp` binary on PATH."""
    cmd = shutil.which("freecad-mcp")
    if not cmd:
        return None
    return {
        "name": "freecad",
        "transport": "stdio",
        "command": cmd,
        "args": [],
    }


# ---------------------------------------------------------------------------
# Rhino
# ---------------------------------------------------------------------------

def get_rhino_server_config() -> dict[str, Any] | None:
    """Rhino MCP server — requires Rhino + RhinoMCP plugin running over HTTP/SSE."""
    rhino_url = os.environ.get("RHINO_MCP_URL", "").strip()
    if not rhino_url:
        return None
    return {
        "name": "rhino",
        "transport": "sse",
        "url": rhino_url,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def get_available_mcp_servers() -> list[dict[str, Any]]:
    """Return all currently-available MCP server configs.

    Returns a list (possibly empty). Callers that need at least one server
    must check `len(get_available_mcp_servers()) > 0` or use is_mcp_available().
    """
    servers: list[dict[str, Any]] = []
    for fn in (get_onshape_server_config,
               get_freecad_server_config,
               get_rhino_server_config):
        try:
            cfg = fn()
            if cfg is not None:
                servers.append(cfg)
        except Exception:
            # Probing should never raise — silently skip a misbehaving probe
            pass
    return servers


def is_mcp_available() -> bool:
    """Quick check: are any MCP CAD servers reachable?"""
    return len(get_available_mcp_servers()) > 0


def get_preferred_server(
    available: list[dict[str, Any]] | None = None,
    preference_order: tuple[str, ...] = ("onshape", "freecad", "rhino"),
) -> dict[str, Any] | None:
    """Pick the most-preferred available server. Returns None if none available."""
    if available is None:
        available = get_available_mcp_servers()
    by_name = {s["name"]: s for s in available}
    for name in preference_order:
        if name in by_name:
            return by_name[name]
    return None
