r"""projection.py — Builds the VR-projection manifest for a machine.

When the user is in StructSight VR and looks at a registered machine, the
viewer requests this manifest. It contains everything the renderer needs
to drop a hologram of the running job's part onto the machine's build
plate, fading in completed layers as the print/cut progresses.

The manifest is intentionally small (URLs + pose + progress) — the heavy
geometry is fetched as a .glb and streamed by the VR client.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import machine_calibration

_BRIDGE_BASE = os.environ.get("ARIA_OPENCLAW_BASE",
                               "http://localhost:7507")


@dataclass
class ProjectionManifest:
    machine_id: str
    fiducial_id: str
    has_calibration: bool
    build_plate_origin_offset_mm: tuple = (0.0, 0.0, 0.0)
    build_plate_quat: list = field(default_factory=lambda: [0, 0, 0, 1])
    build_volume_mm: tuple = (220.0, 220.0, 250.0)
    has_active_job: bool = False
    job_id: str = ""
    run_id: str = ""
    progress: float = 0.0  # 0..1
    state: str = ""
    glb_url: str = ""              # URL the VR client GETs for the part .glb
    layer_count: int = 0
    layer_height_mm: float = 0.2
    completed_layer_count: int = 0
    expected_runtime_s: float = 0.0
    expected_bbox_mm: tuple = (0.0, 0.0, 0.0)
    # Hint to the VR shader: "ghost" = render uncompleted layers semi-
    # transparent, "solid" = render whole part opaque (when finished).
    render_mode: str = "ghost"


def _query_bridge_job(machine_id: str) -> dict | None:
    """Find the running/queued job for the given machine on the OpenClaw
    bridge. Returns the job dict from the bridge's /info endpoint, or
    None if no job is associated."""
    try:
        with urllib.request.urlopen(
                f"{_BRIDGE_BASE}/info", timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    jobs = data.get("jobs", [])
    # Prefer a running job; fall back to queued/preflight.
    for j in jobs:
        if j.get("machine_id") == machine_id and j.get("state") == "running":
            return j
    for j in jobs:
        if (j.get("machine_id") == machine_id
                and j.get("state") in ("queued", "preflight")):
            return j
    # Most-recent finished — useful for "show me what just printed".
    for j in sorted(jobs, key=lambda x: x.get("finished_ts", 0), reverse=True):
        if j.get("machine_id") == machine_id and j.get("state") == "done":
            return j
    return None


def _glb_url_for_run(run_id: str) -> str:
    """Returns the dashboard URL for the run's primary .glb asset.

    The dashboard already exposes /api/runs/<run_id>/asset?kind=glb (or
    similar) — this path matches the StructSight convention. If the GLB
    doesn't exist yet the VR client falls back to STL via the same
    handler.
    """
    if not run_id:
        return ""
    return f"/api/runs/{run_id}/asset?kind=glb"


def build(machine_id: str) -> ProjectionManifest:
    """Build the VR-projection manifest for the named machine.

    Used by the dashboard endpoint /api/openclaw/projection/<machine_id>
    that StructSight VR polls every ~1s while the user is looking at a
    machine fiducial.
    """
    cal = machine_calibration.get(machine_id)
    if cal is None:
        return ProjectionManifest(
            machine_id=machine_id,
            fiducial_id="",
            has_calibration=False,
        )
    job = _query_bridge_job(machine_id)
    manifest = ProjectionManifest(
        machine_id=machine_id,
        fiducial_id=cal.fiducial_id,
        has_calibration=True,
        build_plate_origin_offset_mm=cal.build_plate_origin_offset_mm,
        build_plate_quat=cal.build_plate_quat,
        build_volume_mm=cal.build_volume_mm,
    )
    if job is None:
        return manifest
    manifest.has_active_job = True
    manifest.job_id = job.get("job_id", "")
    manifest.run_id = job.get("run_id", "")
    manifest.progress = float(job.get("progress", 0.0))
    manifest.state = job.get("state", "")
    manifest.glb_url = _glb_url_for_run(manifest.run_id)
    manifest.expected_runtime_s = float(job.get("expected_runtime_s", 0.0))
    bbox = job.get("expected_bbox_mm", (0.0, 0.0, 0.0))
    manifest.expected_bbox_mm = tuple(bbox) if bbox else (0.0, 0.0, 0.0)
    # Layer derivation for FDM. Slicer should ideally embed layer count in
    # the manifest, but until it does we estimate from bbox z and layer
    # height (default 0.2mm).
    layer_h = float(os.environ.get("ARIA_DEFAULT_LAYER_H_MM", "0.2"))
    bbox_z = manifest.expected_bbox_mm[2] if len(manifest.expected_bbox_mm) >= 3 else 0.0
    layer_count = int(max(1, round(bbox_z / max(layer_h, 0.05))))
    manifest.layer_height_mm = layer_h
    manifest.layer_count = layer_count
    manifest.completed_layer_count = int(round(layer_count * manifest.progress))
    if manifest.state == "done":
        manifest.render_mode = "solid"
        manifest.completed_layer_count = layer_count
    return manifest


def to_dict(m: ProjectionManifest) -> dict[str, Any]:
    """asdict + tuple-flattening for JSON. The dataclass nests tuples
    that asdict already converts to lists, so this is a thin wrapper."""
    out = asdict(m)
    return out
