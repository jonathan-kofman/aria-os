"""
aria_os/generators/terrain_generator.py

Synthetic terrain heightmap generator — no external APIs, pure numpy/scipy.
Produces DXF contour plans + STL mesh from a natural language description.

Usage:
    from aria_os.generators.terrain_generator import generate_terrain
    result = generate_terrain("mountain terrain 5km x 5km with 200m peak")
    # result["dxf_path"], result["stl_path"], result["elevation_range_m"], ...
"""
from __future__ import annotations

import re
import struct
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Parameter dataclass
# ---------------------------------------------------------------------------

@dataclass
class TerrainParams:
    terrain_type: str        = "hills"     # mountain|hills|plateau|valley|ridge|flat
    width_m: float           = 2000.0      # X extent in metres
    height_m: float          = 2000.0      # Y extent in metres
    peak_elevation_m: float  = 100.0       # max Z in metres
    base_elevation_m: float  = 0.0         # min Z in metres
    roughness: float         = 0.6         # fractal roughness 0-1 (0=smooth, 1=jagged)
    seed: int                = 42
    grid_resolution: int     = 257         # NxN grid (power-of-2 + 1 preferred)
    contour_interval_m: float = 10.0       # vertical spacing between contour lines
    index_interval_m: float  = 50.0        # every Nth index contour (bolder)


# ---------------------------------------------------------------------------
# Natural language parsing
# ---------------------------------------------------------------------------

_TERRAIN_TYPES = {
    "mountain": "mountain", "mountains": "mountain", "peak": "mountain", "summit": "mountain",
    "hill": "hills", "hills": "hills", "rolling": "hills", "hilly": "hills",
    "plateau": "plateau", "mesa": "plateau", "tableland": "plateau",
    "valley": "valley", "basin": "valley", "hollow": "valley",
    "ridge": "ridge", "spine": "ridge", "saddle": "ridge",
    "flat": "flat", "plain": "flat", "plains": "flat", "prairie": "flat",
}

# Roughness defaults per terrain type
_ROUGHNESS_DEFAULTS = {
    "mountain": 0.55, "hills": 0.75, "plateau": 0.70,
    "valley": 0.65, "ridge": 0.60, "flat": 0.90,
}

# Default peak heights per terrain type (when not specified)
_PEAK_DEFAULTS = {
    "mountain": None,   # derived as width_m / 10
    "hills": 80.0,
    "plateau": None,    # derived as width_m / 8
    "valley": 120.0,
    "ridge": None,      # derived as width_m / 12
    "flat": 5.0,
}


def _nice_round(v: float) -> float:
    """Round to nearest nice contour interval: 1,2,5,10,20,25,50,100,200,250,500."""
    nice = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000]
    return min(nice, key=lambda x: abs(x - v))


def parse_terrain_params(description: str) -> TerrainParams:
    s = description.lower()
    p = TerrainParams()

    # Terrain type
    for kw, ttype in sorted(_TERRAIN_TYPES.items(), key=lambda x: -len(x[0])):
        if kw in s:
            p.terrain_type = ttype
            break

    # Size: "5km x 5km", "5km by 5km", "5x5km"
    m = re.search(r"(\d+(?:\.\d+)?)\s*km\s*(?:x|by|\*|×)\s*(\d+(?:\.\d+)?)\s*km", s)
    if m:
        p.width_m = float(m.group(1)) * 1000
        p.height_m = float(m.group(2)) * 1000
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*km", s)
        if m:
            p.width_m = p.height_m = float(m.group(1)) * 1000

    # Size in metres: "500m x 800m"
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\s*(?:x|by|\*|×)\s*(\d+(?:\.\d+)?)\s*m", s)
    if m and float(m.group(1)) > 50:  # ignore tiny things
        p.width_m = float(m.group(1))
        p.height_m = float(m.group(2))

    # Peak / elevation
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\s+(?:peak|summit|high|elevation|tall|height)", s)
    if not m:
        m = re.search(r"(?:peak|summit|elevation|height|high)\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*m", s)
    if m:
        p.peak_elevation_m = float(m.group(1))

    # Depth for valleys
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\s+(?:deep|depth)", s)
    if m:
        p.peak_elevation_m = float(m.group(1))

    # Contour interval
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\s+(?:contour|interval|contours)", s)
    if not m:
        m = re.search(r"(?:contour|interval)\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*m", s)
    if m:
        p.contour_interval_m = float(m.group(1))
        p.index_interval_m = p.contour_interval_m * 5

    # Derive roughness from terrain type
    p.roughness = _ROUGHNESS_DEFAULTS.get(p.terrain_type, 0.65)

    # Derive peak if not set
    if "peak" not in description.lower() and "summit" not in description.lower() and \
       "elevation" not in description.lower():
        default = _PEAK_DEFAULTS.get(p.terrain_type)
        if default is None:
            default = p.width_m / 10
        if p.terrain_type != "flat":  # flat stays at TerrainParams default
            p.peak_elevation_m = default

    # Derive contour interval from peak
    if "contour" not in description.lower() and "interval" not in description.lower():
        p.contour_interval_m = _nice_round(p.peak_elevation_m / 10)
        p.index_interval_m = p.contour_interval_m * 5

    # Grid resolution: scale with domain size, cap at 513 for performance
    cells = max(128, min(513, int(max(p.width_m, p.height_m) / 20)))
    # Force to 2^n + 1 for Diamond-Square
    n = 1
    while (2 ** n + 1) < cells:
        n += 1
    p.grid_resolution = 2 ** n + 1

    return p


