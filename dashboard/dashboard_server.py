"""
dashboard_server.py
-------------------
Self-contained FastAPI dashboard for ARIA-OS.

Runs every pipeline command as a subprocess and streams stdout/stderr
line-by-line via SSE.  No deep integration changes to the pipeline.

Start:
    python dashboard/dashboard_server.py
    # → http://localhost:7860
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
# When the dashboard spawns python subprocesses, force unbuffered stdout
# (`python -u`) so output line-streams immediately into the run-record.
# Without this, a quick-exiting subprocess can drop everything written
# in the last 4KB of buffered stdout.
PYTHON_ARGS_PREFIX = ["-u"]
RUNNER = str(REPO_ROOT / "run_aria_os.py")
STATIC = Path(__file__).resolve().parent / "static"
RUN_LOG_PATH = REPO_ROOT / "outputs" / "dashboard_run_log.json"

import logging as _logging
_log = _logging.getLogger("aria_dashboard")

app = FastAPI(title="ARIA-OS Dashboard", version="2.0.0")

# CORS — for Vercel-hosted frontend hitting this Railway backend, set
# ARIA_CORS_ORIGINS=https://aria.vercel.app,https://aria-preview.vercel.app
# in the Railway environment. Defaults to "*" (open) for local dev.
_cors_origins_raw = os.environ.get("ARIA_CORS_ORIGINS", "*").strip()
_cors_origins = (
    [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    if _cors_origins_raw != "*"
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=(_cors_origins != ["*"]),  # credentials only with explicit origins
)

# Process-start timestamps used by /api/version
_PROCESS_START_TS = time.time()
_PROCESS_START_ISO = datetime.now(timezone.utc).isoformat()

# In-memory run registry  {run_id: RunRecord}
_runs: dict[str, dict] = {}

# Max concurrent subprocess runs — prevents OOM on Railway free tier
_MAX_CONCURRENT_RUNS: int = int(os.environ.get("ARIA_MAX_CONCURRENT_RUNS", "5"))


def _load_run_log() -> None:
    """Load persisted run history from disk into _runs on startup."""
    if not RUN_LOG_PATH.is_file():
        return
    try:
        data = json.loads(RUN_LOG_PATH.read_text(encoding="utf-8"))
        loaded = 0
        for run_id, rec in data.items():
            if run_id not in _runs:
                rec.setdefault("lines", [])
                rec.setdefault("proc", None)
                _runs[run_id] = rec
                loaded += 1
        _log.info("Loaded %d run(s) from %s", loaded, RUN_LOG_PATH)
    except Exception as exc:
        _log.warning("Could not load run log: %s", exc)


def _save_run_log() -> None:
    """Persist completed run history to disk (excludes proc, caps lines at 200)."""
    try:
        RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        serializable: dict = {}
        for run_id, rec in _runs.items():
            if rec.get("status") in ("running", "queued"):
                continue  # don't persist in-flight runs
            entry = {k: v for k, v in rec.items() if k not in ("proc", "lines")}
            entry["lines"] = rec.get("lines", [])[-200:]  # cap at 200 lines
            serializable[run_id] = entry
        RUN_LOG_PATH.write_text(json.dumps(serializable, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        _log.warning("Could not save run log: %s", exc)


_load_run_log()


def _warmup_imports() -> dict[str, Any]:
    """
    Pre-import the heavy CAD/scientific packages once at server startup so:

    1. The OS file cache is warm — subprocess `python run_aria_os.py ...`
       loads cadquery / OCP / VTK / matplotlib from page cache, not disk,
       saving 5-10s of cold-start per generation.

    2. If cadquery is broken (libGL.so.1 missing, etc.), the dashboard
       process logs the failure on boot rather than silently degrading
       /api/pipeline/health and waiting for the first user request.

    Wrapped in try/except per-import so a single broken dep doesn't kill
    the dashboard — it just logs and continues.
    """
    results: dict[str, Any] = {}
    targets = ["numpy", "trimesh", "matplotlib.pyplot", "cadquery"]
    t0 = time.time()
    for name in targets:
        try:
            __import__(name)
            results[name] = "ok"
        except Exception as exc:
            results[name] = f"FAIL: {type(exc).__name__}: {exc}"
            _log.warning("warmup: %s failed: %s", name, exc)
    elapsed = round(time.time() - t0, 2)
    results["_elapsed_seconds"] = elapsed
    _log.info("warmup imports finished in %.2fs: %s", elapsed, results)
    return results


# Run warmup at module import (i.e. once per uvicorn worker startup).
# Stored on the module so /api/pipeline/health can surface it.
_WARMUP_RESULTS = _warmup_imports()

# MillForge JWT token store (set via /api/millforge/token or MILLFORGE_JWT env var)
_mf_token: str = os.environ.get("MILLFORGE_JWT", "")


# --------------------------------------------------------------------------- #
# Diagnostic endpoints
# Optimized for fast iteration without Railway round-trips:
#   /api/diag/subprocess    spawns a trivial `python -c print` subprocess and
#                           returns the captured stdout. Isolates whether the
#                           dashboard's subprocess wrapper itself works.
#   /api/diag/run-aria-os   spawns run_aria_os.py with --check (fast, no LLM)
#                           and returns the full stdout. Isolates whether the
#                           ARIA CLI's stdout reaches the wrapper.
#   /api/diag/inproc        runs a tiny cadquery + trimesh ops in-process and
#                           returns the result. No subprocess at all.
# --------------------------------------------------------------------------- #


@app.get("/api/diag/subprocess")
async def diag_subprocess():
    """Spawn `python -c "print('hi')"` and return captured stdout."""
    proc = await asyncio.create_subprocess_exec(
        PYTHON, "-u", "-c",
        "import sys; print('STDOUT:hello'); print('STDERR:bye', file=sys.stderr); sys.stdout.flush()",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "raw_bytes_len": len(stdout),
        "captured": stdout.decode("utf-8", errors="replace"),
    }


@app.get("/api/diag/run-aria-os")
async def diag_run_aria_os():
    """Spawn `run_aria_os.py --check` (fast, no LLM) and return its stdout."""
    proc = await asyncio.create_subprocess_exec(
        PYTHON, "-u", RUNNER, "--check",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    stdout, _ = await proc.communicate()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "raw_bytes_len": len(stdout),
        "stdout": stdout.decode("utf-8", errors="replace"),
    }


@app.get("/api/diag/inproc")
async def diag_inproc():
    """Run cadquery + trimesh in-process. No subprocess. Verifies the kernel works."""
    out: dict[str, Any] = {"checks": {}}
    try:
        import cadquery as cq  # type: ignore
        result = cq.Workplane("XY").box(10, 10, 10)
        bb = result.val().BoundingBox()
        out["checks"]["cadquery"] = {
            "ok": True,
            "version": getattr(cq, "__version__", "?"),
            "bbox": [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)],
        }
    except Exception as exc:
        out["checks"]["cadquery"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        import trimesh  # type: ignore
        mesh = trimesh.creation.box(extents=[1, 1, 1])
        out["checks"]["trimesh"] = {
            "ok": True,
            "triangles": int(len(mesh.faces)),
            "watertight": bool(mesh.is_watertight),
        }
    except Exception as exc:
        out["checks"]["trimesh"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

ARIA_SCHEMA_VERSION = "1.0"

STRUCTSIGHT_URL = os.environ.get("STRUCTSIGHT_URL", "http://localhost:3009")
MILLFORGE_URL = os.environ.get("MILLFORGE_API_URL", "http://localhost:8000")


@app.get("/api/version")
async def version_info():
    """
    Build / git metadata for the running container. Useful to verify
    which deploy is actually serving requests after a Railway rollout.
    Reads RAILWAY_GIT_COMMIT_SHA / RAILWAY_DEPLOYMENT_ID env vars set
    by Railway, plus a process-start timestamp captured at module load.
    """
    return {
        "service": "aria-os-dashboard",
        "schema_version": ARIA_SCHEMA_VERSION,
        "git_commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        "git_branch": os.environ.get("RAILWAY_GIT_BRANCH", "unknown"),
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", "unknown"),
        "started_at": _PROCESS_START_ISO,
        "uptime_seconds": round(time.time() - _PROCESS_START_TS, 1),
        "python_executable": sys.executable,
        "warmup": _WARMUP_RESULTS,
    }


@app.get("/api/pipeline/health")
async def pipeline_health():
    """Aggregated health check across the full StructSight -> ARIA -> MillForge pipeline.

    Probes each system's health endpoint and returns a unified view.
    """
    import urllib.request
    import urllib.error

    def _probe(url: str, timeout: int = 5) -> dict:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return {"status": "healthy", "data": data}
        except urllib.error.URLError as e:
            return {"status": "unreachable", "error": str(e.reason)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    loop = asyncio.get_event_loop()
    # MillForge exposes GET /health (not /api/health). StructSight may vary by app.
    # Skip the StructSight probe entirely when STRUCTSIGHT_URL still points at
    # localhost — that's the default when the env var is unset, and the
    # probe just adds a 5s timeout on serverless deployments where there's
    # no local StructSight to reach. Set STRUCTSIGHT_URL to a real URL to
    # re-enable.
    skip_structsight = "localhost" in STRUCTSIGHT_URL or "127.0.0.1" in STRUCTSIGHT_URL
    if skip_structsight:
        structsight_health = {"status": "not_configured", "url": STRUCTSIGHT_URL}
        millforge_health = await loop.run_in_executor(
            None, _probe, f"{MILLFORGE_URL.rstrip('/')}/health"
        )
    else:
        structsight_health, millforge_health = await asyncio.gather(
            loop.run_in_executor(None, _probe, f"{STRUCTSIGHT_URL}/api/health"),
            loop.run_in_executor(None, _probe, f"{MILLFORGE_URL.rstrip('/')}/health"),
            return_exceptions=True,
        )

    # ARIA's own health
    try:
        from aria_os.api_server import _check_cadquery
        aria_backends = {"cadquery": _check_cadquery()}
    except Exception:
        aria_backends = {"cadquery": {"available": False, "reason": "import failed"}}

    # Check Ollama (skipped entirely in cloud-only mode so the probe doesn't
    # add 3s of timeout latency to every health call on Railway)
    cloud_only = os.environ.get("ARIA_CLOUD_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
    if cloud_only:
        ollama_status = "skipped (cloud_only)"
    else:
        ollama_status = "unavailable"
        try:
            import urllib.request as _ur
            with _ur.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
                if r.status == 200:
                    ollama_status = "available"
        except Exception:
            pass

    ss = structsight_health if isinstance(structsight_health, dict) else {"status": "error", "error": str(structsight_health)}
    mf = millforge_health if isinstance(millforge_health, dict) else {"status": "error", "error": str(millforge_health)}

    # MillForge alone is enough for the pipeline to be considered healthy
    # in cloud-only mode — StructSight is optional and Ollama is intentionally
    # absent. CadQuery being available is what gates real geometry generation.
    cad_ok = aria_backends.get("cadquery", {}).get("available", False)
    mf_ok = mf.get("status") == "healthy"
    # StructSight is "ok" when reachable OR explicitly not configured (skipped
    # because STRUCTSIGHT_URL points at localhost on a serverless deploy).
    ss_ok = ss.get("status") in ("healthy", "not_configured")
    if cloud_only:
        all_healthy = cad_ok and mf_ok
    else:
        all_healthy = cad_ok and mf_ok and ss_ok

    return {
        "pipeline_status": "healthy" if all_healthy else "degraded",
        "systems": {
            "structsight": {
                "url": STRUCTSIGHT_URL,
                **ss,
            },
            "aria_os": {
                "status": "healthy",
                "backends": aria_backends,
                "ollama": ollama_status,
                "cloud_only": cloud_only,
                "schema_version": ARIA_SCHEMA_VERSION,
                "bridge_enabled": bool(os.environ.get("MILLFORGE_API_URL") or os.environ.get("MILLFORGE_JWT")),
            },
            "millforge": {
                "url": MILLFORGE_URL,
                **mf,
            },
        },
        "active_runs": sum(1 for r in _runs.values() if r.get("status") == "running"),
        "total_runs": len(_runs),
    }


@app.get("/schema-version")
async def schema_version():
    """Report the ARIA bridge schema version for MillForge compatibility probing."""
    return {"schema_version": ARIA_SCHEMA_VERSION}


class RunRequest(BaseModel):
    command: str = "generate"   # generate | cam | verify | analyze | quote | draw | list | ecad | assemble | modify | scan
    goal: str = ""
    flags: list[str] = []       # e.g. ["--material", "aluminum", "--machine", "VMC"]
    extra: str = ""             # raw extra CLI text


class VisualizationRequest(BaseModel):
    item_description: str       # what the engineer typed in StructSight
    description: str = ""       # AI-generated description of the visualized result
    suggestions: list[str] = []
    considerations: list[str] = []
    image_base64: str = ""      # base64 JPEG of the annotated before/after image
    image_media_type: str = "image/jpeg"
    trace_id: str = ""          # end-to-end trace ID from StructSight


class MillForgeRequest(BaseModel):
    stl_path: str               # relative path inside outputs/ e.g. "outputs/cad/stl/part.stl"
    material: str = "steel"     # material name — mapped to MillForge MaterialType
    quantity: int = 1
    millforge_url: str = "http://localhost:8000"  # MillForge backend URL


class MillForgeCamRequest(BaseModel):
    cam_dir: str                # relative path to the CAM output dir, e.g. "outputs/cam/llm_bracket_m6_holes"
    millforge_url: str = "http://localhost:8000"
    millforge_token: str = ""   # JWT from MillForge login (or set MILLFORGE_JWT env var)


class MillForgeTokenRequest(BaseModel):
    token: str


def _millforge_ui_base(api_url: str) -> str:
    """Map MillForge API origin (e.g. :8000) to Vite dev UI origin (e.g. :5173)."""
    u = (api_url or "").rstrip("/")
    if ":8000" in u:
        return u.replace(":8000", ":5173", 1)
    return u or "http://localhost:5173"


def _millforge_jobs_deeplink(api_url: str, job_id: int | None = None) -> str:
    """Open Jobs tab and optionally focus a job (session cookie is unchanged — same origin)."""
    base = _millforge_ui_base(api_url).rstrip("/")
    if job_id is not None:
        return f"{base}/?tab=jobs&job={int(job_id)}"
    return f"{base}/?tab=jobs"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _python_cmd() -> list[str]:
    """`python -u` so subprocess stdout streams unbuffered into the run record."""
    return [PYTHON, *PYTHON_ARGS_PREFIX]


def _build_argv(req: RunRequest) -> list[str]:
    """Translate a RunRequest into a run_aria_os.py argv list."""
    cmd = req.command.strip().lower()
    flags = req.flags or []
    goal = req.goal.strip()

    if cmd == "generate":
        # python -u run_aria_os.py "goal text"
        if not goal:
            raise ValueError("goal is required for generate")
        return [*_python_cmd(), RUNNER, goal]

    if cmd == "list":
        return [*_python_cmd(), RUNNER, "--list"]

    if cmd == "validate":
        return [*_python_cmd(), RUNNER, "--validate"]

    if cmd in ("cam",):
        # --cam <step_file> [--material X] [--machine Y]
        if not goal:
            raise ValueError("goal (step file path) is required for cam")
        return [*_python_cmd(), RUNNER, "--cam", goal] + flags

    if cmd == "cam-validate":
        return [*_python_cmd(), RUNNER, "--cam-validate"] + flags

    if cmd == "setup":
        # --setup <step> <cam_script>
        parts = goal.split(None, 1)
        if len(parts) < 2:
            raise ValueError("setup needs: <step_file> <cam_script>")
        return [*_python_cmd(), RUNNER, "--setup", parts[0], parts[1]] + flags

    if cmd in ("verify",):
        if not goal:
            raise ValueError("goal (stl/step path) is required for verify")
        return [*_python_cmd(), RUNNER, "--verify", goal]

    if cmd in ("analyze", "analyze-part"):
        if not goal:
            raise ValueError("goal (step path) is required for analyze")
        return [*_python_cmd(), RUNNER, "--analyze-part", goal]

    if cmd in ("quote",):
        if not goal:
            raise ValueError("goal (step path) is required for quote")
        return [*_python_cmd(), RUNNER, "--quote", goal]

    if cmd in ("draw",):
        if not goal:
            raise ValueError("goal (step path) is required for draw")
        return [*_python_cmd(), RUNNER, "--draw", goal]

    if cmd in ("ecad",):
        if not goal:
            raise ValueError("goal is required for ecad")
        return [*_python_cmd(), RUNNER, "--ecad", goal]

    if cmd in ("autocad",):
        if not goal:
            raise ValueError("goal is required for autocad")
        return [*_python_cmd(), RUNNER, "--autocad", goal]

    if cmd == "assemble":
        # --assemble <config.json>  (config file path required)
        if not goal:
            raise ValueError("goal (config.json path) is required for assemble")
        return [*_python_cmd(), RUNNER, "--assemble", goal]

    if cmd == "assembly":
        # --assembly "<natural-language description>"
        if not goal:
            raise ValueError("goal (NL description) is required for assembly")
        return [*_python_cmd(), RUNNER, "--assembly", goal]

    if cmd in ("optimize",):
        if not goal:
            raise ValueError("goal is required for optimize")
        return [*_python_cmd(), RUNNER, "--optimize", goal]

    if cmd in ("modify",):
        parts = goal.split(None, 1)
        if len(parts) < 2:
            raise ValueError("modify needs: <script.py> <change description>")
        return [*_python_cmd(), RUNNER, "--modify", parts[0], parts[1]]

    if cmd in ("view",):
        if not goal:
            raise ValueError("goal (step path) is required for view")
        return [*_python_cmd(), RUNNER, "--view", goal]

    if cmd in ("image",):
        if not goal:
            raise ValueError("goal (stl path) is required for image")
        return [*_python_cmd(), RUNNER, "--image", goal]

    if cmd in ("cem", "cem-full"):
        if not goal:
            raise ValueError("goal is required for cem")
        return [*_python_cmd(), RUNNER, "--cem-full", goal]

    if cmd in ("cem-advise",):
        if not goal:
            raise ValueError("goal is required for cem-advise")
        return [*_python_cmd(), RUNNER, "--cem-advise", goal]

    if cmd in ("material-study",):
        if not goal:
            raise ValueError("goal (step path) is required for material-study")
        return [*_python_cmd(), RUNNER, "--material-study", goal]

    if cmd in ("lattice",):
        if not goal:
            raise ValueError("goal is required for lattice")
        return [*_python_cmd(), RUNNER, "--lattice", goal]

    if cmd in ("scenario",):
        if not goal:
            raise ValueError("goal is required for scenario")
        return [*_python_cmd(), RUNNER, "--scenario", goal]

    if cmd in ("system",):
        if not goal:
            raise ValueError("goal is required for system")
        return [*_python_cmd(), RUNNER, "--system", goal]

    # Specialized flows that the verification agent found unmapped.
    # Each was being silently passed as a bare goal to run_aria_os.py,
    # which routed it through the default CAD pipeline instead of the
    # specialized handler. Adding explicit mappings here is the
    # highest-impact fix from the 2026-04-15 verification round.

    if cmd == "terrain":
        if not goal:
            raise ValueError("goal (NL description) is required for terrain")
        return [*_python_cmd(), RUNNER, "--terrain", goal]

    if cmd == "scan":
        if not goal:
            raise ValueError("goal (mesh file path) is required for scan")
        return [*_python_cmd(), RUNNER, "--scan", goal] + flags

    if cmd in ("catalog-search", "catalog_search"):
        if not goal:
            raise ValueError("goal (query) is required for catalog-search")
        return [*_python_cmd(), RUNNER, "--catalog-search", goal]

    if cmd == "catalog":
        # --catalog [--topology X] [--search Y] [--tags Z]
        return [*_python_cmd(), RUNNER, "--catalog"] + flags

    if cmd == "scan-dir":
        if not goal:
            raise ValueError("goal (directory path) is required for scan-dir")
        return [*_python_cmd(), RUNNER, "--scan-dir", goal] + flags

    if cmd == "reconstruct":
        if not goal:
            raise ValueError("goal (catalog id) is required for reconstruct")
        return [*_python_cmd(), RUNNER, "--reconstruct", goal]

    if cmd == "image-full":
        if not goal:
            raise ValueError("goal (image path) is required for image-full")
        return [*_python_cmd(), RUNNER, "--image-full", goal] + flags

    if cmd == "refine":
        if not goal:
            raise ValueError("goal (script path + refinement) is required for refine")
        parts = goal.split(None, 1)
        if len(parts) < 2:
            raise ValueError("refine needs: <script.py> <refinement description>")
        return [*_python_cmd(), RUNNER, "--refine", parts[0], parts[1]]

    if cmd == "review":
        if not goal:
            raise ValueError("goal (file path) is required for review")
        return [*_python_cmd(), RUNNER, "--review", goal] + flags

    if cmd == "review-view":
        return [*_python_cmd(), RUNNER, "--review-view"] + flags

    if cmd == "scenario":
        if not goal:
            raise ValueError("goal (NL situation) is required for scenario")
        return [*_python_cmd(), RUNNER, "--scenario", goal] + flags

    if cmd == "scenario-dry-run":
        if not goal:
            raise ValueError("goal is required for scenario-dry-run")
        return [*_python_cmd(), RUNNER, "--scenario-dry-run", goal]

    if cmd == "system-dry-run":
        if not goal:
            raise ValueError("goal is required for system-dry-run")
        return [*_python_cmd(), RUNNER, "--system-dry-run", goal]

    if cmd == "material-study-all":
        return [*_python_cmd(), RUNNER, "--material-study-all"] + flags

    if cmd == "ecad-to-enclosure":
        if not goal:
            raise ValueError("goal (kicad project path) is required for ecad-to-enclosure")
        return [*_python_cmd(), RUNNER, "--ecad-to-enclosure", goal] + flags

    if cmd == "ecad-variants":
        if not goal:
            raise ValueError("goal (kicad project path) is required for ecad-variants")
        return [*_python_cmd(), RUNNER, "--ecad-variants", goal]

    if cmd == "constrain":
        if not goal:
            raise ValueError("goal (config.json) is required for constrain")
        return [*_python_cmd(), RUNNER, "--constrain", goal] + flags

    if cmd == "print-scale":
        if not goal:
            raise ValueError("goal (part stub) is required for print-scale")
        return [*_python_cmd(), RUNNER, "--print-scale", goal] + flags

    if cmd == "optimize-and-regenerate":
        if not goal:
            raise ValueError("goal is required for optimize-and-regenerate")
        return [*_python_cmd(), RUNNER, "--optimize-and-regenerate", goal] + flags

    if cmd == "lattice-test":
        return [*_python_cmd(), RUNNER, "--lattice-test"] + flags

    if cmd == "generate-and-assemble":
        if not goal:
            raise ValueError("goal is required for generate-and-assemble")
        return [*_python_cmd(), RUNNER, "--generate-and-assemble", goal] + flags

    if cmd == "check":
        return [*_python_cmd(), RUNNER, "--check"]

    # Fallback: treat as raw goal
    if goal:
        return [*_python_cmd(), RUNNER, goal]
    raise ValueError(f"Unknown command: {cmd}")


def _scan_outputs() -> list[dict]:
    """Walk outputs/ and return a structured file list."""
    out_dir = REPO_ROOT / "outputs"
    if not out_dir.exists():
        return []
    files = []
    _skip = {'.gitkeep', '.gitignore', 'Thumbs.db', '.DS_Store'}
    for p in sorted(out_dir.rglob("*"), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
        if not p.is_file() or p.name in _skip:
            continue
        rel = p.relative_to(REPO_ROOT)
        ext = p.suffix.lower()
        kind = (
            "step" if ext == ".step" else
            "stl"  if ext == ".stl"  else
            "svg"  if ext == ".svg"  else
            "png"  if ext == ".png"  else
            "dxf"  if ext == ".dxf"  else
            "json" if ext == ".json" else
            "py"   if ext == ".py"   else
            "md"   if ext == ".md"   else
            "other"
        )
        files.append({
            "path": str(rel).replace("\\", "/"),
            "name": p.name,
            "kind": kind,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
        })
        if len(files) >= 500:
            break
    return files


# --------------------------------------------------------------------------- #
# Run management
# --------------------------------------------------------------------------- #

async def _stream_subprocess(run_id: str, argv: list[str]) -> None:
    """Run subprocess, capture output line by line, store in run record."""
    rec = _runs[run_id]
    rec["status"] = "running"
    rec["lines"] = []
    rec["start"] = time.time()
    rec["proc"] = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(REPO_ROOT),
        )
        rec["pid"] = proc.pid
        rec["proc"] = proc
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            rec["lines"].append({"t": time.time(), "text": line})
            if rec.get("stop_requested"):
                proc.kill()
                break
        await proc.wait()
        rec["returncode"] = proc.returncode
        if rec.get("stop_requested"):
            rec["status"] = "stopped"
        else:
            rec["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception as e:
        rec["lines"].append({"t": time.time(), "text": f"[ERROR] {e}"})
        rec["status"] = "error"
    finally:
        rec["proc"] = None
        rec["end"] = time.time()
        _save_run_log()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.post("/api/run")
async def start_run(req: RunRequest):
    try:
        argv = _build_argv(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = str(uuid.uuid4())[:8]
    _runs[run_id] = {
        "id": run_id,
        "command": req.command,
        "goal": req.goal,
        "argv": argv,
        "status": "queued",
        "lines": [],
        "returncode": None,
        "start": time.time(),
        "end": None,
    }

    # Rate limit — refuse if too many runs are already in-flight
    active = sum(1 for r in _runs.values() if r.get("status") == "running")
    if active >= _MAX_CONCURRENT_RUNS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent runs ({active}/{_MAX_CONCURRENT_RUNS}). "
                   "Wait for an existing run to finish."
        )

    # Fire and forget in event loop
    asyncio.create_task(_stream_subprocess(run_id, argv))
    return {"run_id": run_id, "argv": argv}


@app.get("/api/run/{run_id}/stream")
async def stream_run(run_id: str):
    """SSE: stream stdout/stderr lines as they arrive."""
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="run not found")

    async def generator():
        sent = 0
        while True:
            rec = _runs[run_id]
            lines = rec["lines"]
            while sent < len(lines):
                ev = json.dumps({"text": lines[sent]["text"]})
                yield f"data: {ev}\n\n"
                sent += 1

            status = rec["status"]
            if status in ("done", "failed", "error") and sent >= len(lines):
                yield f"data: {json.dumps({'done': True, 'status': status, 'returncode': rec.get('returncode')})}\n\n"
                return

            yield ": heartbeat\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.post("/api/run/{run_id}/stop")
async def stop_run(run_id: str):
    """Kill a running subprocess."""
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="run not found")
    rec = _runs[run_id]
    if rec["status"] not in ("running", "queued"):
        return {"status": rec["status"], "detail": "already finished"}
    rec["stop_requested"] = True
    proc = rec.get("proc")
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
    rec["lines"].append({"t": time.time(), "text": "[STOPPED] Run killed by user."})
    rec["status"] = "stopped"
    rec["end"] = time.time()
    return {"status": "stopped"}


@app.get("/api/run/{run_id}")
async def get_run(run_id: str, include_lines: bool = True, limit: int = 500):
    """
    Fetch a run record. By default the response includes the last `limit`
    captured stdout/stderr lines; pass include_lines=false to skip them
    when polling status only. Cap defaults to 500 lines to avoid huge
    payloads on long runs.
    """
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="run not found")
    raw = _runs[run_id]
    rec = {k: v for k, v in raw.items() if k != "proc"}
    if include_lines:
        lines = raw.get("lines", []) or []
        rec["lines"] = lines[-limit:] if limit and len(lines) > limit else lines
        rec["lines_total"] = len(lines)
    else:
        rec.pop("lines", None)
    return rec


@app.get("/api/runs")
async def list_runs():
    runs = []
    for rec in reversed(list(_runs.values())):
        runs.append({
            "id": rec["id"],
            "command": rec["command"],
            "goal": rec["goal"][:80],
            "status": rec["status"],
            "returncode": rec.get("returncode"),
            "elapsed": round((rec["end"] or time.time()) - rec["start"], 1),
        })
    return {"runs": runs[:50]}


@app.get("/api/outputs")
async def list_outputs():
    return {"files": _scan_outputs()}


@app.get("/api/file")
async def get_file(path: str):
    """Serve a file from the repo by relative path (outputs/ only)."""
    resolved = (REPO_ROOT / path).resolve()
    allowed = (REPO_ROOT / "outputs").resolve()
    if not str(resolved).startswith(str(allowed)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(resolved), filename=resolved.name)


@app.get("/api/bundle")
async def download_bundle(path: str):
    """Stream a directory of artifacts as a ZIP. *path* is the directory
    relative to outputs/. Used by the UI's 'Download all' button so users
    don't have to click each STEP/STL/SVG/JSON individually.
    """
    import io
    import zipfile
    from fastapi.responses import StreamingResponse

    resolved = (REPO_ROOT / path).resolve()
    allowed = (REPO_ROOT / "outputs").resolve()
    if not str(resolved).startswith(str(allowed)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    # Generator: stream zip bytes so we don't allocate the whole thing in RAM.
    def iter_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fp in resolved.rglob("*"):
                if not fp.is_file():
                    continue
                arc = fp.relative_to(resolved)
                zf.write(str(fp), arcname=str(arc))
        buf.seek(0)
        yield buf.read()

    bundle_name = f"{resolved.name}.zip"
    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_name}"'},
    )


# Pre-canned drone build presets — exposed so the frontend can show
# "Quick Build" launcher buttons (5" FPV, 7" Long Range, Military Recon).
# Each preset POSTs to /api/run with a synthesized goal that the orchestrator
# routes to drone_quad / drone_quad_military.
DRONE_PRESETS = {
    "5inch_fpv": {
        "label": "5\" FPV Racer",
        "description": "5-inch X-frame quadcopter, racing setup",
        "goal": "drone_quad 5inch FPV racer 220mm diagonal 5\" tri-blade props",
        "estimated_seconds": 30,
        "outputs": ["assembly STEP/STL", "22 part STEPs", "BOM", "render"],
    },
    "7inch_long_range": {
        "label": "7\" Long Range",
        "description": "7-inch X-frame, longer arms for endurance",
        "goal": "drone_quad 7inch long range 295mm diagonal 7\" tri-blade props",
        "estimated_seconds": 32,
        "outputs": ["assembly STEP/STL", "22 part STEPs", "BOM", "render"],
    },
    "military_recon": {
        "label": "Military Recon (7\")",
        "description": "Armored recon drone — vision pod, fiber tether, GPS, payload rail",
        "goal": "drone_recon_military_7inch full pipeline",
        "estimated_seconds": 45,
        "outputs": ["31-part assembly", "FC + ESC KiCad PCBs", "8 GD&T drawings",
                    "populated PCB STEPs", "print bundle for Centauri Carbon"],
    },
}


@app.get("/api/presets")
async def list_presets():
    """Return the catalog of pre-canned drone builds for UI launcher tiles."""
    return {"presets": DRONE_PRESETS}


@app.post("/api/preset/{preset_id}")
async def run_preset(preset_id: str):
    """Run a pre-canned preset by id. Returns {run_id, preset_id, goal}.

    Drone presets call into drone_quad / drone_quad_military directly rather
    than the legacy orchestrator, since those modules produce the full
    multi-domain output (mechanical + ECAD + drawings) in one call.
    """
    preset = DRONE_PRESETS.get(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Unknown preset: {preset_id}")

    run_id = uuid.uuid4().hex[:12]

    def _run_drone():
        """Use the unified build_pipeline so the user gets the FULL bundle:
        mechanical + ECAD + drawings + slicer-ready prints + CAM scripts +
        preview thumbnails. Single call, single result, single ZIP at the end.
        """
        try:
            from aria_os.build_pipeline import run_full_build
            result = run_full_build(preset_id=preset_id)
            return result.to_dict()
        except Exception as exc:
            import traceback
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }

    # Kick off in a background thread; client polls /api/preset/run/{run_id}
    import threading
    state = {"status": "running", "preset": preset_id, "started_at": time.time()}
    _PRESET_RUNS[run_id] = state

    def worker():
        result = _run_drone()
        state.update({"status": "done", "result": result, "ended_at": time.time()})

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id, "preset_id": preset_id, "preset": preset}


# In-memory preset run registry. Lost on restart — that's fine for short jobs.
_PRESET_RUNS: dict[str, dict] = {}


@app.get("/api/preset/run/{run_id}")
async def get_preset_run(run_id: str):
    """Poll a preset run's status. Returns {status: 'running'|'done', ...}."""
    state = _PRESET_RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return state


