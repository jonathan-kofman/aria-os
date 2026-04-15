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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Process-start timestamps used by /api/version
_PROCESS_START_TS = time.time()
_PROCESS_START_ISO = datetime.now(timezone.utc).isoformat()

# In-memory run registry  {run_id: RunRecord}
_runs: dict[str, dict] = {}


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
    if cloud_only:
        all_healthy = cad_ok and mf_ok
    else:
        all_healthy = cad_ok and mf_ok and ss.get("status") == "healthy"

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

    if cmd in ("assemble", "assembly"):
        if not goal:
            raise ValueError("goal is required for assemble")
        return [*_python_cmd(), RUNNER, "--assemble", goal]

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
async def get_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="run not found")
    rec = {k: v for k, v in _runs[run_id].items() if k not in ("lines", "proc")}
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
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index():
    html = STATIC / "index.html"
    if not html.exists():
        return HTMLResponse("<h1>ARIA-OS Dashboard</h1><p>static/index.html not found.</p>")
    return HTMLResponse(html.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ARIA_PORT", 7861))
    print(f"ARIA-OS Dashboard → http://localhost:{port}")
    uvicorn.run("dashboard.dashboard_server:app", host="0.0.0.0", port=port, reload=False)
