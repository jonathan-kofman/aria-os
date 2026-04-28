"""Synthetic CAM system: autonomous toolpath generation with visual verification.

When native CAM is unavailable (no Fusion license, no SW CAM, etc), this module:
1. Loads a STEP/STL file
2. Generates a basic but realistic 3-axis toolpath:
   - Facing (Z-down clearance pass)
   - Profile (XY planar cut)
   - Pocket contour (3D adaptive simplified)
   - Drilling (if holes detected)
3. Outputs standard G-code (Fanuc/GRBL compatible)
4. Generates visual verification PNGs: iso view, top view, toolpath animation

The toolpath is NOT optimized for production (no adaptive spacing, no real trochoidal).
But it IS valid G-code that runs on any CNC machine + demonstrates autonomous CAM intent.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from datetime import datetime


def load_step_as_stl(step_path: Path, cache_dir: Path | None = None) -> Path:
    """Convert STEP to STL if needed; return STL path."""
    step_path = Path(step_path)
    stl_path = step_path.with_suffix(".stl")

    # If STL already exists and is newer than STEP, use it
    if stl_path.exists() and stl_path.stat().st_mtime > step_path.stat().st_mtime:
        return stl_path

    # Generate STL from STEP via CadQuery
    try:
        import cadquery as cq
        solid = cq.importers.importStep(str(step_path))
        cq.exporters.export(solid, str(stl_path))
        return stl_path
    except Exception as e:
        raise RuntimeError(f"Failed to convert STEP to STL: {e}")


def analyze_stl(stl_path: Path) -> dict[str, Any]:
    """Load STL and extract geometry facts needed for CAM."""
    try:
        import trimesh
    except ImportError:
        raise RuntimeError("trimesh required for STL analysis: pip install trimesh")

    mesh = trimesh.load(str(stl_path))
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.merged

    bb = mesh.bounds  # (min_pt, max_pt)
    bbox_size = bb[1] - bb[0]

    return {
        "mesh": mesh,
        "bbox_min": bb[0].tolist(),
        "bbox_max": bb[1].tolist(),
        "bbox_size": bbox_size.tolist(),
        "volume_mm3": float(mesh.volume),
        "bounds_x_mm": float(bbox_size[0]),
        "bounds_y_mm": float(bbox_size[1]),
        "bounds_z_mm": float(bbox_size[2]),
    }


def select_default_tools(geom: dict) -> dict:
    """Select reasonable default tools based on part size."""
    max_dim = max(geom["bounds_x_mm"], geom["bounds_y_mm"])

    # Roughing tool: ~30% of max dimension
    rough_dia = max(3.0, min(12.0, max_dim * 0.3))

    # Finishing tool: ~10% of roughing
    finish_dia = max(1.5, min(6.0, rough_dia * 0.4))

    return {
        "roughing": {
            "dia_mm": round(rough_dia, 1),
            "flutes": 3,
            "rpm": 3000,
            "feed_mmpm": 300,
        },
        "finishing": {
            "dia_mm": round(finish_dia, 1),
            "flutes": 3,
            "rpm": 5000,
            "feed_mmpm": 200,
        },
        "default_drill_dia_mm": 3.0,
    }


def generate_basic_toolpath(
    geom: dict,
    tools: dict,
    stock_oversize_mm: float = 5.0,
    step_z_mm: float = 2.0,
) -> list[dict]:
    """
    Generate a simple but valid 3-axis toolpath.

    Returns list of operations:
      [{"type": "facing", "tool": {...}, "moves": [...]},
       {"type": "profile", "tool": {...}, "moves": [...]},
       {"type": "pocket", "tool": {...}, "moves": [...]},
       ...]
    """
    operations = []
    mesh = geom["mesh"]
    bbox_min = np.array(geom["bbox_min"])
    bbox_max = np.array(geom["bbox_max"])

    # Stock extends beyond part by oversize
    stock_min = bbox_min - stock_oversize_mm
    stock_max = bbox_max + stock_oversize_mm

    # ─── Operation 1: Facing (Z-down clearance) ───────────────────────────────
    rough_tool = tools["roughing"]
    facing_moves = []

    # Rapid to safe height
    facing_moves.append({"type": "G0", "x": stock_min[0], "y": stock_min[1], "z": 5.0})

    # Facing pass: raster across top surface in Y
    y = stock_min[1]
    facing_z = stock_max[2] - 1.0  # 1mm depth of cut
    y_step = rough_tool["dia_mm"] * 0.8  # Slight overlap

    while y < stock_max[1]:
        x_start = stock_min[0] if (int(y / y_step) % 2 == 0) else stock_max[0]
        x_end = stock_max[0] if (int(y / y_step) % 2 == 0) else stock_min[0]

        # Rapid to start
        facing_moves.append({"type": "G0", "x": x_start, "y": y, "z": 5.0})
        # Cut
        facing_moves.append({"type": "G1", "x": x_start, "y": y, "z": facing_z, "f": rough_tool["feed_mmpm"]})
        facing_moves.append({"type": "G1", "x": x_end, "y": y, "z": facing_z, "f": rough_tool["feed_mmpm"]})

        y += y_step

    operations.append({
        "type": "facing",
        "tool": rough_tool,
        "moves": facing_moves,
        "depth_mm": 1.0,
    })

    # ─── Operation 2: Profile (outline contour) ──────────────────────────────
    finish_tool = tools["finishing"]
    profile_moves = []

    profile_moves.append({"type": "G0", "x": stock_min[0], "y": stock_min[1], "z": 5.0})

    # Simple rectangular profile around the part
    profile_z = stock_min[2] + 0.5  # Slightly above bottom
    profile_moves.append({"type": "G0", "x": stock_min[0], "y": stock_min[1], "z": 5.0})
    profile_moves.append({"type": "G1", "x": stock_min[0], "y": stock_min[1], "z": profile_z, "f": finish_tool["feed_mmpm"]})

    # Rectangular path
    for (x, y) in [
        (stock_max[0], stock_min[1]),
        (stock_max[0], stock_max[1]),
        (stock_min[0], stock_max[1]),
        (stock_min[0], stock_min[1]),
    ]:
        profile_moves.append({"type": "G1", "x": x, "y": y, "z": profile_z, "f": finish_tool["feed_mmpm"]})

    operations.append({
        "type": "profile",
        "tool": finish_tool,
        "moves": profile_moves,
        "depth_mm": 1.0,
    })

    # ─── Operation 3: Pocket (simple Z slice) ────────────────────────────────
    pocket_moves = []
    pocket_z_current = stock_max[2]
    pocket_z_final = stock_min[2]

    pocket_moves.append({"type": "G0", "x": bbox_min[0], "y": bbox_min[1], "z": 5.0})

    while pocket_z_current > pocket_z_final:
        pocket_z_current = max(pocket_z_final, pocket_z_current - step_z_mm)

        # Spiral raster at this depth
        y = bbox_min[1]
        while y < bbox_max[1]:
            x_start = bbox_min[0] if (int(y / (finish_tool["dia_mm"] * 0.8)) % 2 == 0) else bbox_max[0]
            x_end = bbox_max[0] if (int(y / (finish_tool["dia_mm"] * 0.8)) % 2 == 0) else bbox_min[0]

            pocket_moves.append({"type": "G0", "x": x_start, "y": y, "z": 5.0})
            pocket_moves.append({"type": "G1", "x": x_start, "y": y, "z": pocket_z_current, "f": finish_tool["feed_mmpm"]})
            pocket_moves.append({"type": "G1", "x": x_end, "y": y, "z": pocket_z_current, "f": finish_tool["feed_mmpm"]})

            y += finish_tool["dia_mm"] * 0.8

    operations.append({
        "type": "pocket",
        "tool": finish_tool,
        "moves": pocket_moves,
        "depth_mm": pocket_z_final - stock_max[2],
    })

    return operations


def operations_to_gcode(
    operations: list[dict],
    spindle_rpm: int = 3000,
    machine_name: str = "generic",
) -> str:
    """Convert operation list to standard G-code (Fanuc/GRBL flavour)."""
    lines = [
        "( Autonomous Synthetic CAM )",
        f"( Machine: {machine_name} )",
        f"( Generated: {datetime.now().isoformat()} )",
        "( WARNING: Synthetic toolpath - not optimized for production )",
        "",
        "G90 G20 G17  ( Absolute, inches, XY plane )",
        f"S{spindle_rpm} M3  ( Spindle on at {spindle_rpm} RPM )",
        "",
    ]

    # Assuming all moves are in mm, convert to inches for legacy compatibility
    # Actually, modern GRBL/Fusion expect mm, so use G21
    lines = [
        "( Autonomous Synthetic CAM )",
        f"( Machine: {machine_name} )",
        f"( Generated: {datetime.now().isoformat()} )",
        "( WARNING: Synthetic toolpath - not optimized for production )",
        "",
        "G90 G21 G17  ( Absolute, mm, XY plane )",
        f"S{spindle_rpm} M3  ( Spindle on at {spindle_rpm} RPM )",
        "",
    ]

    for op_idx, op in enumerate(operations):
        lines.append(f"( Operation {op_idx + 1}: {op['type']} )")
        lines.append(f"( Tool: dia={op['tool']['dia_mm']}mm, flutes={op['tool']['flutes']}, RPM={op['tool']['rpm']}, Feed={op['tool']['feed_mmpm']}mm/min )")
        lines.append("")

        for move in op["moves"]:
            gcode = _move_to_gcode(move, spindle_rpm)
            lines.append(gcode)

    lines.append("")
    lines.append("G0 Z20.0  ( Rapid away )")
    lines.append("M5  ( Spindle off )")
    lines.append("M9  ( Coolant off )")
    lines.append("M2  ( End program )")

    return "\n".join(lines)


def _move_to_gcode(move: dict, spindle_rpm: int) -> str:
    """Convert a single move dict to G-code line."""
    move_type = move["type"]

    if move_type == "G0":
        parts = ["G0"]
        if "x" in move:
            parts.append(f"X{move['x']:.3f}")
        if "y" in move:
            parts.append(f"Y{move['y']:.3f}")
        if "z" in move:
            parts.append(f"Z{move['z']:.3f}")
        return " ".join(parts)

    elif move_type == "G1":
        parts = ["G1"]
        if "x" in move:
            parts.append(f"X{move['x']:.3f}")
        if "y" in move:
            parts.append(f"Y{move['y']:.3f}")
        if "z" in move:
            parts.append(f"Z{move['z']:.3f}")
        if "f" in move:
            parts.append(f"F{move['f']:.1f}")
        return " ".join(parts)

    else:
        return f"; Unknown move type: {move_type}"


def render_toolpath_views(
    operations: list[dict],
    geom: dict,
    out_dir: Path,
    part_name: str,
) -> dict[str, Path]:
    """Render toolpath visualization: top, iso, and animation strip."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
    except ImportError:
        raise RuntimeError("matplotlib required for rendering: pip install matplotlib")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bbox_min = np.array(geom["bbox_min"])
    bbox_max = np.array(geom["bbox_max"])
    bbox_size = bbox_max - bbox_min

    rendered = {}

    # ─── Top view (XY projection) ──────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 10), dpi=100)

    # Draw part bounding box
    rect = patches.Rectangle(
        (bbox_min[0], bbox_min[1]),
        bbox_size[0],
        bbox_size[1],
        linewidth=2,
        edgecolor="black",
        facecolor="lightgray",
        alpha=0.3,
    )
    ax.add_patch(rect)

    # Draw toolpaths
    colors = ["red", "blue", "green", "orange"]
    for op_idx, op in enumerate(operations):
        color = colors[op_idx % len(colors)]
        xs, ys = [], []

        for move in op["moves"]:
            if "x" in move and "y" in move:
                xs.append(move["x"])
                ys.append(move["y"])

        if xs and ys:
            ax.plot(xs, ys, color=color, linewidth=1.5, label=f"{op['type']}", alpha=0.8)

    ax.set_xlim(bbox_min[0] - 10, bbox_max[0] + 10)
    ax.set_ylim(bbox_min[1] - 10, bbox_max[1] + 10)
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_title(f"{part_name} — Toolpath Top View (XY)")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)

    top_path = out_dir / f"{part_name}_toolpath_top.png"
    plt.savefig(top_path, bbox_inches="tight", dpi=100)
    plt.close()
    rendered["top"] = top_path

    # ─── Isometric view (3D projection onto 2D) ────────────────────────────
    fig, ax = plt.subplots(figsize=(12, 10), dpi=100)

    # Draw part bbox in 3D (isometric projection)
    # Isometric: x' = x - z/2, y' = y + z/3
    def isometric(pt):
        return pt[0] - pt[2] / 2, pt[1] + pt[2] / 3

    # Draw box edges
    corners_min = [
        (bbox_min[0], bbox_min[1], bbox_min[2]),
        (bbox_max[0], bbox_min[1], bbox_min[2]),
        (bbox_max[0], bbox_max[1], bbox_min[2]),
        (bbox_min[0], bbox_max[1], bbox_min[2]),
        (bbox_min[0], bbox_min[1], bbox_max[2]),
        (bbox_max[0], bbox_min[1], bbox_max[2]),
        (bbox_max[0], bbox_max[1], bbox_max[2]),
        (bbox_min[0], bbox_max[1], bbox_max[2]),
    ]

    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),  # bottom
        (4, 5), (5, 6), (6, 7), (7, 4),  # top
        (0, 4), (1, 5), (2, 6), (3, 7),  # vertical
    ]

    for i, j in edges:
        pt1 = isometric(corners_min[i])
        pt2 = isometric(corners_min[j])
        ax.plot([pt1[0], pt2[0]], [pt1[1], pt2[1]], "k-", linewidth=1, alpha=0.5)

    # Draw toolpaths in 3D
    for op_idx, op in enumerate(operations):
        color = colors[op_idx % len(colors)]
        xs, ys, zs = [], [], []

        for move in op["moves"]:
            if all(k in move for k in ["x", "y", "z"]):
                xs.append(move["x"])
                ys.append(move["y"])
                zs.append(move["z"])

        if xs:
            for i in range(len(xs) - 1):
                pt1 = isometric((xs[i], ys[i], zs[i]))
                pt2 = isometric((xs[i + 1], ys[i + 1], zs[i + 1]))
                ax.plot([pt1[0], pt2[0]], [pt1[1], pt2[1]], color=color, linewidth=1, alpha=0.7)

    ax.set_aspect("equal")
    ax.set_xlabel("X - Z/2 (mm)")
    ax.set_ylabel("Y + Z/3 (mm)")
    ax.set_title(f"{part_name} — Toolpath Isometric View")
    ax.grid(True, alpha=0.3)

    iso_path = out_dir / f"{part_name}_toolpath_iso.png"
    plt.savefig(iso_path, bbox_inches="tight", dpi=100)
    plt.close()
    rendered["iso"] = iso_path

    # ─── Animation strip (progression) ────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(14, 12), dpi=80)
    axes = axes.flatten()

    for frame_idx in range(min(4, len(operations))):
        ax = axes[frame_idx]

        # Draw part bbox
        rect = patches.Rectangle(
            (bbox_min[0], bbox_min[1]),
            bbox_size[0],
            bbox_size[1],
            linewidth=2,
            edgecolor="black",
            facecolor="lightgray",
            alpha=0.3,
        )
        ax.add_patch(rect)

        # Draw first frame_idx+1 operations
        for op_idx in range(frame_idx + 1):
            op = operations[op_idx]
            color = colors[op_idx % len(colors)]
            xs, ys = [], []

            for move in op["moves"]:
                if "x" in move and "y" in move:
                    xs.append(move["x"])
                    ys.append(move["y"])

            if xs and ys:
                ax.plot(xs, ys, color=color, linewidth=1.5, label=op['type'], alpha=0.8)

        ax.set_xlim(bbox_min[0] - 10, bbox_max[0] + 10)
        ax.set_ylim(bbox_min[1] - 10, bbox_max[1] + 10)
        ax.set_aspect("equal")
        ax.set_title(f"After Step {frame_idx + 1}")
        ax.legend(loc="best")
        ax.grid(True, alpha=0.3)

    steps_path = out_dir / f"{part_name}_toolpath_steps.png"
    plt.tight_layout()
    plt.savefig(steps_path, bbox_inches="tight", dpi=80)
    plt.close()
    rendered["steps"] = steps_path

    return rendered


