"""
aria_os/clearance_checker.py — Post-assembly clearance checker.

Checks whether assembled parts are interpenetrating or too close to each other
by loading each part's STL counterpart, applying position/rotation transforms,
and computing closest-point distances between meshes.

Usage (CLI):
    python -m aria_os.clearance_checker assembly_configs/clock_gear_train.json
    python -m aria_os.clearance_checker assembly_configs/clock_gear_train.json --min-clearance 1.0
"""

from __future__ import annotations

import json
import math
import sys
from itertools import combinations
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def check_clearance(
    parts: list[dict],
    min_clearance_mm: float = 0.5,
    proximity_threshold_mm: float = 100.0,
) -> dict:
    """
    Check clearance between every pair of assembled parts.

    Parameters
    ----------
    parts : list[dict]
        Each dict: {"id": str, "step": str, "pos": [x, y, z], "rot": [rx, ry, rz]}
        ``step`` is used only to derive the STL path — replace ``step/`` with
        ``stl/`` and ``.step`` with ``.stl``.
    min_clearance_mm : float
        Minimum acceptable clearance.  Pairs below this (but ≥ 0) are "tight".
    proximity_threshold_mm : float
        Skip pairs whose bounding-box centres are farther apart than this
        distance (their clearance is trivially fine).

    Returns
    -------
    dict
        {
          "pairs": [
            {
              "a": str,           # id of first part
              "b": str,           # id of second part
              "clearance_mm": float,
              "status": "ok" | "tight" | "interpenetrating"
            },
            ...
          ],
          "violations": [...],    # subset of pairs where status != "ok"
          "passed": bool          # True iff no violations
        }
    """
    try:
        import numpy as np
        import trimesh
        import trimesh.proximity
    except ImportError as exc:
        raise ImportError(
            "trimesh and numpy are required for clearance checking. "
            "Install with: pip install trimesh numpy"
        ) from exc

    ROOT = Path(__file__).resolve().parent.parent

    # ------------------------------------------------------------------
    # 1.  Load and transform every part's mesh
    # ------------------------------------------------------------------
    loaded: list[dict] = []   # {"id", "mesh_world"}

    for part in parts:
        part_id = part.get("id", "?")
        step_raw = part.get("step", "")

        # Derive STL path from STEP path
        stl_raw = step_raw.replace("/step/", "/stl/").replace("\\step\\", "\\stl\\")
        if stl_raw.lower().endswith(".step"):
            stl_raw = stl_raw[:-5] + ".stl"
        elif stl_raw.lower().endswith(".stp"):
            stl_raw = stl_raw[:-4] + ".stl"

        # Resolve path
        stl_path = Path(stl_raw)
        if not stl_path.is_absolute():
            candidate = ROOT / stl_path
            stl_path = candidate

        if not stl_path.exists():
            print(
                f"[clearance] WARNING: STL not found for '{part_id}', skipping: {stl_path}"
            )
            continue

        try:
            mesh = trimesh.load_mesh(str(stl_path), force="mesh")
        except Exception as exc:
            print(f"[clearance] WARNING: could not load STL for '{part_id}': {exc}")
            continue

        # Apply rotation then translation
        pos = part.get("pos", [0.0, 0.0, 0.0])
        rot = part.get("rot", [0.0, 0.0, 0.0])
        rx, ry, rz = (math.radians(float(a)) for a in rot)
        tx, ty, tz = (float(a) for a in pos)

        # Build 4×4 transform: intrinsic XYZ Euler (Rx · Ry · Rz) then translate
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)

        Rx = np.array([
            [1,   0,   0,  0],
            [0,  cx, -sx,  0],
            [0,  sx,  cx,  0],
            [0,   0,   0,  1],
        ], dtype=float)
        Ry = np.array([
            [ cy, 0, sy, 0],
            [  0, 1,  0, 0],
            [-sy, 0, cy, 0],
            [  0, 0,  0, 1],
        ], dtype=float)
        Rz = np.array([
            [cz, -sz, 0, 0],
            [sz,  cz, 0, 0],
            [ 0,   0, 1, 0],
            [ 0,   0, 0, 1],
        ], dtype=float)
        T = np.array([
            [1, 0, 0, tx],
            [0, 1, 0, ty],
            [0, 0, 1, tz],
            [0, 0, 0,  1],
        ], dtype=float)

        transform = T @ Rx @ Ry @ Rz
        mesh.apply_transform(transform)

        loaded.append({"id": part_id, "mesh": mesh})

    # ------------------------------------------------------------------
    # 2.  Check every pair
    # ------------------------------------------------------------------
    pairs: list[dict] = []
    violations: list[dict] = []

    for (entry_a, entry_b) in combinations(loaded, 2):
        id_a = entry_a["id"]
        id_b = entry_b["id"]
        mesh_a: trimesh.Trimesh = entry_a["mesh"]
        mesh_b: trimesh.Trimesh = entry_b["mesh"]

        # Proximity filter — compare bounding-box centres
        centre_a = mesh_a.bounds.mean(axis=0)
        centre_b = mesh_b.bounds.mean(axis=0)
        centre_dist = float(np.linalg.norm(centre_a - centre_b))
        if centre_dist > proximity_threshold_mm:
            continue

        # Closest-point distance between the two surface meshes
        clearance_mm = _surface_clearance(mesh_a, mesh_b)

        if clearance_mm < 0:
            status = "interpenetrating"
        elif clearance_mm < min_clearance_mm:
            status = "tight"
        else:
            status = "ok"

        entry = {
            "a": id_a,
            "b": id_b,
            "clearance_mm": round(clearance_mm, 4),
            "status": status,
        }
        pairs.append(entry)
        if status != "ok":
            violations.append(entry)

    return {
        "pairs": pairs,
        "violations": violations,
        "passed": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _surface_clearance(mesh_a: "trimesh.Trimesh", mesh_b: "trimesh.Trimesh") -> float:
    """
    Return the signed minimum surface-to-surface clearance between two meshes.

    Positive  → gap between surfaces (OK or tight).
    Negative  → meshes interpenetrate (overlap distance).

    Strategy
    --------
    1.  Sample up to 1 000 points on mesh_a's surface.
    2.  Query closest point on mesh_b for each sample.
    3.  The minimum distance across all samples approximates the gap.
    4.  If the bounding boxes overlap *and* any sample point from mesh_a is
        contained inside mesh_b (contains_points), the clearance is negative.
    """
    import numpy as np
    import trimesh.proximity
    import trimesh.sample

    # Sample surface points on mesh_a
    n_samples = min(1000, max(50, len(mesh_a.faces) // 4))
    try:
        pts_a, _ = trimesh.sample.sample_surface(mesh_a, n_samples)
    except Exception:
        pts_a = mesh_a.vertices

    # Closest distances from mesh_a surface samples → mesh_b surface
    try:
        _, dists, _ = trimesh.proximity.closest_point(mesh_b, pts_a)
    except Exception:
        # Fall back to bounding-box separation if proximity fails
        return _bbox_clearance(mesh_a, mesh_b)

    min_dist = float(np.min(dists))

    # Check interpenetration: are any mesh_a surface points inside mesh_b?
    try:
        inside = mesh_b.contains(pts_a)
        if inside.any():
            # Return a negative clearance proportional to the deepest penetration.
            # Use closest distances to mesh_b surface for the "inside" points;
            # those distances represent how deeply they are embedded.
            inside_dists = dists[inside]
            return -float(np.max(inside_dists))
    except Exception:
        pass

    return min_dist


def _bbox_clearance(mesh_a: "trimesh.Trimesh", mesh_b: "trimesh.Trimesh") -> float:
    """Axis-aligned bounding-box clearance — fast fallback."""
    import numpy as np

    mn_a, mx_a = mesh_a.bounds
    mn_b, mx_b = mesh_b.bounds

    gaps = np.maximum(0, np.maximum(mn_a - mx_b, mn_b - mx_a))
    overlap = np.minimum(0, np.minimum(mx_a - mn_b, mx_b - mn_a))

    gap_total = float(np.linalg.norm(gaps))
    if gap_total > 0:
        return gap_total
    # Overlapping bounding boxes — rough penetration depth
    return float(np.max(overlap))  # negative


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "ok": "OK  ",
    "tight": "WARN",
    "interpenetrating": "FAIL",
}


def print_clearance_table(result: dict, min_clearance_mm: float = 0.5) -> None:
    pairs = result["pairs"]
    violations = result["violations"]
    passed = result["passed"]

    if not pairs:
        print("[clearance] No pairs within proximity threshold — nothing to report.")
        return

    col_w = max(len(p["a"]) for p in pairs)
    col_w = max(col_w, max(len(p["b"]) for p in pairs), 20)

    header = f"  {'Part A':<{col_w}}  {'Part B':<{col_w}}  {'Clearance':>12}  Status"
    print()
    print("[clearance] Post-assembly clearance check")
    print(f"[clearance] Min required: {min_clearance_mm} mm")
    print(f"[clearance] Pairs checked: {len(pairs)}")
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    for p in pairs:
        icon = _STATUS_ICON.get(p["status"], "????")
        cl = p["clearance_mm"]
        cl_str = f"{cl:+.3f} mm"
        print(f"  [{icon}] {p['a']:<{col_w}}  {p['b']:<{col_w}}  {cl_str:>12}  {p['status']}")

    print("-" * len(header))

    if passed:
        print(f"[clearance] PASSED — all {len(pairs)} pairs meet clearance requirement.")
    else:
        print(
            f"[clearance] FAILED — {len(violations)} violation(s) out of {len(pairs)} pair(s):"
        )
        for v in violations:
            print(f"             {v['a']} ↔ {v['b']}: {v['clearance_mm']:+.3f} mm ({v['status']})")
    print()


# ---------------------------------------------------------------------------
# CLI entry point  (python -m aria_os.clearance_checker <config.json>)
# ---------------------------------------------------------------------------

def _load_parts_from_config(config_path: Path) -> tuple[list[dict], float]:
    """Return (parts_list, min_clearance_mm) from an assembly JSON config."""
    with open(config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    parts = config.get("parts", [])
    min_clearance = float(config.get("min_clearance_mm", 0.5))
    return parts, min_clearance


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Post-assembly clearance checker for ARIA-OS assemblies."
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to assembly JSON config (same format as assemble.py).",
    )
    parser.add_argument(
        "--min-clearance",
        type=float,
        default=None,
        metavar="MM",
        help="Minimum acceptable clearance in mm (default: 0.5, or value from config).",
    )
    parser.add_argument(
        "--proximity",
        type=float,
        default=100.0,
        metavar="MM",
        help="Skip pairs whose bounding-box centres are farther than this (default: 100 mm).",
    )

    args = parser.parse_args(argv)

    config_path = args.config
    if not config_path.is_absolute():
        ROOT = Path(__file__).resolve().parent.parent
        config_path = ROOT / config_path
    if not config_path.exists():
        print(f"[clearance] Error: config not found: {config_path}")
        sys.exit(1)

    parts, min_clearance_cfg = _load_parts_from_config(config_path)
    min_clearance = args.min_clearance if args.min_clearance is not None else min_clearance_cfg

    result = check_clearance(
        parts,
        min_clearance_mm=min_clearance,
        proximity_threshold_mm=args.proximity,
    )
    print_clearance_table(result, min_clearance_mm=min_clearance)

    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