# ---------------------------------------------------------------------------
# Diamond-Square heightmap generation
# ---------------------------------------------------------------------------

def generate_heightmap(params: TerrainParams) -> np.ndarray:
    """
    Returns an (N x N) float32 array of elevations normalised to
    [base_elevation_m, peak_elevation_m].
    Uses Diamond-Square fractal (midpoint displacement) — numpy-only.
    """
    N = params.grid_resolution
    rng = np.random.RandomState(params.seed)

    # Diamond-Square requires 2^n + 1 grid
    # N is already set to 2^n + 1 by parse_terrain_params
    grid = np.zeros((N, N), dtype=np.float64)

    # Seed corners
    grid[0, 0] = grid[0, -1] = grid[-1, 0] = grid[-1, -1] = rng.uniform(-1, 1)

    step = N - 1
    scale = 1.0
    H = 1.0 - params.roughness  # higher H = smoother

    while step > 1:
        half = step // 2

        # --- Diamond step: fill centres of each square ---
        for y in range(0, N - 1, step):
            for x in range(0, N - 1, step):
                avg = (grid[y, x] + grid[y, x + step] +
                       grid[y + step, x] + grid[y + step, x + step]) / 4.0
                grid[y + half, x + half] = avg + rng.uniform(-scale, scale)

        # --- Square step: fill midpoints of each edge ---
        for y in range(0, N, half):
            for x in range((y + half) % step, N, step):
                vals = []
                if y >= half:         vals.append(grid[y - half, x])
                if y + half < N:      vals.append(grid[y + half, x])
                if x >= half:         vals.append(grid[y, x - half])
                if x + half < N:      vals.append(grid[y, x + half])
                grid[y, x] = sum(vals) / len(vals) + rng.uniform(-scale, scale)

        scale *= 2 ** (-H)
        step = half

    # Apply terrain-type shaping
    grid = _shape_heightmap(grid, params)

    # Normalise to [base_elevation_m, peak_elevation_m]
    lo, hi = grid.min(), grid.max()
    if hi > lo:
        grid = (grid - lo) / (hi - lo)
    else:
        grid = np.zeros_like(grid)
    grid = grid * (params.peak_elevation_m - params.base_elevation_m) + params.base_elevation_m

    return grid.astype(np.float32)


