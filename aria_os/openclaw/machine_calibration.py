r"""machine_calibration.py — Per-machine spatial calibration store.

Each physical machine (printer/CNC) needs to be located in the user's
shop space so the StructSight VR app can drop a hologram of the part on
the build plate at the right pose. Calibration captures:

  - The build-plate origin (translation in the user's room frame)
  - The build-plate orientation (quaternion)
  - The build volume dimensions (mm)
  - An AprilTag / QR fiducial id printed on the machine — anchors the
    pose at runtime even if the room frame drifts (passthrough VR
    relocalizes off the tag, not the IMU)

Calibration is captured once during shop setup with a brief VR ritual:
  1. User stands in front of the machine.
  2. StructSight prompts: "Place the AprilTag on the front-left corner of
     the build plate, then look at it."
  3. The headset's camera detects the tag, captures pose.
  4. User confirms; calibration is saved here.

The store is a JSON file under outputs/openclaw_calibrations.json so it
survives dashboard restarts. Each entry is keyed by machine_id.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock


@dataclass
class MachineCalibration:
    machine_id: str
    fiducial_id: str  # AprilTag id or QR string printed on machine
    fiducial_pose_in_machine: list = field(default_factory=lambda: [0, 0, 0, 0, 0, 0, 1])
    # ^ [tx, ty, tz, qx, qy, qz, qw] — fiducial pose RELATIVE to the
    #   build-plate origin. Captured during the calibration ritual.
    build_plate_origin_offset_mm: tuple = (0.0, 0.0, 0.0)
    # ^ Offset from fiducial mount point to build-plate origin (mm).
    #   For most printers this is "fiducial sits on front-left corner of
    #   plate" → (0, 0, 0). User can tweak via VR refine handles.
    build_volume_mm: tuple = (220.0, 220.0, 250.0)
    # ^ X, Y, Z working volume (mm). Used to pre-clamp the projection so
    #   it doesn't poke through the gantry.
    build_plate_quat: list = field(default_factory=lambda: [0, 0, 0, 1])
    # ^ Quat for plate-up orientation. Default = identity (Z up).
    notes: str = ""


_lock = RLock()
_store: dict[str, MachineCalibration] = {}
_loaded_path: Path | None = None


def _store_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    p = repo_root / "outputs" / "openclaw_calibrations.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load() -> None:
    global _loaded_path
    p = _store_path()
    _loaded_path = p
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    with _lock:
        _store.clear()
        for machine_id, entry in raw.items():
            _store[machine_id] = MachineCalibration(**entry)


def save() -> None:
    p = _loaded_path or _store_path()
    with _lock:
        snapshot = {mid: asdict(c) for mid, c in _store.items()}
    p.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def get(machine_id: str) -> MachineCalibration | None:
    with _lock:
        return _store.get(machine_id)


def upsert(cal: MachineCalibration) -> None:
    with _lock:
        _store[cal.machine_id] = cal
    save()


def list_all() -> list[MachineCalibration]:
    with _lock:
        return list(_store.values())


# Auto-load on import so first call is hot.
load()