@app.get("/api/graph/status")
async def graph_status():
    """Graphify integration health check — is the codebase graph built?"""
    try:
        from aria_os.graphify_setup import status
        return status()
    except Exception as exc:
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/graph/build")
async def graph_build(force: bool = False):
    """Build / refresh the codebase knowledge graph. Returns ok/error.

    Idempotent — skips rebuild if no source files changed since last build,
    unless force=True.
    """
    try:
        from aria_os.graphify_setup import build_codebase_graph
        return build_codebase_graph(force=force)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}")


@app.get("/api/graph/query")
async def graph_query(q: str, limit: int = 20):
    """Query the codebase knowledge graph by free-text query.

    Used by external LLM agents (and the UI) to do cheap structural lookups
    over the pipeline source. Returns matched nodes (file paths, function
    names, class names) with relevance scores.

    Falls back to a grep-style match against the graph JSON if no semantic
    search is configured (graphify supports both).
    """
    try:
        from aria_os.graphify_setup import GRAPH_DIR
        import json as _json
        graph_path = GRAPH_DIR / "codebase.json"
        if not graph_path.is_file():
            raise HTTPException(status_code=404,
                                detail="codebase graph not built — POST /api/graph/build first")
        data = _json.loads(graph_path.read_text(encoding="utf-8"))
        nodes = data.get("nodes", [])
        ql = q.lower()
        matches = []
        for n in nodes:
            txt = " ".join(str(v) for v in n.values() if isinstance(v, str)).lower()
            if ql in txt:
                matches.append(n)
                if len(matches) >= limit:
                    break
        return {"query": q, "n_matches": len(matches),
                "n_nodes_total": len(nodes), "matches": matches}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}")


