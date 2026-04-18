"""
MCP CAD client — Claude + MCP CAD server tool-call loop.

The Anthropic Python SDK (≥0.86) does NOT accept `mcp_servers=` directly on
`messages.create()`. The integration pattern is:

    1. Connect to the MCP server using the `mcp` Python package
    2. Get its tool list via session.list_tools()
    3. Pass tools to Anthropic via `tools=`
    4. When Claude returns a `tool_use` block, call the MCP server with that
       tool, get the result, append a `tool_result` block to the conversation,
       and call `messages.create` again until Claude stops requesting tools

This module implements that loop. Requires `pip install mcp anthropic`.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any


def _ensure_anthropic():
    try:
        import anthropic
        return anthropic
    except ImportError as exc:
        raise ImportError(
            "MCP pipeline requires anthropic SDK. "
            "Install with `pip install -U anthropic`."
        ) from exc


def _ensure_mcp_sdk():
    try:
        import mcp  # type: ignore
        return mcp
    except ImportError as exc:
        raise ImportError(
            "MCP pipeline requires the `mcp` Python package. "
            "Install with `pip install mcp`."
        ) from exc


@dataclass
class MCPGenerationResult:
    """Outcome of one MCP-driven CAD generation."""
    success: bool
    step_path: str | None = None
    stl_path: str | None = None
    native_path: str | None = None
    server_used: str = ""
    model_used: str = ""
    n_tool_calls: int = 0
    elapsed_s: float = 0.0
    conversation_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    raw_response: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "step_path": self.step_path,
            "stl_path": self.stl_path,
            "native_path": self.native_path,
            "server_used": self.server_used,
            "model_used": self.model_used,
            "n_tool_calls": self.n_tool_calls,
            "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
            "log_lines": len(self.conversation_log),
        }


def _scan_for_artifact_paths(text: str) -> dict[str, str]:
    """Find STEP/STL/native CAD file paths in arbitrary text — tolerant scanner.

    MCP servers report exported file paths in tool_result blocks, sometimes as
    JSON, sometimes as plain text. This function extracts what it can find.
    """
    import re
    paths: dict[str, str] = {}
    pattern = re.compile(
        r"""(?ix)
        (?:["'\s>=]|^)
        (
          (?:[a-z]:[\\/]|[\\/]|\.{1,2}[\\/])?
          [^"'\s<>:|*?]+\.(?:step|stp|stl|sldprt|f3d|3dm|fcstd|x_t|iges|igs)
        )
        """
    )
    for match in pattern.finditer(text):
        path = match.group(1).strip()
        lower = path.lower()
        if lower.endswith((".step", ".stp")) and "step_path" not in paths:
            paths["step_path"] = path
        elif lower.endswith(".stl") and "stl_path" not in paths:
            paths["stl_path"] = path
        elif "native_path" not in paths:
            paths["native_path"] = path
    return paths


def extract_paths_from_response(response: Any) -> dict[str, str]:
    """Walk an Anthropic response for tool_result blocks reporting file paths."""
    paths: dict[str, str] = {}
    try:
        content = getattr(response, "content", []) or []
    except Exception:
        return paths

    for block in content:
        block_type = getattr(block, "type", "")
        if block_type == "tool_result":
            raw = getattr(block, "content", "")
            if isinstance(raw, list):
                raw = " ".join(
                    (getattr(c, "text", "") or
                     (c.get("text", "") if isinstance(c, dict) else ""))
                    for c in raw
                )
            for k, v in _scan_for_artifact_paths(str(raw)).items():
                paths.setdefault(k, v)
        elif block_type == "tool_use":
            tool_input = getattr(block, "input", {}) or {}
            for v in tool_input.values():
                if isinstance(v, str):
                    for k, p in _scan_for_artifact_paths(v).items():
                        paths.setdefault(k, p)
        elif block_type == "text":
            txt = getattr(block, "text", "") or ""
            for k, p in _scan_for_artifact_paths(txt).items():
                paths.setdefault(k, p)
    return paths


class MCPCADClient:
    """Claude + MCP CAD server orchestration via manual tool-call loop."""

    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        mcp_servers: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        max_iterations: int = 25,
    ):
        if not mcp_servers:
            raise ValueError("MCPCADClient requires at least one MCP server config")
        self.mcp_servers = mcp_servers
        self.model = model or os.environ.get("MCP_MODEL", self.DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations

    def generate_part_sync(
        self,
        goal: str,
        *,
        material: str | None = None,
        target_process: str | None = None,
        extra_constraints: str | None = None,
    ) -> MCPGenerationResult:
        """Synchronous wrapper. Manages its own event loop."""
        try:
            return asyncio.run(self.generate_part(
                goal=goal, material=material,
                target_process=target_process,
                extra_constraints=extra_constraints,
            ))
        except RuntimeError as exc:
            return MCPGenerationResult(
                success=False,
                error=f"sync wrapper inside running event loop: {exc}. "
                      "Call generate_part() directly from async context.",
            )
        except Exception as exc:
            return MCPGenerationResult(
                success=False,
                error=f"sync wrapper raised {type(exc).__name__}: {exc}",
            )

    async def generate_part(
        self,
        goal: str,
        *,
        material: str | None = None,
        target_process: str | None = None,
        extra_constraints: str | None = None,
    ) -> MCPGenerationResult:
        """Run one MCP CAD generation conversation."""
        from .prompts import build_system_prompt

        t0 = time.monotonic()
        try:
            anthropic = _ensure_anthropic()
        except ImportError as exc:
            return MCPGenerationResult(
                success=False, error=str(exc), elapsed_s=time.monotonic() - t0,
            )
        try:
            _ensure_mcp_sdk()
        except ImportError as exc:
            return MCPGenerationResult(
                success=False, error=str(exc), elapsed_s=time.monotonic() - t0,
            )

        try:
            tools, mcp_session = await self._connect_and_list_tools(self.mcp_servers[0])
        except Exception as exc:
            return MCPGenerationResult(
                success=False,
                error=f"MCP server connection failed: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - t0,
            )

        system_prompt = build_system_prompt(
            material=material, target_process=target_process,
            extra_constraints=extra_constraints,
        )
        client = anthropic.Anthropic()
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
        log: list[dict[str, Any]] = []
        n_tool_calls = 0
        last_response = None

        try:
            for iteration in range(self.max_iterations):
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
                last_response = response
                stop_reason = getattr(response, "stop_reason", None)
                content = list(response.content) if response.content else []
                messages.append({"role": "assistant", "content": content})

                tool_uses = [b for b in content if getattr(b, "type", "") == "tool_use"]
                if not tool_uses or stop_reason != "tool_use":
                    break

                tool_results = []
                for tu in tool_uses:
                    n_tool_calls += 1
                    name = getattr(tu, "name", "")
                    inputs = getattr(tu, "input", {}) or {}
                    log.append({"iteration": iteration, "type": "tool_use",
                                "name": name, "input": inputs})
                    try:
                        result_text = await self._call_mcp_tool(mcp_session, name, inputs)
                    except Exception as exc:
                        result_text = f"ERROR: {type(exc).__name__}: {exc}"
                    log.append({"iteration": iteration, "type": "tool_result",
                                "content": result_text[:500]})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": getattr(tu, "id", ""),
                        "content": result_text,
                    })
                messages.append({"role": "user", "content": tool_results})
        except Exception as exc:
            return MCPGenerationResult(
                success=False,
                error=f"MCP loop failed: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - t0,
                conversation_log=log,
                n_tool_calls=n_tool_calls,
            )
        finally:
            try:
                await self._disconnect(mcp_session)
            except Exception:
                pass

        elapsed = time.monotonic() - t0
        # Scan ALL responses (not just last) for artifact paths — paths usually
        # appear in tool_result blocks of intermediate turns, not the final text.
        paths: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                # Iterate over Anthropic content blocks
                for block in content:
                    block_type = getattr(block, "type", None) or (
                        block.get("type", "") if isinstance(block, dict) else ""
                    )
                    if block_type == "tool_result":
                        raw = getattr(block, "content", None) or (
                            block.get("content", "") if isinstance(block, dict) else ""
                        )
                        if isinstance(raw, list):
                            raw = " ".join(
                                (getattr(c, "text", "") or
                                 (c.get("text", "") if isinstance(c, dict) else ""))
                                for c in raw
                            )
                        for k, v in _scan_for_artifact_paths(str(raw)).items():
                            paths.setdefault(k, v)
        success = bool(paths.get("step_path") or paths.get("stl_path")
                       or paths.get("native_path"))

        return MCPGenerationResult(
            success=success,
            step_path=paths.get("step_path"),
            stl_path=paths.get("stl_path"),
            native_path=paths.get("native_path"),
            server_used=self.mcp_servers[0].get("name", "unknown"),
            model_used=self.model,
            n_tool_calls=n_tool_calls,
            elapsed_s=elapsed,
            conversation_log=log,
            error=None if success else "no exported geometry path found in MCP conversation",
            raw_response=last_response,
        )

    # ----------------------------------------------------------------------
    # MCP server connection — multiple transports
    # ----------------------------------------------------------------------

    async def _connect_and_list_tools(
        self, server_cfg: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], Any]:
        """Connect to an MCP server and return (tools_for_anthropic, session)."""
        from mcp import ClientSession  # type: ignore
        transport = server_cfg.get("transport", "stdio")

        if transport == "stdio":
            from mcp.client.stdio import stdio_client, StdioServerParameters  # type: ignore
            params = StdioServerParameters(
                command=server_cfg.get("command", ""),
                args=server_cfg.get("args", []),
                env=server_cfg.get("env"),
            )
            cm = stdio_client(params)
            read, write = await cm.__aenter__()
        elif transport in ("sse", "http"):
            from mcp.client.sse import sse_client  # type: ignore
            url = server_cfg.get("url", "")
            cm = sse_client(url)
            read, write = await cm.__aenter__()
        else:
            raise ValueError(
                f"Unknown MCP transport: {transport}. Supported: stdio, sse, http."
            )

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        tools_response = await session.list_tools()

        tools_for_anthropic = []
        for tool in getattr(tools_response, "tools", []):
            tools_for_anthropic.append({
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "input_schema": getattr(tool, "inputSchema", {"type": "object"}),
            })
        return tools_for_anthropic, session

    async def _call_mcp_tool(
        self, session: Any, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Invoke a tool on the MCP server. Returns the result content as text."""
        result = await session.call_tool(tool_name, tool_input)
        parts: list[str] = []
        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts) if parts else str(result)

    async def _disconnect(self, session: Any) -> None:
        try:
            await session.__aexit__(None, None, None)
        except Exception:
            pass