def _shape_heightmap(grid: np.ndarray, params: TerrainParams) -> np.ndarray:
    N = grid.shape[0]
    cx, cy = (N - 1) / 2, (N - 1) / 2
    Y, X = np.mgrid[0:N, 0:N]
    tt = params.terrain_type

    if tt == "mountain":
        # Radial Gaussian bump — highest at centre
        sigma = N * 0.3
        mask = np.exp(-((X - cx) ** 2 + (Y - cy) ** 2) / (2 * sigma ** 2))
        grid = grid * 0.4 + grid * mask * 0.6

    elif tt == "hills":
        # No strong shaping — gentle variation
        pass

    elif tt == "plateau":
        # Flatten the top 35% to a plateau
        threshold = np.percentile(grid, 65)
        flat_val = np.percentile(grid, 80)
        grid = np.where(grid > threshold, flat_val + (grid - flat_val) * 0.15, grid)

    elif tt == "valley":
        # Invert and apply bowl (highest at edges)
        grid = grid.max() - grid
        sigma = N * 0.35
        bowl = 1.0 - np.exp(-((X - cx) ** 2 + (Y - cy) ** 2) / (2 * sigma ** 2))
        grid = grid * 0.3 + grid * bowl * 0.7

    elif tt == "ridge":
        # Elongated Gaussian along X axis
        sigma_y = N * 0.12
        mask = np.exp(-(Y - cy) ** 2 / (2 * sigma_y ** 2))
        grid = grid * 0.3 + grid * mask * 0.7

    elif tt == "flat":
        # Very low amplitude variation
        grid = grid * 0.08

    return grid


# ---------------------------------------------------------------------------
# Contour extraction (pure numpy marching squares)
# ---------------------------------------------------------------------------

def heightmap_to_contours(
    heightmap: np.ndarray,
    params: TerrainParams,
) -> list[tuple[float, list[list[tuple[float, float]]]]]:
    """
    Extract iso-contour polylines at each contour level.
    Returns list of (elevation_m, [polyline, ...]) where each polyline is
    a list of (x_m, y_m) tuples in project coordinates.
    """
    N, M = heightmap.shape
    lo = float(heightmap.min())
    hi = float(heightmap.max())

    # Contour levels — snap to contour_interval grid
    first = (int(lo / params.contour_interval_m) + 1) * params.contour_interval_m
    levels = np.arange(first, hi, params.contour_interval_m)

    # Physical coordinates for each grid point
    xs = np.linspace(0, params.width_m, M)
    ys = np.linspace(0, params.height_m, N)

    result = []
    for level in levels:
        segs = _marching_squares(heightmap, xs, ys, float(level))
        if segs:
            result.append((float(level), segs))

    return result


def _marching_squares(
    grid: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    level: float,
) -> list[list[tuple[float, float]]]:
    """
    Simple marching squares: extract line segments at a single iso-level,
    then chain them into polylines.
    """
    N, M = grid.shape
    segments = []

    def interp(a, b, va, vb):
        if abs(vb - va) < 1e-10:
            return 0.5
        return (level - va) / (vb - va)

    for i in range(N - 1):
        for j in range(M - 1):
            v00 = grid[i,   j]
            v10 = grid[i+1, j]
            v01 = grid[i,   j+1]
            v11 = grid[i+1, j+1]

            x0, x1 = xs[j], xs[j+1]
            y0, y1 = ys[i], ys[i+1]

            # Bitmask: bit set if corner is above level
            case = ((v00 >= level) << 0 | (v01 >= level) << 1 |
                    (v11 >= level) << 2 | (v10 >= level) << 3)

            if case == 0 or case == 15:
                continue

            # Edge midpoints via linear interpolation
            def edge(axis, fixed, t, a, b):
                if axis == "x":
                    return (a + t * (b - a), fixed)
                return (fixed, a + t * (b - a))

            t_left   = interp(y0, y1, v00, v10)
            t_right  = interp(y0, y1, v01, v11)
            t_bottom = interp(x0, x1, v00, v01)
            t_top    = interp(x0, x1, v10, v11)

            left   = (x0, y0 + t_left   * (y1 - y0))
            right  = (x1, y0 + t_right  * (y1 - y0))
            bottom = (x0 + t_bottom * (x1 - x0), y0)
            top    = (x0 + t_top    * (x1 - x0), y1)

            # Lookup table: 16 cases → list of (pt_a, pt_b) pairs
            _segs = {
                1:  [(left, bottom)],
                2:  [(bottom, right)],
                3:  [(left, right)],
                4:  [(right, top)],
                5:  [(left, top), (bottom, right)],
                6:  [(bottom, top)],
                7:  [(left, top)],
                8:  [(top, left)],
                9:  [(top, bottom)],
                10: [(bottom, left), (top, right)],
                11: [(top, right)],
                12: [(right, left)],
                13: [(right, bottom)],
                14: [(left, bottom)],
            }
            for (pa, pb) in _segs.get(case, []):
                segments.append((pa, pb))

    if not segments:
        return []

    # Chain segments into polylines
    return _chain_segments(segments)


