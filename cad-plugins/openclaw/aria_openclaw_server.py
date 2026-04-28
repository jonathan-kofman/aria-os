r"""aria_openclaw_server.py — OpenClaw bridge for the ARIA-OS shop floor.

Mirrors the SW (7501) / Rhino (7502) / KiCad (7505) HTTP listener contract so
the orchestrator can dispatch fabrication jobs to a Pi running OpenClaw the
same way it dispatches CAD ops: POST /op {kind, params} -> JSON.

OpenClaw itself is an LLM-agent runtime that lives on the Pi. This bridge
exposes the *interface* ARIA needs; the agent consumes job manifests and
calls underlying machine drivers (Klipper / OctoPrint / Marlin for 3D print,
LinuxCNC / CNCjs / grblHAL for CNC). Each driver is a swappable stub here.

Design rules carried over from the other bridges:
  - Bind to BOTH localhost and 127.0.0.1 (Windows HTTP.sys hostname filter).
    Same fix as `feedback_dual_host_listener.md`.
  - ThreadingHTTPServer for IP-bind (immune to hostname dropping).
  - Per-job artifacts under outputs/runs/<run_id>/openclaw/ — never collide.
  - HMAC-token auth on every /op (env: ARIA_OPENCLAW_TOKEN).
  - Watchdog timer per job; physical-relay e-stop if Pi GPIO available.

Endpoints (bound to http://localhost:7507/):
  GET  /status            — { ok, machines, jobs_running, recipe_count }
  GET  /info              — full state dump (debug)
  POST /op                — body:{ kind, params }
  GET  /events/{job_id}   — SSE stream of telemetry frames for a job
  POST /quit              — graceful shutdown

Launch (on the Pi):
  python -m cad_plugins.openclaw.aria_openclaw_server
  ARIA_OPENCLAW_PORT=7600 python -m ...

The orchestrator reaches this via /api/openclaw/submit on the dashboard
backend (mirrors /api/ecad/text-to-board for KiCad).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Drivers are local stubs until the Pi is provisioned. Each module exposes:
#   submit(manifest) -> job_id
#   poll(job_id)     -> { state, progress, telemetry }
#   cancel(job_id)   -> bool
#   estop()          -> bool
from cad_plugins.openclaw import drivers  # type: ignore  # noqa: E402


_log = logging.getLogger("aria_openclaw")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# State.
# ---------------------------------------------------------------------------
@dataclass
class Machine:
    machine_id: str
    kind: str            # "fdm" | "sla" | "cnc_mill" | "cnc_router" | "laser"
    driver: str          # "klipper" | "octoprint" | "marlin" | "linuxcnc" | "grbl"
    state: str = "idle"  # idle | warming | running | error | needs_attention
    current_job: Optional[str] = None
    last_seen_ts: float = 0.0
    notes: str = ""


@dataclass
class Job:
    job_id: str
    run_id: str
    machine_id: str
    artifact_url: str
    state: str = "queued"     # queued | preflight | running | done | failed | cancelled
    progress: float = 0.0     # 0.0 -> 1.0
    started_ts: float = 0.0
    finished_ts: float = 0.0
    expected_runtime_s: float = 0.0
    expected_bbox_mm: tuple = (0.0, 0.0, 0.0)
    slicer_hash: str = ""
    cam_hash: str = ""
    last_error: str = ""
    telemetry: list = field(default_factory=list)


_state_lock = threading.RLock()
_machines: dict[str, Machine] = {}
_jobs: dict[str, Job] = {}


def _machine_seed():
    # Default machines for development. Replace with real serial/USB
    # discovery on the Pi.
    if _machines:
        return
    _machines["printer_a"] = Machine(
        machine_id="printer_a", kind="fdm", driver="klipper",
        notes="default dev FDM stub")
    _machines["cnc_a"] = Machine(
        machine_id="cnc_a", kind="cnc_mill", driver="linuxcnc",
        notes="default dev CNC stub")


# ---------------------------------------------------------------------------
# Auth.
# ---------------------------------------------------------------------------
def _hmac_ok(headers, body: bytes) -> bool:
    secret = os.environ.get("ARIA_OPENCLAW_TOKEN", "")
    if not secret:
        # Dev mode: no token configured -> allow. Production must set this.
        return True
    sig = headers.get("X-Aria-Sig", "")
    expected = hmac.new(secret.encode("utf-8"), body,
                         hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# Op handlers.
# ---------------------------------------------------------------------------
def _op_submit_job(p: dict) -> dict:
    required = ("run_id", "machine_id", "artifact_url")
    missing = [k for k in required if k not in p]
    if missing:
        return {"ok": False, "error": f"missing required fields: {missing}"}
    machine_id = p["machine_id"]
    with _state_lock:
        m = _machines.get(machine_id)
        if m is None:
            return {"ok": False, "error": f"unknown machine_id: {machine_id}"}
        if m.state not in ("idle",):
            return {"ok": False,
                    "error": f"machine {machine_id} not idle (state={m.state})"}
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=job_id,
            run_id=p["run_id"],
            machine_id=machine_id,
            artifact_url=p["artifact_url"],
            expected_runtime_s=float(p.get("expected_runtime_s", 0.0)),
            expected_bbox_mm=tuple(p.get("expected_bbox_mm", (0.0, 0.0, 0.0))),
            slicer_hash=str(p.get("slicer_hash", "")),
            cam_hash=str(p.get("cam_hash", "")),
        )
        _jobs[job_id] = job
        m.state = "warming"
        m.current_job = job_id
    # Hand off to driver async.
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"ok": True, "kind": "submitJob", "job_id": job_id,
            "machine_id": machine_id, "run_id": p["run_id"]}


def _run_job(job_id: str):
    """Driver loop running in a background thread.

    Pre-flight -> driver.submit -> poll loop -> finish + machine state
    reset. Real driver calls are stubbed; the protocol is what's
    important for v1.
    """
    job = _jobs.get(job_id)
    if not job:
        return
    machine = _machines.get(job.machine_id)
    if not machine:
        return
    try:
        with _state_lock:
            job.state = "preflight"
            job.started_ts = time.time()
        ok, why = drivers.preflight(machine, job)
        if not ok:
            with _state_lock:
                job.state = "failed"
                job.last_error = f"preflight failed: {why}"
                job.finished_ts = time.time()
                machine.state = "needs_attention"
                machine.current_job = None
            return
        with _state_lock:
            job.state = "running"
            machine.state = "running"
        drv_id = drivers.submit(machine, job)
        # Poll loop with watchdog.
        last_progress_ts = time.time()
        watchdog_s = float(os.environ.get("ARIA_OPENCLAW_WATCHDOG_S", "120"))
        while True:
            tick = drivers.poll(machine, drv_id)
            with _state_lock:
                job.progress = tick.get("progress", job.progress)
                if "telemetry" in tick:
                    job.telemetry.append(tick["telemetry"])
            if tick.get("done"):
                with _state_lock:
                    job.state = "done"
                    job.finished_ts = time.time()
                    machine.state = "idle"
                    machine.current_job = None
                return
            if tick.get("failed"):
                with _state_lock:
                    job.state = "failed"
                    job.last_error = tick.get("error", "driver reported failure")
                    job.finished_ts = time.time()
                    machine.state = "needs_attention"
                    machine.current_job = None
                return
            # Watchdog: progress hasn't moved for too long.
            if tick.get("progressed"):
                last_progress_ts = time.time()
            if time.time() - last_progress_ts > watchdog_s:
                drivers.estop(machine)
                with _state_lock:
                    job.state = "failed"
                    job.last_error = f"watchdog timeout ({watchdog_s}s no progress)"
                    job.finished_ts = time.time()
                    machine.state = "error"
                    machine.current_job = None
                return
            time.sleep(1.0)
    except Exception as ex:
        _log.exception("job %s threw", job_id)
        with _state_lock:
            job.state = "failed"
            job.last_error = f"{type(ex).__name__}: {ex}"
            job.finished_ts = time.time()
            machine.state = "needs_attention"
            machine.current_job = None


def _op_poll_status(p: dict) -> dict:
    job_id = p.get("job_id")
    if not job_id:
        return {"ok": False, "error": "job_id required"}
    with _state_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        return {"ok": True, "kind": "pollStatus", "job": asdict(job)}


def _op_cancel(p: dict) -> dict:
    job_id = p.get("job_id")
    if not job_id:
        return {"ok": False, "error": "job_id required"}
    with _state_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        machine = _machines.get(job.machine_id)
    ok = drivers.cancel(machine, job_id) if machine else False
    with _state_lock:
        if job.state in ("queued", "preflight", "running"):
            job.state = "cancelled"
            job.finished_ts = time.time()
        if machine and machine.current_job == job_id:
            machine.state = "idle"
            machine.current_job = None
    return {"ok": ok, "kind": "cancel", "job_id": job_id}


def _op_estop(p: dict) -> dict:
    """Hard stop ALL machines. Hits driver e-stop AND, if Home Assistant is
    wired, cuts the smart-plug power. Idempotent."""
    machine_id = p.get("machine_id")
    results = {}
    with _state_lock:
        targets = (
            [_machines[machine_id]] if machine_id and machine_id in _machines
            else list(_machines.values())
        )
    for m in targets:
        ok = drivers.estop(m)
        results[m.machine_id] = ok
        with _state_lock:
            if m.current_job:
                j = _jobs.get(m.current_job)
                if j and j.state in ("queued", "preflight", "running"):
                    j.state = "cancelled"
                    j.last_error = "e-stop hit"
                    j.finished_ts = time.time()
            m.state = "needs_attention"
            m.current_job = None
    return {"ok": True, "kind": "eStop", "results": results}


def _op_home_axes(p: dict) -> dict:
    machine_id = p.get("machine_id")
    if not machine_id:
        return {"ok": False, "error": "machine_id required"}
    with _state_lock:
        m = _machines.get(machine_id)
    if not m:
        return {"ok": False, "error": f"unknown machine_id: {machine_id}"}
    if m.state != "idle":
        return {"ok": False, "error": f"machine not idle (state={m.state})"}
    ok = drivers.home_axes(m)
    return {"ok": ok, "kind": "homeAxes", "machine_id": machine_id}


def _op_run_visual_check(p: dict) -> dict:
    """Pull a frame from the Pi camera and post it to the dashboard's
    visual_verifier provider chain. Reuses the existing infra so we don't
    duplicate the multi-provider vision logic."""
    job_id = p.get("job_id")
    if not job_id:
        return {"ok": False, "error": "job_id required"}
    with _state_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        machine = _machines.get(job.machine_id)
    if not machine:
        return {"ok": False, "error": "machine missing"}
    result = drivers.capture_and_verify(machine, job)
    return {"ok": True, "kind": "runVisualCheck", "job_id": job_id,
            "result": result}


def _op_list_machines(_p: dict) -> dict:
    with _state_lock:
        return {"ok": True, "machines": [asdict(m) for m in _machines.values()]}


_OP_ALIASES = {
    "submit":         "submitJob",
    "submitPrint":    "submitJob",
    "submitCnc":      "submitJob",
    "status":         "pollStatus",
    "poll":           "pollStatus",
    "abort":          "cancel",
    "kill":           "eStop",
    "home":           "homeAxes",
    "verify":         "runVisualCheck",
    "machines":       "listMachines",
}

_OPS = {
    "submitJob":       _op_submit_job,
    "pollStatus":      _op_poll_status,
    "cancel":          _op_cancel,
    "eStop":           _op_estop,
    "homeAxes":        _op_home_axes,
    "runVisualCheck":  _op_run_visual_check,
    "listMachines":    _op_list_machines,
}


def _dispatch(kind: str, params: dict) -> dict:
    kind = _OP_ALIASES.get(kind, kind)
    fn = _OPS.get(kind)
    if not fn:
        return {"ok": False, "error": f"Unknown kind: {kind}"}
    try:
        return fn(params)
    except Exception as ex:
        traceback.print_exc()
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


# ---------------------------------------------------------------------------
# HTTP handler.
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet stdlib noise
        _log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            with _state_lock:
                running = sum(1 for j in _jobs.values()
                              if j.state in ("preflight", "running"))
            return self._send_json(200, {
                "ok": True,
                "machines": len(_machines),
                "jobs_total": len(_jobs),
                "jobs_running": running,
                "port": int(os.environ.get("ARIA_OPENCLAW_PORT", "7507")),
            })
        if self.path == "/info":
            with _state_lock:
                return self._send_json(200, {
                    "ok": True,
                    "machines": [asdict(m) for m in _machines.values()],
                    "jobs": [asdict(j) for j in _jobs.values()],
                })
        if self.path.startswith("/events/"):
            job_id = self.path.split("/events/", 1)[1]
            return self._sse_stream(job_id)
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        if not _hmac_ok(self.headers, body):
            return self._send_json(401, {"ok": False, "error": "bad HMAC"})
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception as ex:
            return self._send_json(400,
                {"ok": False, "error": f"bad JSON: {ex}"})
        if self.path == "/op":
            kind = payload.get("kind", "")
            params = payload.get("params", {}) or {}
            result = _dispatch(kind, params)
            return self._send_json(200, {"ok": True, "kind": kind,
                                          "result": result})
        if self.path == "/quit":
            threading.Thread(target=_shutdown, daemon=True).start()
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"ok": False, "error": "not found"})

    def _sse_stream(self, job_id: str):
        with _state_lock:
            job = _jobs.get(job_id)
        if not job:
            return self._send_json(404,
                {"ok": False, "error": f"unknown job_id: {job_id}"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last_idx = 0
        last_state = ""
        while True:
            time.sleep(0.5)
            with _state_lock:
                cur = _jobs.get(job_id)
                if not cur:
                    return
                frames = list(cur.telemetry[last_idx:])
                last_idx = len(cur.telemetry)
                state = cur.state
                progress = cur.progress
            for frame in frames:
                msg = f"data: {json.dumps(frame)}\n\n"
                try:
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return
            if state != last_state:
                msg = f"event: state\ndata: {json.dumps({'state': state, 'progress': progress})}\n\n"
                try:
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return
                last_state = state
            if state in ("done", "failed", "cancelled"):
                return


_server: Optional[ThreadingHTTPServer] = None


def _shutdown():
    global _server
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass


def main():
    _machine_seed()
    port = int(os.environ.get("ARIA_OPENCLAW_PORT", "7507"))
    # Bind to 0.0.0.0 so a Pi on LAN exposes the bridge to the dashboard.
    # Local dev (Windows) gets 127.0.0.1 via dual-stack regardless.
    bind_host = os.environ.get("ARIA_OPENCLAW_HOST", "0.0.0.0")
    addr = (bind_host, port)
    global _server
    _server = ThreadingHTTPServer(addr, _Handler)
    _log.info("aria-openclaw bridge: bound to %s:%d", bind_host, port)
    _log.info("machines: %s", list(_machines.keys()))
    if not os.environ.get("ARIA_OPENCLAW_TOKEN"):
        _log.warning("ARIA_OPENCLAW_TOKEN not set — auth disabled (dev only)")
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _server.server_close()


if __name__ == "__main__":
    main()