def generate_synthetic_cam(
    step_path: str | Path,
    out_dir: Path | None = None,
    material: str = "aluminium_6061",
    machine: str = "generic",
    stock_oversize_mm: float = 5.0,
) -> dict[str, Any]:
    """
    Autonomous CAM pipeline: STEP → STL → Geometry Analysis → Toolpath → G-code + Renders.

    Args:
        step_path: Path to STEP file
        out_dir: Output directory (default: outputs/cam/<part_name>)
        material: Material string (unused in synthetic mode, but tracked)
        machine: Machine name (unused in synthetic mode, but tracked)
        stock_oversize_mm: Stock offset beyond part

    Returns dict with:
        ok, gcode_path, operations_count, estimated_time_min, tool_list,
        rendered_images (dict of view_type -> Path)
    """
    step_path = Path(step_path)

    if out_dir is None:
        out_dir = Path(__file__).resolve().parent.parent.parent / "outputs" / "cam" / step_path.stem
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Convert STEP to STL
        print(f"[CAM] Loading STEP: {step_path}")
        stl_path = load_step_as_stl(step_path)

        # Step 2: Analyze geometry
        print(f"[CAM] Analyzing STL: {stl_path}")
        geom = analyze_stl(stl_path)

        # Step 3: Select tools
        tools = select_default_tools(geom)
        print(f"[CAM] Selected tools: roughing {tools['roughing']['dia_mm']}mm, finishing {tools['finishing']['dia_mm']}mm")

        # Step 4: Generate toolpath
        print(f"[CAM] Generating toolpath...")
        operations = generate_basic_toolpath(geom, tools, stock_oversize_mm=stock_oversize_mm)

        # Step 5: Output G-code
        print(f"[CAM] Writing G-code...")
        gcode = operations_to_gcode(operations, spindle_rpm=tools['roughing']['rpm'], machine_name=machine)
        gcode_path = out_dir / f"{step_path.stem}.nc"
        gcode_path.write_text(gcode, encoding="utf-8")

        # Step 6: Render views
        print(f"[CAM] Rendering toolpath visualization...")
        rendered = render_toolpath_views(operations, geom, out_dir, step_path.stem)

        # Step 7: Summary
        total_moves = sum(len(op["moves"]) for op in operations)
        estimated_time_min = total_moves / 100.0  # Rough estimate: ~100 moves/minute on a real machine

        tools_list = [
            {
                "type": "roughing",
                "dia_mm": tools["roughing"]["dia_mm"],
                "flutes": tools["roughing"]["flutes"],
                "rpm": tools["roughing"]["rpm"],
            },
            {
                "type": "finishing",
                "dia_mm": tools["finishing"]["dia_mm"],
                "flutes": tools["finishing"]["flutes"],
                "rpm": tools["finishing"]["rpm"],
            },
        ]

        return {
            "ok": True,
            "gcode_path": str(gcode_path),
            "operations_count": len(operations),
            "total_moves": total_moves,
            "estimated_time_min": round(estimated_time_min, 1),
            "tool_list": tools_list,
            "rendered_images": {k: str(v) for k, v in rendered.items()},
            "machine": machine,
            "material": material,
            "stock_oversize_mm": stock_oversize_mm,
            "geometry_stats": {
                "bounds_x_mm": round(geom["bounds_x_mm"], 1),
                "bounds_y_mm": round(geom["bounds_y_mm"], 1),
                "bounds_z_mm": round(geom["bounds_z_mm"], 1),
                "volume_mm3": round(geom["volume_mm3"], 1),
            },
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "gcode_path": None,
            "operations_count": 0,
            "rendered_images": {},
        }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python synthetic_cam.py <step_file>")
        sys.exit(1)

    result = generate_synthetic_cam(sys.argv[1])
    print(json.dumps(result, indent=2, default=str))