def _chain_segments(
    segs: list[tuple[tuple, tuple]],
    tol: float = 0.5,  # metres
) -> list[list[tuple[float, float]]]:
    """Greedy chain short segments into polylines."""
    if not segs:
        return []

    segs = list(segs)
    used = [False] * len(segs)
    polylines = []

    for start in range(len(segs)):
        if used[start]:
            continue
        poly = list(segs[start])
        used[start] = True

        extended = True
        while extended:
            extended = False
            tail = poly[-1]
            for i, (a, b) in enumerate(segs):
                if used[i]:
                    continue
                da = (a[0] - tail[0]) ** 2 + (a[1] - tail[1]) ** 2
                db = (b[0] - tail[0]) ** 2 + (b[1] - tail[1]) ** 2
                if da < tol ** 2:
                    poly.append(b)
                    used[i] = True
                    extended = True
                    break
                elif db < tol ** 2:
                    poly.append(a)
                    used[i] = True
                    extended = True
                    break

        if len(poly) >= 2:
            polylines.append(poly)

    return polylines


# ---------------------------------------------------------------------------
# STL export (binary, no deps beyond numpy + struct)
# ---------------------------------------------------------------------------

def heightmap_to_stl(
    heightmap: np.ndarray,
    params: TerrainParams,
    output_path: Path,
) -> Path:
    """
    Convert heightmap to a triangulated surface STL.
    Generates open surface (top only) — not watertight — for simplicity.
    """
    N, M = heightmap.shape
    xs = np.linspace(0, params.width_m, M)
    ys = np.linspace(0, params.height_m, N)
    X, Y = np.meshgrid(xs, ys)

    n_tri = 2 * (N - 1) * (M - 1)
    triangles = np.zeros((n_tri, 3, 3), dtype=np.float32)

    idx = 0
    for i in range(N - 1):
        for j in range(M - 1):
            # Two triangles per cell
            v00 = np.array([X[i,   j],   Y[i,   j],   heightmap[i,   j]])
            v10 = np.array([X[i+1, j],   Y[i+1, j],   heightmap[i+1, j]])
            v01 = np.array([X[i,   j+1], Y[i,   j+1], heightmap[i,   j+1]])
            v11 = np.array([X[i+1, j+1], Y[i+1, j+1], heightmap[i+1, j+1]])
            triangles[idx]     = [v00, v01, v11]
            triangles[idx + 1] = [v00, v11, v10]
            idx += 2

    # Compute face normals
    e1 = triangles[:, 1, :] - triangles[:, 0, :]
    e2 = triangles[:, 2, :] - triangles[:, 0, :]
    normals = np.cross(e1, e2)
    norms = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.where(norms > 1e-10, normals / norms, normals)

    # Write binary STL
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(b"\x00" * 80)              # header
        f.write(struct.pack("<I", n_tri))  # triangle count
        for k in range(n_tri):
            n = normals[k]
            v = triangles[k]
            f.write(struct.pack("<fff", *n))
            for vertex in v:
                f.write(struct.pack("<fff", *vertex))
            f.write(struct.pack("<H", 0))  # attribute byte count

    return output_path


# ---------------------------------------------------------------------------
# DXF contour export
# ---------------------------------------------------------------------------

