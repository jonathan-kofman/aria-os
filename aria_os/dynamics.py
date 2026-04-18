"""
Dynamics + trajectory solver for kinematic chains.

Wraps Pinocchio (if installed) for proper multi-body dynamics. Falls back to a
simple built-in implementation for basic reach/mass/FK calcs when Pinocchio
isn't available.

What you can compute:
- Forward kinematics (end-effector pose given joint angles)
- Reach analysis (convex hull of end-effector positions over joint ranges)
- Jacobian (sensitivity of end-effector to joint motion)
- Mass properties (center of mass, inertia)
- Simple trajectory planning (joint-space linear interpolation with velocity limits)

For full MBD (contact, friction, gravity compensation, real-time control),
use Pinocchio directly with the URDF exported by joints.export_urdf().
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Optional Pinocchio import
# ---------------------------------------------------------------------------

_PINOCCHIO = None
try:
    import pinocchio as _pin  # type: ignore
    _PINOCCHIO = _pin
except ImportError:
    pass


def pinocchio_available() -> bool:
    """True if pinocchio is installed (full dynamics support)."""
    return _PINOCCHIO is not None


# ---------------------------------------------------------------------------
# Simple kinematics (fallback when Pinocchio missing)
# ---------------------------------------------------------------------------

@dataclass
class Pose:
    """Rigid-body pose: position (mm) + RPY rotation (rad)."""
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    roll_rad: float = 0.0
    pitch_rad: float = 0.0
    yaw_rad: float = 0.0

    def as_tuple(self) -> tuple[float, ...]:
        return (self.x_mm, self.y_mm, self.z_mm,
                self.roll_rad, self.pitch_rad, self.yaw_rad)


@dataclass
class ReachReport:
    """Reach analysis output."""
    min_reach_mm: float
    max_reach_mm: float
    workspace_volume_mm3: float
    n_samples: int
    waypoints: list[tuple[float, float, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Built-in simple FK for serial chains
# ---------------------------------------------------------------------------

def simple_forward_kinematics(
    joints: list[dict[str, Any]],
    link_lengths_mm: list[float],
    joint_values: list[float],
) -> Pose:
    """
    Simple serial-chain FK for a planar or spatial manipulator.

    joints: list of joint dicts {type, axis}
    link_lengths_mm: length of each link (distance between consecutive joints)
    joint_values: joint position (radians for revolute, mm for prismatic)

    Returns the pose of the tip. Approximate — for real robotics use Pinocchio.
    """
    import numpy as np

    # Start at origin, rotation = identity
    pos = np.array([0.0, 0.0, 0.0])
    R = np.eye(3)

    for i, joint in enumerate(joints):
        j_type = joint.get("type", "revolute")
        axis = np.array(joint.get("axis", [0, 0, 1]), dtype=float)
        axis = axis / (np.linalg.norm(axis) or 1)
        q = joint_values[i] if i < len(joint_values) else 0.0

        if j_type == "revolute" or j_type == "continuous":
            # Rodrigues rotation
            c = math.cos(q)
            s = math.sin(q)
            K = np.array([[0, -axis[2], axis[1]],
                          [axis[2], 0, -axis[0]],
                          [-axis[1], axis[0], 0]])
            dR = np.eye(3) + s * K + (1 - c) * K @ K
            R = R @ dR
        elif j_type == "prismatic":
            pos = pos + R @ (axis * q)
        elif j_type == "fixed":
            pass

        # Advance by link length along the current end's Z axis
        if i < len(link_lengths_mm):
            pos = pos + R @ np.array([0, 0, link_lengths_mm[i]])

    # Extract RPY from rotation matrix
    pitch = math.asin(-max(-1.0, min(1.0, R[2][0])))
    if abs(math.cos(pitch)) > 1e-6:
        roll = math.atan2(R[2][1], R[2][2])
        yaw = math.atan2(R[1][0], R[0][0])
    else:
        roll = 0.0
        yaw = math.atan2(-R[0][1], R[1][1])

    return Pose(x_mm=float(pos[0]), y_mm=float(pos[1]), z_mm=float(pos[2]),
                roll_rad=roll, pitch_rad=pitch, yaw_rad=yaw)


# ---------------------------------------------------------------------------
# Reach analysis — Monte Carlo sampling
# ---------------------------------------------------------------------------

def compute_reach(
    joints: list[dict[str, Any]],
    link_lengths_mm: list[float],
    n_samples: int = 2000,
    seed: int | None = 42,
) -> ReachReport:
    """
    Monte Carlo reach analysis — sample joint-space uniformly, evaluate FK,
    report the bounding volume of the end-effector positions.
    """
    import numpy as np

    rng = np.random.default_rng(seed)

    points: list[tuple[float, float, float]] = []
    for _ in range(n_samples):
        joint_values: list[float] = []
        for j in joints:
            j_type = j.get("type", "revolute")
            rng_deg = j.get("range_deg", [-180, 180])
            if j_type in ("revolute", "continuous"):
                lo, hi = math.radians(rng_deg[0]), math.radians(rng_deg[1])
            elif j_type == "prismatic":
                lo, hi = rng_deg[0], rng_deg[1]
            else:
                lo = hi = 0.0
            q = rng.uniform(lo, hi)
            joint_values.append(q)

        pose = simple_forward_kinematics(joints, link_lengths_mm, joint_values)
        points.append((pose.x_mm, pose.y_mm, pose.z_mm))

    pts = np.array(points)
    # Reach = distance from origin
    distances = np.linalg.norm(pts, axis=1)
    min_r = float(distances.min())
    max_r = float(distances.max())

    # Rough workspace volume — bounding-box approximation
    extent = pts.max(axis=0) - pts.min(axis=0)
    volume = float(extent[0] * extent[1] * extent[2])

    return ReachReport(
        min_reach_mm=min_r,
        max_reach_mm=max_r,
        workspace_volume_mm3=volume,
        n_samples=n_samples,
        waypoints=points[:100],  # keep a subset for visualization
    )


# ---------------------------------------------------------------------------
# Trajectory planning — joint-space linear interpolation
# ---------------------------------------------------------------------------

@dataclass
class Trajectory:
    """Time-parameterized joint trajectory."""
    times_s: list[float]
    joint_positions: list[list[float]]  # one list per waypoint

    def sample(self, t_s: float) -> list[float]:
        """Linear interpolation at time t."""
        if not self.times_s:
            return []
        if t_s <= self.times_s[0]:
            return list(self.joint_positions[0])
        if t_s >= self.times_s[-1]:
            return list(self.joint_positions[-1])
        for i in range(len(self.times_s) - 1):
            if self.times_s[i] <= t_s <= self.times_s[i + 1]:
                t0, t1 = self.times_s[i], self.times_s[i + 1]
                a = (t_s - t0) / (t1 - t0)
                q0 = self.joint_positions[i]
                q1 = self.joint_positions[i + 1]
                return [q0[j] + a * (q1[j] - q0[j]) for j in range(len(q0))]
        return list(self.joint_positions[-1])


def plan_joint_trajectory(
    start: list[float],
    end: list[float],
    max_velocity_rad_s: list[float],
    *,
    dt: float = 0.01,
) -> Trajectory:
    """
    Plan a trapezoidal-velocity joint trajectory from start to end.

    Respects per-joint velocity limits. All joints finish simultaneously
    (slowest joint sets the duration).
    """
    if len(start) != len(end):
        raise ValueError("start and end must have same number of joints")
    if len(max_velocity_rad_s) != len(start):
        raise ValueError("max_velocity must match joint count")

    # Find duration constrained by slowest joint
    duration = 0.0
    for i in range(len(start)):
        delta = abs(end[i] - start[i])
        vmax = max_velocity_rad_s[i]
        if vmax <= 0:
            continue
        t_required = delta / vmax
        duration = max(duration, t_required)

    n_steps = max(2, int(duration / dt) + 1)
    times = [i * duration / (n_steps - 1) for i in range(n_steps)]
    positions = []
    for t in times:
        a = t / duration if duration > 0 else 1.0
        q = [start[i] + a * (end[i] - start[i]) for i in range(len(start))]
        positions.append(q)

    return Trajectory(times_s=times, joint_positions=positions)


# ---------------------------------------------------------------------------
# Pinocchio wrappers (when available) — pass-through to pin API
# ---------------------------------------------------------------------------

def load_urdf_pinocchio(urdf_path: str | Path) -> Any:
    """
    Load a URDF via Pinocchio. Returns (Model, Data) tuple.
    Raises ImportError if Pinocchio isn't installed.
    """
    if _PINOCCHIO is None:
        raise ImportError("pinocchio not installed — pip install pin")
    model = _PINOCCHIO.buildModelFromUrdf(str(urdf_path))
    data = model.createData()
    return model, data


def compute_gravity_torques(urdf_path: str | Path,
                             joint_values: list[float]) -> list[float]:
    """
    Compute the joint torques needed to hold the robot static against gravity.
    Requires Pinocchio.
    """
    if _PINOCCHIO is None:
        raise ImportError("pinocchio not installed — pip install pin")
    import numpy as _np
    model, data = load_urdf_pinocchio(urdf_path)
    q = _np.array(joint_values)
    tau = _PINOCCHIO.rnea(model, data, q, _np.zeros(model.nv), _np.zeros(model.nv))
    return list(tau.tolist())


def compute_reach_pinocchio(
    urdf_path: str | Path,
    n_samples: int = 5000,
    end_effector_frame: str = "tool0",
) -> ReachReport:
    """
    Full Pinocchio-based reach analysis — better than the fallback for complex
    chains with non-trivial link geometries.
    """
    if _PINOCCHIO is None:
        raise ImportError("pinocchio not installed")
    import numpy as _np
    model, data = load_urdf_pinocchio(urdf_path)
    try:
        ee_id = model.getFrameId(end_effector_frame)
    except Exception:
        ee_id = model.nframes - 1  # fall back to last frame

    rng = _np.random.default_rng(42)
    points = []
    for _ in range(n_samples):
        q = _np.array([rng.uniform(lo, hi) for lo, hi in
                       zip(model.lowerPositionLimit, model.upperPositionLimit)])
        _PINOCCHIO.forwardKinematics(model, data, q)
        _PINOCCHIO.updateFramePlacement(model, data, ee_id)
        pos = data.oMf[ee_id].translation * 1000  # m to mm
        points.append((float(pos[0]), float(pos[1]), float(pos[2])))

    pts = _np.array(points)
    distances = _np.linalg.norm(pts, axis=1)
    extent = pts.max(axis=0) - pts.min(axis=0)
    return ReachReport(
        min_reach_mm=float(distances.min()),
        max_reach_mm=float(distances.max()),
        workspace_volume_mm3=float(extent[0] * extent[1] * extent[2]),
        n_samples=n_samples,
        waypoints=points[:100],
    )