# Build the codebase graph on startup if Graphify is installed (cheap when
# cached). Logs but doesn't fail server boot if graphify isn't there.
@app.on_event("startup")
async def _build_graph_on_startup():
    try:
        from aria_os.graphify_setup import build_codebase_graph, _has_graphify
        if not _has_graphify():
            print("[graph] graphify not installed — skipping graph build")
            return
        result = build_codebase_graph()
        if result.get("ok"):
            print(f"[graph] codebase graph: {result.get('n_nodes')} nodes "
                  f"({'cached' if result.get('cached') else 'built'})")
        else:
            print(f"[graph] build failed: {result.get('error')}")
    except Exception as exc:
        print(f"[graph] startup hook failed: {type(exc).__name__}: {exc}")


@app.get("/api/preset/run/{run_id}/preview")
async def get_preset_preview(run_id: str):
    """Return the 'what's in the box' thumbnail manifest for a completed run.

    Frontend uses this to render the preview tile grid (assembly render +
    GD&T drawing thumbnails) before user downloads the ZIP.
    """
    state = _PRESET_RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if state.get("status") != "done":
        return {"status": state.get("status"), "preview_artifacts": []}
    result = state.get("result") or {}
    return {
        "status": "done",
        "preview_artifacts": result.get("preview_artifacts", []),
        "stages": result.get("stages", {}),
        "output_dir": result.get("output_dir"),
    }