def heightmap_to_dxf(
    contours: list[tuple[float, list[list[tuple]]]],
    params: TerrainParams,
    output_path: Path,
    title: str = "",
) -> Path:
    """Write contour polylines to a DXF file using ezdxf."""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.M
    msp = doc.modelspace()

    # Layer setup
    doc.layers.add("GRADE-EXIST-CONTOUR", color=8, lineweight=18)
    doc.layers.add("GRADE-EXIST-INDEX",   color=8, lineweight=35)
    doc.layers.add("ANNO-ELEV",           color=2, lineweight=18)
    doc.layers.add("ANNO-TEXT",           color=7, lineweight=18)

    n_entities = 0
    for elevation, polylines in contours:
        is_index = (elevation % params.index_interval_m) < 0.01 or \
                   (params.index_interval_m - elevation % params.index_interval_m) < 0.01
        layer = "GRADE-EXIST-INDEX" if is_index else "GRADE-EXIST-CONTOUR"

        for poly in polylines:
            if len(poly) < 2:
                continue
            pts_3d = [(x, y, elevation) for x, y in poly]
            msp.add_lwpolyline(pts_3d[:2], dxfattribs={"layer": layer})
            if len(poly) > 2:
                msp.add_lwpolyline(pts_3d, dxfattribs={"layer": layer})
            n_entities += 1

            # Label index contours at midpoint
            if is_index and len(poly) >= 2:
                mid = len(poly) // 2
                mx, my = poly[mid]
                msp.add_text(
                    f"{elevation:.0f}m",
                    dxfattribs={
                        "layer": "ANNO-ELEV",
                        "height": params.width_m * 0.005,
                        "insert": (mx, my),
                    }
                )

    # Title block
    if title:
        msp.add_text(
            title,
            dxfattribs={
                "layer": "ANNO-TEXT",
                "height": params.width_m * 0.008,
                "insert": (params.width_m * 0.02, -params.width_m * 0.04),
            }
        )
    msp.add_text(
        f"Terrain: {params.terrain_type.title()}  |  "
        f"{params.width_m/1000:.1f}km x {params.height_m/1000:.1f}km  |  "
        f"Elev: {params.base_elevation_m:.0f}-{params.peak_elevation_m:.0f}m  |  "
        f"Contour: {params.contour_interval_m:.0f}m",
        dxfattribs={
            "layer": "ANNO-TEXT",
            "height": params.width_m * 0.005,
            "insert": (params.width_m * 0.02, -params.width_m * 0.055),
        }
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))
    return output_path


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def generate_terrain(
    description: str,
    output_dir: Path | None = None,
    params_override: dict | None = None,
) -> dict:
    """
    Parse description → generate heightmap → write DXF + STL.

    Returns dict with dxf_path, stl_path, params, elevation_range_m,
    contour_count, face_count.
    """
    params = parse_terrain_params(description)
    if params_override:
        for k, v in params_override.items():
            if hasattr(params, k):
                setattr(params, k, v)

    if output_dir is None:
        output_dir = Path("outputs/terrain")

    slug = re.sub(r"[^a-z0-9]+", "_", description.lower())[:40].strip("_")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[TERRAIN] Generating {params.terrain_type} terrain "
          f"{params.width_m/1000:.1f}km x {params.height_m/1000:.1f}km, "
          f"peak {params.peak_elevation_m:.0f}m, "
          f"grid {params.grid_resolution}x{params.grid_resolution}")

    # Generate heightmap
    heightmap = generate_heightmap(params)
    actual_min = float(heightmap.min())
    actual_max = float(heightmap.max())

    print(f"[TERRAIN] Heightmap {params.grid_resolution}x{params.grid_resolution}, "
          f"elevation {actual_min:.1f}-{actual_max:.1f}m")

    # Extract contours
    contours = heightmap_to_contours(heightmap, params)
    contour_count = sum(len(polys) for _, polys in contours)
    print(f"[TERRAIN] {len(contours)} levels, {contour_count} polylines")

    # Write DXF
    dxf_path = out_dir / f"{slug}_contours.dxf"
    heightmap_to_dxf(contours, params, dxf_path, title=description[:60])
    dxf_size = dxf_path.stat().st_size
    print(f"[TERRAIN] DXF: {dxf_path.name} ({dxf_size//1024} KB)")

    # Write STL
    stl_path = out_dir / f"{slug}_mesh.stl"
    heightmap_to_stl(heightmap, params, stl_path)
    stl_size = stl_path.stat().st_size
    face_count = 2 * (params.grid_resolution - 1) ** 2
    print(f"[TERRAIN] STL: {stl_path.name} ({stl_size//1024} KB, {face_count:,} faces)")

    # Save params JSON sidecar
    json_path = out_dir / f"{slug}_params.json"
    json_path.write_text(json.dumps(asdict(params), indent=2))

    return {
        "dxf_path": str(dxf_path),
        "stl_path": str(stl_path),
        "params":   asdict(params),
        "elevation_range_m": (actual_min, actual_max),
        "contour_count": contour_count,
        "face_count": face_count,
        # Convenience aliases (flat access)
        "terrain_type":      params.terrain_type,
        "width_m":           params.width_m,
        "height_m":          params.height_m,
        "peak_elevation_m":  params.peak_elevation_m,
        "n_contours":        contour_count,
        "grid_resolution":   params.grid_resolution,
    }
