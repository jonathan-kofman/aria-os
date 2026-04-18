"""
Graphify integration — build a knowledge graph over the ARIA-OS codebase
and run-output bundles, expose it as an MCP server so LLM agents can query
prior runs / cross-reference parts instead of re-reading raw files.

Why:
  - Prompt → spec_extractor → CadQuery template → KiCad → CAM is a deep
    pipeline. Debugging "why did X happen" means grep across 100+ files.
    Graphify's tree-sitter graph maps the call structure for cheap queries.
  - ARIA outputs are heterogeneous (STEP/STL/KiCad/SVG/PDF/PNG/gcode).
    Graphify ingests images + PDFs natively, so a post-run index makes the
    bundle queryable via MCP.
  - 71x token reduction claim (Graphify's own benchmark) means the visual-
    verify and spec-extraction agents can hit MCP for prior run lookups
    instead of re-reading raw artifacts.

Two operations:
  build_codebase_graph()  — index aria_os/ source (one-time / per deploy)
  build_outputs_graph()   — index outputs/ bundles (per pipeline run)

The serve commands are documented for the user to run manually since they
spawn long-lived processes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_DIR = REPO_ROOT / ".graphify"


def _has_graphify() -> bool:
    """Check that the graphify CLI / Python module is installed."""
    try:
        import graphify  # noqa: F401
        return True
    except ImportError:
        return shutil.which("graphify") is not None


def build_codebase_graph(*, force: bool = False) -> dict[str, Any]:
    """Build a knowledge graph of aria_os/ + dashboard/ + frontend/src/ source.

    Run once at deploy time (or when the codebase changes substantially).
    Output: REPO_ROOT/.graphify/codebase.json — point an MCP server at it.

    Returns {ok: bool, graph_path: str, n_nodes: int, error?: str}.
    """
    if not _has_graphify():
        return {
            "ok": False,
            "error": "graphify not installed. `pip install graphifyy`",
            "install_hint": f"{sys.executable} -m pip install graphifyy",
        }

    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    graph_path = GRAPH_DIR / "codebase.json"

    if graph_path.is_file() and not force:
        # Check stale-ness: if any source file is newer than the graph, rebuild
        graph_mtime = graph_path.stat().st_mtime
        source_dirs = [REPO_ROOT / "aria_os", REPO_ROOT / "dashboard",
                       REPO_ROOT / "frontend" / "src"]
        newest = max(
            (f.stat().st_mtime for d in source_dirs if d.is_dir()
             for f in d.rglob("*") if f.is_file()),
            default=0,
        )
        if newest <= graph_mtime:
            try:
                data = json.loads(graph_path.read_text(encoding="utf-8"))
                return {
                    "ok": True,
                    "graph_path": str(graph_path),
                    "n_nodes": len(data.get("nodes", [])),
                    "cached": True,
                }
            except Exception:
                pass  # corrupted — rebuild

    try:
        # Run graphify CLI on the source dirs
        cmd = [
            sys.executable, "-m", "graphify", "build",
            "--root", str(REPO_ROOT),
            "--include", "aria_os",
            "--include", "dashboard",
            "--include", "frontend/src",
            "--exclude", "outputs",
            "--exclude", "node_modules",
            "--exclude", ".git",
            "--exclude", "__pycache__",
            "--output", str(graph_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        if result.returncode != 0:
            return {
                "ok": False,
                "error": result.stderr[:500] or result.stdout[:500],
                "cmd": " ".join(cmd),
            }
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "graph_path": str(graph_path),
            "n_nodes": len(data.get("nodes", [])),
            "n_edges": len(data.get("edges", [])),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "graphify build timed out (>300s)"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def build_outputs_graph(output_dir: str | Path, *,
                        run_id: str | None = None) -> dict[str, Any]:
    """Index a single pipeline run's output bundle into a Graphify graph.

    Call after a build completes (e.g. from build_pipeline.run_full_build's
    final stage). The graph captures STEP↔STL↔drawing↔BOM relationships so
    the visual-verify agent can query the bundle structure cheaply.

    Returns the same shape as build_codebase_graph().
    """
    if not _has_graphify():
        return {"ok": False, "error": "graphify not installed"}

    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return {"ok": False, "error": f"output_dir not found: {output_dir}"}

    graph_path = output_dir / ".graphify" / f"run_{run_id or 'latest'}.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        cmd = [
            sys.executable, "-m", "graphify", "build",
            "--root", str(output_dir),
            "--output", str(graph_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr[:500]}
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "graph_path": str(graph_path),
            "n_nodes": len(data.get("nodes", [])),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def mcp_serve_command(graph_path: str | Path | None = None) -> str:
    """Return the shell command to start the Graphify MCP server for a graph.

    Doesn't actually start it (long-lived process — spawn from systemd or a
    process manager). User runs this when they want LLM agents to query the
    graph via MCP.
    """
    g = Path(graph_path) if graph_path else (GRAPH_DIR / "codebase.json")
    return f"{sys.executable} -m graphify.serve {g}"


def status() -> dict[str, Any]:
    """One-shot health check of the Graphify integration. Used by /api endpoint."""
    info: dict[str, Any] = {"installed": _has_graphify()}
    if not info["installed"]:
        info["install_hint"] = f"{sys.executable} -m pip install graphifyy"
        return info
    info["graph_dir"] = str(GRAPH_DIR)
    if (GRAPH_DIR / "codebase.json").is_file():
        try:
            data = json.loads((GRAPH_DIR / "codebase.json").read_text(encoding="utf-8"))
            info["codebase_graph"] = {
                "path": str(GRAPH_DIR / "codebase.json"),
                "n_nodes": len(data.get("nodes", [])),
                "n_edges": len(data.get("edges", [])),
            }
        except Exception as exc:
            info["codebase_graph_error"] = str(exc)
    info["mcp_serve_cmd"] = mcp_serve_command()
    return info


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Graphify integration for ARIA-OS")
    p.add_argument("--build-codebase", action="store_true",
                   help="Build/refresh the codebase knowledge graph")
    p.add_argument("--build-outputs", metavar="DIR",
                   help="Index a single output bundle directory")
    p.add_argument("--status", action="store_true",
                   help="Print current Graphify integration status")
    p.add_argument("--force", action="store_true",
                   help="Rebuild even if cache is fresh")
    args = p.parse_args()

    if args.status:
        print(json.dumps(status(), indent=2))
    elif args.build_codebase:
        print(json.dumps(build_codebase_graph(force=args.force), indent=2))
    elif args.build_outputs:
        print(json.dumps(build_outputs_graph(args.build_outputs), indent=2))
    else:
        p.print_help()
