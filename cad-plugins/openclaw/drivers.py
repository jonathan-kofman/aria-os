r"""drivers.py — Machine-driver shims for the OpenClaw bridge.

Each function below dispatches to a per-driver implementation based on the
Machine's `driver` field. Initially everything is a stub — the protocol
matters more than the implementation for v1, so the dashboard / orchestrator
can be developed against a real listener before the Pi arrives.

Per-driver real wires (TODO when Pi is online):
  klipper:    Moonraker REST API (http://pi:7125)
  octoprint:  REST API + websocket (http://pi:5000/api/printer)
  marlin:     Direct serial via `pyserial`
  linuxcnc:   linuxcnc-python module + `linuxcncrsh` if remote
  grbl:       Serial G-code streaming
  cncjs:      cncjs websocket API

All functions take the Machine and return JSON-serializable dicts. The bridge
treats failures as `{ok=False, error=...}` and updates state accordingly.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

_log = logging.getLogger("aria_openclaw.drivers")

# In-process job tracking for the stubs. Real drivers won't need this — the
# machine itself is the source of truth.
_stub_jobs: dict[str, dict[str, Any]] = {}


def preflight(machine, job) -> tuple[bool, str]:
    """Verify the machine is ready: bed temp, filament loaded, work-zero set,
    no prior job mid-run, etc. Driver-specific."""
    drv = machine.driver
    if drv == "klipper":
        return _klipper_preflight(machine, job)
    if drv == "octoprint":
        return _octoprint_preflight(machine, job)
    if drv == "linuxcnc":
        return _linuxcnc_preflight(machine, job)
    return True, "stub: skipped preflight"


def submit(machine, job) -> str:
    """Hand the artifact off to the machine. Returns a driver-side job id
    (which may differ from the bridge's job_id)."""
    drv_id = job.job_id  # stub: use the same id
    # Honor the caller's expected_runtime_s. Floor at 2s so demo jobs
    # (YC pitch flow with ~8s artifacts) can actually complete inside
    # the verify-polling window. The 60s legacy floor blocked that.
    _stub_jobs[drv_id] = {
        "started": time.time(),
        "expected_runtime_s": max(2.0, job.expected_runtime_s or 30.0),
        "progress": 0.0,
    }
    _log.info("stub: submitted job %s to %s (%s)", drv_id,
              machine.machine_id, machine.driver)
    return drv_id


def poll(machine, drv_id) -> dict:
    """Tick: progress, telemetry, done / failed flags."""
    job = _stub_jobs.get(drv_id)
    if not job:
        return {"failed": True, "error": "stub: unknown job"}
    elapsed = time.time() - job["started"]
    expected = job["expected_runtime_s"]
    progress = min(1.0, elapsed / expected)
    prev = job["progress"]
    job["progress"] = progress
    out = {
        "progress": progress,
        "progressed": progress > prev + 1e-3,
        "telemetry": {
            "ts": time.time(),
            "elapsed_s": round(elapsed, 1),
            # Stub temps drift toward target with some jitter.
            "hotend_c": round(200 + random.uniform(-3, 3), 1)
                         if machine.kind == "fdm" else None,
            "bed_c": round(60 + random.uniform(-1, 1), 1)
                      if machine.kind == "fdm" else None,
            "spindle_load": round(0.4 + random.uniform(-0.05, 0.05), 2)
                             if "cnc" in machine.kind else None,
            "layer": int(progress * 100) if machine.kind == "fdm" else None,
        },
    }
    if progress >= 1.0:
        out["done"] = True
        _stub_jobs.pop(drv_id, None)
    return out


def cancel(machine, drv_id) -> bool:
    if drv_id in _stub_jobs:
        _stub_jobs.pop(drv_id, None)
    _log.info("stub: cancel %s on %s", drv_id, machine.machine_id)
    return True


def estop(machine) -> bool:
    """Hard stop: driver halt + GPIO relay if available + Home Assistant
    smart-plug cut. Idempotent."""
    _log.warning("stub: e-stop on %s (%s)", machine.machine_id, machine.driver)
    # TODO when Pi: pull GPIO relay LOW, then call HA service smart_plug.off
    return True


def home_axes(machine) -> bool:
    _log.info("stub: home %s", machine.machine_id)
    return True


def capture_and_verify(machine, job) -> dict:
    """Pull a frame from the Pi camera and hand it to visual_verifier.

    On the real Pi: OpenCV captures from /dev/video0, encodes to JPEG, posts
    to dashboard /api/visual/verify. Here we return a stub frame ID.
    """
    return {
        "frame_id": f"stub_{int(time.time())}",
        "verdict": "skip",
        "reason": "stub driver — no camera",
    }


# ---------------------------------------------------------------------------
# Per-driver pre-flight stubs. Replace with real probes when Pi is online.
# ---------------------------------------------------------------------------
def _klipper_preflight(machine, job):
    # Real: GET http://pi:7125/printer/objects/query?print_stats&heater_bed&extruder
    return True, "stub: assumed klipper ready"


def _octoprint_preflight(machine, job):
    return True, "stub: assumed octoprint ready"


def _linuxcnc_preflight(machine, job):
    # Real: linuxcnc.stat() — check estop, machine_on, all_homed, in_position.
    return True, "stub: assumed linuxcnc ready"
