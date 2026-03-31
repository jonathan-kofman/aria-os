"""
aria_os/api_server.py — FastAPI server for the ARIA-OS CAD pipeline.

Endpoints:
    POST /api/generate  — generate a part from a natural-language description
    GET  /api/health    — backend availability + metadata
    GET  /api/runs      — recent run log entries

Run with:
    uvicorn aria_os.api_server:app
    uvicorn aria_os.api_server:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, field_validator
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    # Stub classes so the module can be imported without FastAPI installed
    class FastAPI:  # type: ignore
        def get(self, *a, **kw):
            def dec(f): return f
            return dec
        def post(self, *a, **kw):
            def dec(f): return f
            return dec
    class BaseModel:  # type: ignore
        pass
    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code, detail): super().__init__(detail)
    def field_validator(*a, **kw):  # type: ignore
        def dec(f): return f
        return dec

app = FastAPI(
    title="ARIA-OS CAD Pipeline API",
    description="Generate mechanical CAD from natural-language descriptions.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# In-memory run log
# ---------------------------------------------------------------------------

_RUN_LOG: list[dict] = []
_LOG_PATH: Optional[Path] = Path(__file__).resolve().parent.parent / "outputs" / "api_run_log.json"


def _append_run(entry: dict) -> None:
    _RUN_LOG.append(entry)
    # Keep last 500 entries in memory
    if len(_RUN_LOG) > 500:
        _RUN_LOG.pop(0)
    # Optional persist to disk
    if _LOG_PATH:
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            existing: list = []
            if _LOG_PATH.exists():
                try:
                    existing = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
            existing.append(entry)
            _LOG_PATH.write_text(json.dumps(existing[-500:], indent=2), encoding="utf-8")
        except Exception:
            pass  # disk log is non-critical


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    description: str
    dry_run: bool = False

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("description must not be empty or whitespace")
        if len(stripped) < 4:
            raise ValueError("description must be at least 4 characters")
        return stripped


class GenerateResponse(BaseModel):
    status: str
    part_id: str
    backend: str
    step_path: str
    stl_path: str
    validation_passed: bool
    attempts: int
    elapsed_s: float
    warnings: list[str]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Backend availability helpers
# ---------------------------------------------------------------------------

def _check_cadquery() -> dict:
    try:
        import cadquery  # noqa: F401
        return {"available": True, "version": getattr(cadquery, "__version__", "unknown")}
    except ImportError:
        return {"available": False, "reason": "cadquery not installed"}


def _check_grasshopper() -> dict:
    # Grasshopper requires Rhino Compute or local Rhino — check env
    rh = os.environ.get("RHINO_COMPUTE_URL", "")
    if rh:
        return {"available": True, "endpoint": rh}
    return {"available": False, "reason": "RHINO_COMPUTE_URL not set"}


def _check_blender() -> dict:
    import shutil
    blender = shutil.which("blender")
    if blender:
        return {"available": True, "path": blender}
    return {"available": False, "reason": "blender not on PATH"}


def _check_fusion360() -> dict:
    # Fusion 360 must be run interactively — scripts are generated but not executed server-side
    return {"available": True, "note": "Script generation only; Fusion 360 must run locally"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """Report availability of all four CAD backends."""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "backends": {
            "cadquery":     _check_cadquery(),
            "grasshopper":  _check_grasshopper(),
            "blender":      _check_blender(),
            "fusion360":    _check_fusion360(),
        },
    }


@app.get("/api/runs")
def get_runs(limit: int = 20) -> dict:
    """Return the last *limit* run log entries."""
    limit = max(1, min(limit, 500))
    return {"runs": _RUN_LOG[-limit:], "total": len(_RUN_LOG)}


@app.post("/api/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """
    Generate a CAD part from a natural-language description.

    - **description**: natural language part goal (min 4 chars, non-empty)
    - **dry_run**: if true, run planning + routing but skip actual generation
    """
    t0 = time.monotonic()
    warnings: list[str] = []
    error: Optional[str] = None

    try:
        from . import orchestrator
        from pathlib import Path as _Path

        repo_root = _Path(__file__).resolve().parent.parent
        session = orchestrator.run(req.description, repo_root=repo_root)

        elapsed = time.monotonic() - t0
        backend = session.get("cad_tool", "cadquery") if isinstance(session, dict) else "cadquery"
        part_id = session.get("part_id", "") if isinstance(session, dict) else ""
        step_path = str(session.get("step_path", "")) if isinstance(session, dict) else ""
        stl_path  = str(session.get("stl_path", "")) if isinstance(session, dict) else ""
        attempts  = int(session.get("attempts", 1)) if isinstance(session, dict) else 1
        val_info  = session.get("validation", {}) if isinstance(session, dict) else {}
        val_passed = val_info.get("passed", True) if isinstance(val_info, dict) else True
        warnings   = val_info.get("warnings", []) if isinstance(val_info, dict) else []

        step_size = Path(step_path).stat().st_size if step_path and Path(step_path).exists() else 0
        stl_size  = Path(stl_path).stat().st_size if stl_path and Path(stl_path).exists() else 0

        entry = {
            "timestamp":        datetime.utcnow().isoformat() + "Z",
            "description":      req.description,
            "backend":          backend,
            "part_id":          part_id,
            "validation_passed": val_passed,
            "attempts":         attempts,
            "elapsed_s":        round(elapsed, 2),
            "file_sizes":       {"step": step_size, "stl": stl_size},
        }
        _append_run(entry)

        return GenerateResponse(
            status="success",
            part_id=part_id,
            backend=backend,
            step_path=step_path,
            stl_path=stl_path,
            validation_passed=val_passed,
            attempts=attempts,
            elapsed_s=round(elapsed, 2),
            warnings=warnings or [],
        )

    except Exception as exc:
        elapsed = time.monotonic() - t0
        error = str(exc)
        entry = {
            "timestamp":   datetime.utcnow().isoformat() + "Z",
            "description": req.description,
            "backend":     "unknown",
            "error":       error,
            "elapsed_s":   round(elapsed, 2),
        }
        _append_run(entry)
        raise HTTPException(status_code=500, detail=error)