@app.post("/api/millforge/token")
async def set_millforge_token(req: MillForgeTokenRequest):
    """Store a MillForge JWT for use by /api/send-cam-to-millforge."""
    global _mf_token
    _mf_token = req.token.strip()
    return {"status": "ok", "has_token": bool(_mf_token)}


@app.get("/api/millforge/token")
async def get_millforge_token_status():
    return {"has_token": bool(_mf_token), "millforge_ui": "http://localhost:5173", "aria_port": 7861}


@app.post("/api/send-cam-to-millforge")
async def send_cam_to_millforge(req: MillForgeCamRequest):
    """
    Read the ARIA-OS setup_sheet.json from a CAM output dir and POST it to
    MillForge POST /api/jobs/import-from-cam (requires JWT auth).

    Returns the created MillForge Job record.
    """
    import urllib.request as _ur, urllib.error

    # Resolve path
    full_dir = (REPO_ROOT / req.cam_dir).resolve()
    allowed = (REPO_ROOT / "outputs").resolve()
    if not str(full_dir).startswith(str(allowed)):
        raise HTTPException(status_code=403, detail="Path outside outputs/")

    setup_json = full_dir / "setup_sheet.json"
    if not setup_json.is_file():
        raise HTTPException(status_code=404, detail=f"setup_sheet.json not found in {req.cam_dir}")

    payload = json.loads(setup_json.read_text(encoding="utf-8"))

    # Use token from request, fallback to env/stored token
    token = req.millforge_token.strip() or _mf_token
    if not token:
        raise HTTPException(
            status_code=401,
            detail="No MillForge JWT token. Log in to MillForge and save the token via POST /api/millforge/token"
        )

    base = req.millforge_url.rstrip("/")
    try:
        mf_req = _ur.Request(
            f"{base}/api/jobs/import-from-cam",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with _ur.urlopen(mf_req, timeout=15) as r:
            job = json.loads(r.read())
        jid = job.get("id")
        ui = _millforge_ui_base(base)
        return {
            "status": "created",
            "job": job,
            "millforge_ui": ui,
            "millforge_job_url": _millforge_jobs_deeplink(base, jid),
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        detail = json.loads(body).get("detail", body[:200]) if body.startswith("{") else body[:200]
        raise HTTPException(status_code=502, detail=f"MillForge error {e.code}: {detail}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MillForge unreachable: {e}")


@app.post("/api/send-to-millforge")
async def send_to_millforge(req: MillForgeRequest):
    """
    Upload the finished STL to MillForge /api/orders/from-cad (no auth needed),
    then hit /api/quote to get an instant price, and return everything in one shot.
    """
    import os, urllib.request as _ur, urllib.error

    # Security: resolve and confirm path is inside outputs/
    full = (REPO_ROOT / req.stl_path).resolve()
    allowed = (REPO_ROOT / "outputs").resolve()
    if not str(full).startswith(str(allowed)):
        raise HTTPException(status_code=403, detail="Path outside outputs/")
    if not full.is_file():
        raise HTTPException(status_code=404, detail=f"STL not found: {req.stl_path}")

    stl_bytes = full.read_bytes()

    # Map ARIA material names → MillForge MaterialType enum
    _mat_map = {
        "steel_mild": "steel", "steel_stainless": "steel", "steel": "steel",
        "aluminum_6061": "aluminum", "aluminum": "aluminum",
        "titanium": "titanium",
        "brass": "copper", "copper": "copper",
        "abs_plastic": "aluminum", "pla_plastic": "aluminum",  # best-effort fallback
        "nylon": "aluminum",
    }
    mf_material = _mat_map.get(req.material.lower(), "steel")
    base = req.millforge_url.rstrip("/")

    # ── Step 1: Upload STL → extract dimensions/complexity ──────────────────
    cad_result: dict = {}
    try:
        boundary = "AriaBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{full.name}"\r\n'
            f"Content-Type: model/stl\r\n\r\n"
        ).encode() + stl_bytes + f"\r\n--{boundary}--\r\n".encode()

        cad_req = _ur.Request(
            f"{base}/api/orders/from-cad",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with _ur.urlopen(cad_req, timeout=15) as r:
            cad_result = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"MillForge from-cad error: {e.code} {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MillForge unreachable: {e}. Is it running on {base}?")

    # ── Step 2: Get instant quote ────────────────────────────────────────────
    quote_result: dict = {}
    try:
        dims = cad_result.get("dimensions", "100x100x10mm")
        quote_payload = json.dumps({
            "material": mf_material,
            "dimensions": dims,
            "quantity": req.quantity,
            "priority": 5,
        }).encode()
        qreq = _ur.Request(
            f"{base}/api/quote",
            data=quote_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(qreq, timeout=10) as r:
            quote_result = json.loads(r.read())
    except Exception:
        pass  # quote is best-effort; cad_result is enough to show useful info

    ui = _millforge_ui_base(base)
    return {
        "stl_file": full.name,
        "material": mf_material,
        "quantity": req.quantity,
        "cad": cad_result,
        "quote": quote_result,
        "millforge_ui": ui,
        "millforge_jobs_url": _millforge_jobs_deeplink(base, None),
    }


@app.post("/api/from-visualization")
async def from_visualization(req: VisualizationRequest):
    """
    Accept a StructSight visualization result and start an ARIA-OS generate run.

    The full vision analysis (description, suggestions, considerations, image)
    is saved as a context JSON file that the coordinator reads to enrich the
    pipeline — not just the goal string.
    """
    goal = req.item_description.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="item_description is required")

    # Keep the goal concise — the full context is saved to the JSON file
    # and injected by the coordinator during spec extraction.
    # The goal should be a short, actionable CAD description (≤300 chars).
    MAX_GOAL = 300

    if len(goal) > MAX_GOAL:
        goal = goal[:MAX_GOAL].rsplit(" ", 1)[0]

    run_id = str(uuid.uuid4())[:8]

    # Write StructSight context file so the coordinator can read it
    context_dir = REPO_ROOT / "workspace" / "structsight"
    context_dir.mkdir(parents=True, exist_ok=True)
    context_file = context_dir / f"{run_id}_context.json"
    trace_id = req.trace_id or run_id  # use StructSight's trace_id if provided
    structsight_context = {
        "run_id": run_id,
        "trace_id": trace_id,
        "item_description": req.item_description,
        "description": req.description,
        "suggestions": req.suggestions,
        "considerations": req.considerations,
        "has_image": bool(req.image_base64),
        "image_media_type": req.image_media_type,
    }
    try:
        context_file.write_text(json.dumps(structsight_context, indent=2), encoding="utf-8")
        # Save image separately if present (large data)
        if req.image_base64:
            img_file = context_dir / f"{run_id}_image.b64"
            img_file.write_text(req.image_base64, encoding="utf-8")
    except Exception as exc:
        _log.warning("Could not save StructSight context: %s", exc)

    # Build CLI argv with --structsight-context flag
    run_req = RunRequest(command="generate", goal=goal)
    try:
        argv = _build_argv(run_req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _runs[run_id] = {
        "id": run_id,
        "command": "generate",
        "goal": goal,
        "source": "structsight",
        "item_description": req.item_description,
        "structsight_context": structsight_context,
        "site_image_b64": req.image_base64 or "",
        "site_image_type": req.image_media_type,
        "argv": argv,
        "status": "queued",
        "lines": [],
        "returncode": None,
        "start": time.time(),
        "end": None,
    }

    asyncio.create_task(_stream_subprocess(run_id, argv))
    return {"run_id": run_id, "goal": goal}


@app.get("/api/run/{run_id}/status")
async def get_run_status(run_id: str):
    """Lightweight status endpoint for external polling (e.g. StructSight).

    Returns run status, geometry validation result, and output file paths
    without the full line buffer.
    """
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="run not found")

    rec = _runs[run_id]
    result: dict = {
        "run_id": run_id,
        "status": rec["status"],
        "command": rec.get("command", ""),
        "goal": rec.get("goal", ""),
        "elapsed_s": round((rec.get("end") or time.time()) - rec.get("start", time.time()), 1),
        "returncode": rec.get("returncode"),
    }

    # Extract generation results + current stage from output lines
    lines_text = [l.get("text", "") for l in rec.get("lines", [])]
    phase_labels = {
        "[Phase 1]": "Researching materials & standards",
        "[Phase 2]": "Building geometry spec",
        "[Phase 3]": "Generating CAD geometry",
        "[Phase 4]": "Running CAM + simulation",
        "[Phase 5]": "Final assembly & MillForge",
    }
    current_stage = None
    for line in lines_text:
        for marker, label in phase_labels.items():
            if marker in line:
                current_stage = label
    result["current_stage"] = current_stage

    for line in reversed(lines_text):
        if "STEP:" in line and "KB" in line:
            result["step_path"] = line.split("STEP:")[-1].strip().split("(")[0].strip()
        if "STL:" in line and "KB" in line:
            result["stl_path"] = line.split("STL:")[-1].strip().split("(")[0].strip()
        if "Geometry:" in line:
            result["geometry_passed"] = "PASS" in line

    # Include MillForge feedback if available
    if rec.get("millforge_feedback"):
        result["millforge_feedback"] = rec["millforge_feedback"]

    return result


class MillForgeFeedback(BaseModel):
    aria_job_id: str
    actual_cycle_time_minutes: float | None = None
    qc_passed: bool | None = None
    defects_found: list[str] = []
    feedback_notes: str = ""
    stage: str = ""


@app.post("/api/bridge/callback")
async def receive_millforge_feedback(feedback: MillForgeFeedback):
    """Receive post-completion feedback from MillForge.

    Stores feedback on the matching run record so the dashboard can display
    manufacturing outcomes alongside generation results.
    """
    # Find the run by aria_job_id (which maps to our run_id system)
    matched_run = None
    for run_id, rec in _runs.items():
        if rec.get("id") == feedback.aria_job_id:
            matched_run = rec
            break
        # Also check millforge_job metadata
        mf_job = rec.get("millforge_job", {})
        if mf_job.get("aria_job_id") == feedback.aria_job_id:
            matched_run = rec
            break

    if matched_run is None:
        raise HTTPException(status_code=404, detail=f"No run found for aria_job_id='{feedback.aria_job_id}'")

    matched_run["millforge_feedback"] = {
        "actual_cycle_time_minutes": feedback.actual_cycle_time_minutes,
        "qc_passed": feedback.qc_passed,
        "defects_found": feedback.defects_found,
        "feedback_notes": feedback.feedback_notes,
        "stage": feedback.stage,
        "received_at": time.time(),
    }
    _save_run_log()

    _log.info("MillForge feedback received for run %s: qc_passed=%s stage=%s",
              feedback.aria_job_id, feedback.qc_passed, feedback.stage)

    return {"status": "ok", "aria_job_id": feedback.aria_job_id}


# ---------------------------------------------------------------------------
# QC Memory endpoints — MillForge pushes defect feedback; ARIA reads history
# ---------------------------------------------------------------------------

class _QCFeedbackPayload(BaseModel):
    aria_job_id: str
    part_type: str = ""
    material: str = ""
    defects_found: list[str] = []
    qc_passed: bool = True


@app.post("/api/memory/qc-feedback")
async def receive_qc_feedback(payload: _QCFeedbackPayload):
    """Called by MillForge after QC runs on an ARIA-sourced job.
    Stores the outcome so future generations can avoid the same defects.
    """
    try:
        from aria_os.agents.memory import record_qc_feedback
        record_qc_feedback(
            part_type=payload.part_type,
            material=payload.material,
            defects=payload.defects_found,
            qc_passed=payload.qc_passed,
            aria_job_id=payload.aria_job_id,
        )
        # Also store on the matching run record for dashboard display
        for rec in _runs.values():
            mf_job = rec.get("millforge_job", {})
            if mf_job.get("aria_job_id") == payload.aria_job_id or rec.get("id") == payload.aria_job_id:
                rec.setdefault("qc_memory", []).append({
                    "defects": payload.defects_found,
                    "qc_passed": payload.qc_passed,
                    "part_type": payload.part_type,
                    "material": payload.material,
                })
                break
        _save_run_log()
        _log.info("QC feedback stored: aria_job_id=%s passed=%s defects=%s",
                  payload.aria_job_id, payload.qc_passed, payload.defects_found)
        return {"status": "ok", "stored": True}
    except Exception as exc:
        _log.warning("QC feedback storage failed: %s", exc)
        return {"status": "error", "detail": str(exc)}


@app.get("/api/memory/qc-summary")
async def qc_memory_summary(material: str = "", part_type: str = "", limit: int = 20):
    """Return recent QC failure history, optionally filtered by material/part_type."""
    try:
        import json as _json
        from pathlib import Path as _Path
        mem_dir = _Path(__file__).resolve().parent.parent / "data" / "memory"
        log_path = mem_dir / "qc_feedback.jsonl"
        if not log_path.exists():
            return {"entries": [], "total": 0}
        entries = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = _json.loads(line)
            except Exception:
                continue
            if material and e.get("material", "") != material.lower():
                continue
            if part_type and part_type.lower() not in e.get("part_type", ""):
                continue
            entries.append(e)
        entries = entries[-limit:]
        return {"entries": list(reversed(entries)), "total": len(entries)}
    except Exception as exc:
        return {"entries": [], "total": 0, "error": str(exc)}


# --------------------------------------------------------------------------- #
# React frontend compatibility routes
# Used by the Vite dashboard at https://aria-os-dashboard.vercel.app/.
# Mirrors the route surface in dashboard/aria_server.py so the React SPA
# can talk to this server without requiring a separate process.
# --------------------------------------------------------------------------- #


class _GenerateRequest(BaseModel):
    goal: str
    max_attempts: int = 3


@app.post("/api/generate")
async def react_generate(req: _GenerateRequest):
    """
    Compat shim for the React SPA. Delegates to the existing `start_run`
    handler so all run registry / streaming machinery keeps working.
    """
    return await start_run(RunRequest(command="generate", goal=req.goal))


@app.post("/api/generate-from-image")
async def react_generate_from_image(
    image: UploadFile = File(...),
    goal: str = Form(""),
):
    """
    Image-to-CAD entry point used by the React SPA's photo upload UI.
    Saves the upload to outputs/uploads/<uuid>.ext, then spawns the
    same image pipeline the CLI exposes via `--image`. Returns a
    run_id the SPA can poll via /api/run/{run_id}.
    """
    if not image.filename:
        raise HTTPException(status_code=400, detail="image filename missing")

    suffix = Path(image.filename).suffix.lower() or ".jpg"
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        raise HTTPException(status_code=400, detail=f"unsupported image type: {suffix}")

    upload_dir = REPO_ROOT / "outputs" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    image_id = uuid.uuid4().hex[:12]
    image_path = upload_dir / f"{image_id}{suffix}"
    try:
        contents = await image.read()
        if not contents:
            raise HTTPException(status_code=400, detail="empty image upload")
        image_path.write_bytes(contents)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to persist upload: {exc}")

    # Spawn `python run_aria_os.py --image <photo> [hint]`
    argv = [*_python_cmd(), RUNNER, "--image", str(image_path)]
    if goal.strip():
        argv.append(goal.strip())

    run_id = str(uuid.uuid4())[:8]
    _runs[run_id] = {
        "id": run_id,
        "command": "image",
        "goal": goal or f"image:{image.filename}",
        "argv": argv,
        "status": "queued",
        "lines": [],
        "returncode": None,
        "start": time.time(),
        "end": None,
        "image_path": str(image_path),
    }
    asyncio.create_task(_stream_subprocess(run_id, argv))
    return {
        "status": "started",
        "run_id": run_id,
        "image_id": image_id,
        "message": f"image pipeline started for {image.filename}",
    }


@app.get("/api/parts")
async def react_list_parts():
    """List generated parts from the learning log (used by the React SPA)."""
    log_path = REPO_ROOT / "outputs" / "cad" / "learning_log.json"
    if not log_path.is_file():
        return {"parts": []}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        return {"parts": data if isinstance(data, list) else []}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/parts/{part_id}/stl")
async def react_get_stl(part_id: str):
    """Serve the STL file for a part (best-effort glob match in outputs/cad/stl/)."""
    stl_dir = REPO_ROOT / "outputs" / "cad" / "stl"
    matches = list(stl_dir.glob(f"*{part_id}*.stl")) if stl_dir.is_dir() else []
    if not matches:
        raise HTTPException(status_code=404, detail=f"No STL found for {part_id}")
    stl_file = max(matches, key=lambda p: p.stat().st_mtime)
    # Path-traversal guard
    allowed_prefix = os.path.realpath(str(REPO_ROOT / "outputs"))
    resolved = os.path.realpath(str(stl_file))
    if not resolved.startswith(allowed_prefix + os.sep) and resolved != allowed_prefix:
        raise HTTPException(status_code=403, detail="Access denied")
    return FileResponse(resolved, media_type="model/stl", filename=stl_file.name)


@app.get("/api/sessions")
async def react_list_sessions():
    """List session-log files used by the React SPA."""
    sessions_dir = REPO_ROOT / "sessions"
    if not sessions_dir.is_dir():
        return {"sessions": []}
    files = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "sessions": [
            {"name": f.name, "size_bytes": f.stat().st_size}
            for f in files[:50]
        ]
    }


