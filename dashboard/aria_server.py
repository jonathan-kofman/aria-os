"""
aria_server.py
FastAPI backend for the ARIA-OS agentic UI.

Endpoints:
  POST /api/generate          — run ARIA pipeline with a goal string
  GET  /api/log/stream        — SSE stream of pipeline events
  GET  /api/parts             — list generated parts from learning_log.json
  GET  /api/parts/{id}/stl    — serve a part's STL file
  GET  /api/sessions          — list session logs from sessions/
  GET  /api/cem               — latest CEM check results from learning_log
"""
import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from aria_os import event_bus

REPO_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="ARIA-OS Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8501"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class GenerateRequest(BaseModel):
    goal: str
    max_attempts: int = 3


# --------------------------------------------------------------------------- #
# Pipeline runner (runs in background thread to keep FastAPI responsive)
# --------------------------------------------------------------------------- #

def _run_pipeline(goal: str, max_attempts: int) -> None:
    from aria_os.orchestrator import run
    try:
        run(goal, repo_root=REPO_ROOT, max_attempts=max_attempts)
    except Exception as e:
        event_bus.emit("error", f"Pipeline error: {e}", {"goal": goal})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.post("/api/generate")
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    """Kick off the ARIA pipeline in a background thread."""
    event_bus.emit("step", f"Received goal: {req.goal[:80]}", {"goal": req.goal})
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_pipeline, req.goal, req.max_attempts)
    return {"status": "started", "goal": req.goal}


@app.get("/api/log/stream")
async def log_stream():
    """SSE endpoint — streams pipeline events as they are emitted."""
    async def generator():
        while True:
            events = await asyncio.get_event_loop().run_in_executor(
                None, event_bus.get_events, 0.5
            )
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
            # Heartbeat every ~10 seconds keeps the connection alive
            yield ": heartbeat\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/api/parts")
async def list_parts():
    """Return all part attempts from the learning log."""
    log_path = REPO_ROOT / "outputs" / "cad" / "learning_log.json"
    if not log_path.exists():
        return {"parts": []}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        # learning_log is a list of attempt records
        if isinstance(data, list):
            return {"parts": data}
        return {"parts": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/parts/{part_id}/stl")
async def get_stl(part_id: str):
    """Serve the STL file for a part."""
    import os
    # Search outputs/cad/stl/ for a matching file
    stl_dir = REPO_ROOT / "outputs" / "cad" / "stl"
    matches = list(stl_dir.glob(f"*{part_id}*.stl")) if stl_dir.exists() else []
    if not matches:
        raise HTTPException(status_code=404, detail=f"No STL found for {part_id}")
    # Return the most recently modified match
    stl_file = max(matches, key=lambda p: p.stat().st_mtime)
    # Path traversal protection: ensure resolved path stays within outputs/
    allowed_prefix = os.path.realpath(str(REPO_ROOT / "outputs"))
    resolved = os.path.realpath(str(stl_file))
    if not resolved.startswith(allowed_prefix + os.sep) and resolved != allowed_prefix:
        raise HTTPException(status_code=403, detail="Access denied: path outside outputs directory")
    return FileResponse(resolved, media_type="model/stl", filename=stl_file.name)


@app.get("/api/sessions")
async def list_sessions():
    """List session log files."""
    sessions_dir = REPO_ROOT / "sessions"
    if not sessions_dir.exists():
        return {"sessions": []}
    files = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "sessions": [
            {"name": f.name, "size_bytes": f.stat().st_size}
            for f in files[:50]
        ]
    }


@app.get("/api/cem")
async def cem_summary():
    """Return the latest CEM results from the learning log."""
    log_path = REPO_ROOT / "outputs" / "cad" / "learning_log.json"
    if not log_path.exists():
        return {"cem": []}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {"cem": []}
        # Return entries that have cem data, most recent first
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------------------------------------------------------------------------- #
# Dev entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.aria_server:app", host="0.0.0.0", port=8000, reload=True)