@app.get("/api/cem")
async def react_cem_summary():
    """Latest CEM check results from the learning log."""
    log_path = REPO_ROOT / "outputs" / "cad" / "learning_log.json"
    if not log_path.is_file():
        return {"cem": []}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {"cem": []}
        cem_entries = [
            {
                "part_id":    e.get("part_id"),
                "goal":       e.get("goal", ""),
                "cem_passed": e.get("cem_passed"),
                "timestamp":  e.get("timestamp", ""),
            }
            for e in reversed(data)
            if "cem_passed" in e
        ]
        return {"cem": cem_entries[:100]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Serve static files (index.html etc.)
# IMPORTANT: mount more-specific paths first. Starlette matches mounts in
# insertion order, so /static/uploads must come before /static.
_UPLOADS_DIR = REPO_ROOT / "outputs" / "uploads"
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/uploads", StaticFiles(directory=str(_UPLOADS_DIR)), name="uploads")

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index():
    html = STATIC / "index.html"
    if not html.exists():
        return HTMLResponse("<h1>ARIA-OS Dashboard</h1><p>static/index.html not found.</p>")
    return HTMLResponse(html.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# PWA root-level aliases
# Service workers registered at /sw.js get site-wide scope. The manifest is
# also aliased at the root so tools that probe "/manifest.json" find it.
# ---------------------------------------------------------------------------

@app.get("/sw.js")
async def pwa_service_worker():
    sw = STATIC / "sw.js"
    if not sw.is_file():
        raise HTTPException(status_code=404, detail="sw.js not found")
    # Service-Worker-Allowed lets the SW control the whole origin even when
    # served from a nested path (belt-and-suspenders: we already serve it
    # from root here, but StaticFiles at /static/sw.js benefits too).
    return FileResponse(
        str(sw),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.json")
async def pwa_manifest():
    mf = STATIC / "manifest.json"
    if not mf.is_file():
        raise HTTPException(status_code=404, detail="manifest.json not found")
    return FileResponse(str(mf), media_type="application/manifest+json")


# ---------------------------------------------------------------------------
# Mobile: printable run summary
# Renders a static HTML page for the latest (or specified) run that formats
# cleanly when saved as PDF or printed from a phone. No JS, no sockets.
# ---------------------------------------------------------------------------

def _html_escape(s: Any) -> str:
    try:
        from html import escape as _esc
        return _esc(str(s), quote=True)
    except Exception:
        return str(s)


@app.get("/print")
async def print_summary(run_id: str = ""):
    """
    Printable summary for a run. If run_id is omitted, uses the most recent run.
    Renders plain HTML with generous margins and no dark background so it
    prints/saves to PDF cleanly from a mobile browser.
    """
    if not _runs:
        return HTMLResponse("<h1>No runs yet</h1><p>Start a run from the dashboard, then reload.</p>", status_code=200)

    if run_id:
        rec = _runs.get(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="run not found")
    else:
        # Most recent by start time
        rec = max(_runs.values(), key=lambda r: r.get("start", 0))

    lines = rec.get("lines", []) or []
    last_lines = lines[-120:]  # cap for print
    start = rec.get("start")
    end = rec.get("end") or time.time()
    elapsed = round(end - start, 1) if start else None
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat() if start else "?"

    # Pull useful artifact paths out of lines (mirrors /api/run/{id}/status logic)
    step_path = stl_path = None
    geom_pass = None
    for ln in reversed([l.get("text", "") for l in lines]):
        if step_path is None and "STEP:" in ln and "KB" in ln:
            step_path = ln.split("STEP:")[-1].strip().split("(")[0].strip()
        if stl_path is None and "STL:" in ln and "KB" in ln:
            stl_path = ln.split("STL:")[-1].strip().split("(")[0].strip()
        if geom_pass is None and "Geometry:" in ln:
            geom_pass = "PASS" in ln
        if step_path and stl_path and geom_pass is not None:
            break

    rows = [
        ("Run ID",       rec.get("id", "?")),
        ("Command",      rec.get("command", "?")),
        ("Goal",         rec.get("goal", "")),
        ("Status",       rec.get("status", "?")),
        ("Return code", rec.get("returncode")),
        ("Started (UTC)", start_iso),
        ("Elapsed (s)",  elapsed),
        ("STEP",         step_path or "-"),
        ("STL",          stl_path or "-"),
        ("Geometry",     "PASS" if geom_pass else ("FAIL" if geom_pass is False else "-")),
    ]

    rows_html = "\n".join(
        f"<tr><th style='text-align:left;padding:4px 10px;background:#f4f4f4;border:1px solid #ddd;'>{_html_escape(k)}</th>"
        f"<td style='padding:4px 10px;border:1px solid #ddd;font-family:monospace;'>{_html_escape(v) if v is not None else '-'}</td></tr>"
        for k, v in rows
    )

    log_html = _html_escape("\n".join(l.get("text", "") for l in last_lines)) or "(no output)"

    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ARIA-OS Run {_html_escape(rec.get('id', ''))}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#111;
         background:#fff; max-width: 820px; margin: 24px auto; padding: 0 16px; font-size:14px; }}
  h1 {{ font-size: 20px; margin: 0 0 8px 0; }}
  h2 {{ font-size: 16px; margin: 24px 0 8px 0; border-bottom:1px solid #ccc; padding-bottom:4px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  pre {{ background:#f7f7f7; border:1px solid #ddd; padding:10px; font-size:12px;
         overflow-x:auto; white-space:pre-wrap; word-break:break-word; max-height:none; }}
  .muted {{ color:#666; font-size:12px; }}
  @media print {{
    body {{ margin: 0; }}
    pre  {{ font-size: 10px; }}
    a[href]:after {{ content: ""; }}
  }}
</style>
</head><body>
<h1>ARIA-OS Run Summary</h1>
<div class="muted">Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}</div>
<h2>Run</h2>
<table>{rows_html}</table>
<h2>Tail of log (last {len(last_lines)} lines)</h2>
<pre>{log_html}</pre>
<div class="muted">ARIA-OS Dashboard &middot; run_id={_html_escape(rec.get('id', ''))}</div>
</body></html>
"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Mobile: generic file upload (STL / STEP)
# Saves to outputs/uploads/<uuid>.<ext> and returns the viewer URL.
# ---------------------------------------------------------------------------

_ALLOWED_UPLOAD_EXTS = {".stl", ".step", ".stp", ".obj", ".3mf", ".ply"}


@app.post("/api/upload")
async def upload_mesh(file: UploadFile = File(...)):
    """
    Accept an STL/STEP upload from mobile, save to outputs/uploads/<uuid>.<ext>,
    and return a URL the dashboard Three.js viewer can load.

    Note: files in outputs/uploads/ are served via /api/file?path=..., which
    already guards against path traversal. A /static/uploads/ alias is also
    mounted for direct URL-style access (simpler on mobile).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename missing")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type: {suffix}. Allowed: {sorted(_ALLOWED_UPLOAD_EXTS)}",
        )

    upload_dir = REPO_ROOT / "outputs" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_id = uuid.uuid4().hex[:12]
    out_path = upload_dir / f"{file_id}{suffix}"
    try:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty upload")
        out_path.write_bytes(data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to persist upload: {exc}")

    rel = f"outputs/uploads/{out_path.name}"
    return {
        "status": "ok",
        "id": file_id,
        "filename": file.filename,
        "size_bytes": out_path.stat().st_size,
        "path": rel,
        "url":         f"/static/uploads/{out_path.name}",   # direct file URL (see mount below)
        "api_file_url": f"/api/file?path={rel}",             # guarded relative-path URL
    }


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ARIA_PORT", 7861))
    print(f"ARIA-OS Dashboard -> http://localhost:{port}")
    # Pass the app object directly instead of an import-string so the script
    # runs whether or not the `dashboard` package is on sys.path. (Running
    # with `python dashboard/dashboard_server.py` makes the script's own
    # directory the cwd-of-import, not the repo root, so the import string
    # form fails with `ModuleNotFoundError: No module named 'dashboard'`.)
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
