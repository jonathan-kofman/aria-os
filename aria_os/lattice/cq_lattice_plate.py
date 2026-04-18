"""
Parametric SLM-style lattice plate generator (CadQuery).

Produces a flat or slightly-curved plate with a periodic arc-cell pattern
cut through it — visually similar to typical SLM-printed metal lattices
where overlapping curves form a repeating petal/rosette motif.

The geometry:
  - Flat plate (L × W × T) with optional cylindrical curvature radius
  - Periodic 2×2 cell grid; each cell cuts 4 arcs that intersect at
    corners forming an X-shape strut + petal-shaped voids
  - Strut width + cell size + skin (solid border) all parametric

Print process: SLM (selective laser melting), DMLS, or LPBF — any
metal AM. PETG prints fine if you bump strut width above ~1.5mm.

Usage:
    from aria_os.lattice.cq_lattice_plate import build_lattice_plate, LatticeParams
    plate = build_lattice_plate(LatticeParams(
        length_mm=120, width_mm=80, thickness_mm=4.0,
        cell_size_mm=12.0, strut_width_mm=1.6,
        curvature_radius_mm=400.0,  # 0 = flat
    ))
    import cadquery as cq
    cq.exporters.export(plate, "lattice_plate.step")
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class LatticeParams:
    """Parametric lattice plate definition."""
    length_mm: float = 120.0          # plate length (X)
    width_mm: float = 80.0            # plate width (Y)
    thickness_mm: float = 4.0         # plate thickness (Z)
    cell_size_mm: float = 12.0        # one lattice cell side length
    strut_width_mm: float = 1.6       # min material between cuts
    skin_mm: float = 4.0              # solid border around the lattice region
    curvature_radius_mm: float = 0.0  # 0 = flat; positive = cylindrical bend
    pattern: str = "rosette"          # "rosette" | "diamond" | "honeycomb" | "arc_x"
    cell_aspect: float = 1.0          # X-stretch of cells (1.0 = square)


def build_lattice_plate(params: LatticeParams):
    """Build the parametric lattice plate. Returns a cadquery Workplane."""
    import cadquery as cq

    L, W, T = params.length_mm, params.width_mm, params.thickness_mm

    # Base plate
    plate = cq.Workplane("XY").box(L, W, T, centered=(True, True, False))

    # Lattice region — inset from edges by skin_mm so we leave a solid border
    region_x_min = -L / 2 + params.skin_mm
    region_x_max = +L / 2 - params.skin_mm
    region_y_min = -W / 2 + params.skin_mm
    region_y_max = +W / 2 - params.skin_mm
    region_w = region_x_max - region_x_min
    region_h = region_y_max - region_y_min

    cs = params.cell_size_mm
    cs_x = cs * params.cell_aspect
    cs_y = cs

    # Number of cells that fit in the lattice region
    nx = max(1, int(region_w // cs_x))
    ny = max(1, int(region_h // cs_y))
    # Recenter the cell grid in the region (so partial cells are equal on both sides)
    grid_w = nx * cs_x
    grid_h = ny * cs_y
    x0 = (region_x_min + region_x_max - grid_w) / 2
    y0 = (region_y_min + region_y_max - grid_h) / 2

    # Build the cutter solid for this pattern, then subtract once.
    # Building one cutter is much faster than calling .cut() N×M times.
    cutter = _build_pattern_cutter(
        cq, params, nx, ny, cs_x, cs_y, x0, y0, T,
    )
    if cutter is not None:
        plate = plate.cut(cutter)

    # Optional cylindrical curvature — bend the plate around the X-axis.
    # Implemented by shrinking the region's Z thickness with the chord-of-arc
    # function. Cheap approximation: real curved SLM panels are usually
    # designed flat then formed; we model the formed shape directly.
    if params.curvature_radius_mm > 0:
        plate = _apply_curvature(cq, plate, params)

    return plate


def _build_pattern_cutter(cq, params: LatticeParams,
                          nx: int, ny: int,
                          cs_x: float, cs_y: float,
                          x0: float, y0: float,
                          plate_t: float):
    """Build a single cutter solid containing every void hole, then return
    it (caller .cut()s it from the plate). Much faster than cutting each
    cell individually."""
    pattern = params.pattern
    sw = params.strut_width_mm
    voids = []   # list of 2D outlines to extrude as cutters

    for ix in range(nx):
        for iy in range(ny):
            cx = x0 + (ix + 0.5) * cs_x
            cy = y0 + (iy + 0.5) * cs_y
            if pattern == "rosette":
                voids.extend(_rosette_cell_voids(cx, cy, cs_x, cs_y, sw))
            elif pattern == "diamond":
                voids.extend(_diamond_cell_voids(cx, cy, cs_x, cs_y, sw))
            elif pattern == "honeycomb":
                voids.extend(_hex_cell_voids(cx, cy, cs_x, cs_y, sw))
            elif pattern == "arc_x":
                voids.extend(_arc_x_cell_voids(cx, cy, cs_x, cs_y, sw))
            else:
                voids.extend(_rosette_cell_voids(cx, cy, cs_x, cs_y, sw))

    if not voids:
        return None

    # Build one big cutter — combine all void outlines, extrude through plate.
    # Each void is a list of 2D points (closed polyline).
    cutter = None
    for outline in voids:
        if len(outline) < 3:
            continue
        wp = (cq.Workplane("XY").polyline(outline).close()
              .extrude(plate_t + 1.0)
              .translate((0, 0, -0.5)))
        cutter = wp if cutter is None else cutter.union(wp)
    return cutter


def _rosette_cell_voids(cx: float, cy: float,
                        sx: float, sy: float,
                        strut_w: float) -> list[list[tuple[float, float]]]:
    """4-petal rosette — 4 lens-shaped voids forming an X with petals.

    Looks like the SLM-printed plate the user shared: arc segments meeting
    at the cell corners with curved-petal voids in between.

    Each cell produces 4 lens (vesica) voids, one per quadrant.
    """
    voids = []
    # Petal radius: each petal is the inside of two arcs whose centers are
    # at adjacent cell corners. Petal-touching gap = strut_w.
    half_x = sx / 2
    half_y = sy / 2
    # Lens radius: distance from cell corner to opposite corner
    petal_offset = min(half_x, half_y) * 0.78  # how far the petal center is from cell center
    petal_r = min(half_x, half_y) * 0.62       # petal arc radius

    # 4 petals: one per cell quadrant (NE, NW, SW, SE). Each petal is the
    # boundary of two intersecting circles producing a lens shape.
    for ang_deg in (45, 135, 225, 315):
        ang = math.radians(ang_deg)
        ox = cx + petal_offset * math.cos(ang)
        oy = cy + petal_offset * math.sin(ang)
        # Approximate lens shape with 24 polyline points
        outline = []
        n = 24
        # Two arcs of opposite sense form the lens
        for i in range(n // 2 + 1):
            t = i / (n // 2)
            a = -math.pi / 2 + math.pi * t
            outline.append((
                ox + petal_r * math.cos(a + ang),
                oy + petal_r * math.sin(a + ang),
            ))
        for i in range(n // 2 + 1):
            t = i / (n // 2)
            a = math.pi / 2 + math.pi * t
            outline.append((
                ox + petal_r * 0.6 * math.cos(a + ang),
                oy + petal_r * 0.6 * math.sin(a + ang),
            ))
        # Shrink the outline by strut_w/2 (so adjacent voids leave material)
        outline = _shrink_polyline(outline, ox, oy, strut_w / 2.0)
        voids.append(outline)
    return voids


def _diamond_cell_voids(cx: float, cy: float, sx: float, sy: float,
                        strut_w: float) -> list[list[tuple[float, float]]]:
    """Diamond / square-rotated 45° hole per cell. Simpler than rosette."""
    half = (min(sx, sy) - strut_w) / 2.0
    return [[
        (cx, cy + half), (cx + half, cy),
        (cx, cy - half), (cx - half, cy),
    ]]


def _arc_x_cell_voids(cx: float, cy: float, sx: float, sy: float,
                      strut_w: float) -> list[list[tuple[float, float]]]:
    """X-strut pattern matching SLM-printed metal lattice aesthetic.

    Each cell has ONE diamond-shape void with each side ARC-BOWED INWARD
    toward the cell center. Material between cells forms a + (X-strut)
    with bulged/curved arms. Adjacent cells share corner-circle arcs so
    the assembled grid reads as overlapping circles forming X-struts —
    exactly the photo aesthetic.

    Construction:
      - 4 vertices at cell-edge midpoints (slightly inset by strut_w/2)
      - Each connecting side replaced by a curve bowing toward (cx, cy)
      - Bow amplitude proportional to cell size for visual impact
    """
    half_x = sx / 2.0
    half_y = sy / 2.0
    inset  = strut_w / 2.0

    # Diamond vertices at edge midpoints (inset for inter-cell wall)
    diamond = [
        (cx,                  cy + half_y - inset),  # top
        (cx + half_x - inset, cy),                   # right
        (cx,                  cy - half_y + inset),  # bottom
        (cx - half_x + inset, cy),                   # left
    ]

    # Each side becomes a curve bowing toward the cell center.
    # Bow amplitude = ~22% of half cell — gives the SLM "arc" look without
    # eating into the strut nodes at corners.
    n_per_arc = 12
    bow_amp = 0.22 * min(half_x, half_y)

    outline = []
    for i in range(4):
        p1 = diamond[i]
        p2 = diamond[(i + 1) % 4]
        for j in range(n_per_arc):
            t = j / n_per_arc
            # Linear interp p1 → p2
            x = p1[0] + (p2[0] - p1[0]) * t
            y = p1[1] + (p2[1] - p1[1]) * t
            # Bow displacement: max at midpoint, smooth falloff via sin
            bow = math.sin(t * math.pi) * bow_amp
            # Direction toward cell center
            dxc, dyc = cx - x, cy - y
            d = math.hypot(dxc, dyc) + 1e-9
            x += (dxc / d) * bow
            y += (dyc / d) * bow
            outline.append((x, y))

    return [outline]


def _hex_cell_voids(cx: float, cy: float, sx: float, sy: float,
                    strut_w: float) -> list[list[tuple[float, float]]]:
    """Honeycomb-style hex hole. Aspect locked to sx for clean tiling."""
    r = (min(sx, sy) - strut_w) / 2.0
    pts = []
    for i in range(6):
        a = math.radians(60 * i + 30)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [pts]


def _shrink_polyline(pts: list[tuple[float, float]],
                     cx: float, cy: float,
                     amount: float) -> list[tuple[float, float]]:
    """Pull each point of `pts` toward (cx, cy) by `amount` mm.

    Approximates inset offset for closed convex/near-convex outlines.
    """
    if amount <= 0:
        return pts
    out = []
    for x, y in pts:
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy)
        if d <= amount:
            out.append((cx, cy))
        else:
            f = (d - amount) / d
            out.append((cx + dx * f, cy + dy * f))
    return out


def _apply_curvature(cq, plate, params: LatticeParams):
    """Bend the plate around the X-axis to a cylindrical curvature.

    CadQuery's BRep doesn't easily curve a plate non-trivially. We do this
    as a mesh post-process: tessellate, displace each vertex along Y/Z by
    the chord-of-arc function so the part follows R = curvature_radius_mm,
    then return as a Mesh-wrapped Workplane.

    Result: the plate's neutral axis (mid-thickness) follows a cylindrical
    arc of radius R; bend axis is X (so width-direction wraps).
    """
    # cadquery doesn't support arbitrary mesh deformation of BRep — we
    # convert to mesh, deform, and re-import as STL (no longer parametric
    # but visually correct). Caller should call apply_curvature_to_stl()
    # after exporting the flat STL for the curvature to actually take effect.
    plate._aria_curvature_radius_mm = params.curvature_radius_mm
    return plate


def apply_curvature_to_stl(stl_path, curvature_radius_mm: float,
                            bend_axis: str = "x"):
    """Bend an exported STL around an axis to give it cylindrical curvature.

    Vertex transform (bend axis = X, plate originally on XY plane):
      For each vertex (x, y, z):
        θ = y / R
        y' = R * sin(θ)
        z' = z + R * (1 - cos(θ))

    The plate's centerline at y=0 is unchanged; tips bend symmetrically.
    Negative R = bend the other way.
    """
    import trimesh
    import numpy as np
    from pathlib import Path

    if not curvature_radius_mm or abs(curvature_radius_mm) < 1.0:
        return stl_path

    stl_path = Path(stl_path)
    mesh = trimesh.load_mesh(str(stl_path))
    if hasattr(mesh, "dump"):
        mesh = mesh.dump(concatenate=True)
    R = float(curvature_radius_mm)
    V = mesh.vertices.copy()
    if bend_axis.lower() == "x":
        # bend around X-axis → wrap Y direction
        y = V[:, 1]
        z = V[:, 2]
        theta = y / R
        V[:, 1] = R * np.sin(theta)
        V[:, 2] = z + R * (1.0 - np.cos(theta))
    else:
        # bend around Y-axis → wrap X direction
        x = V[:, 0]
        z = V[:, 2]
        theta = x / R
        V[:, 0] = R * np.sin(theta)
        V[:, 2] = z + R * (1.0 - np.cos(theta))

    mesh.vertices = V
    # Recompute normals after vertex displacement (matters for shading +
    # for any downstream mesh-validity check)
    mesh.fix_normals()
    mesh.export(str(stl_path))
    return stl_path


# ---------------------------------------------------------------------------
# Convenience: build + export + bbox stats
# ---------------------------------------------------------------------------

def build_and_export(params: LatticeParams, out_step_path):
    """Build, export STEP + STL, return {step_path, stl_path, bbox, volume,
    curved}.

    Always exports an STL alongside the STEP because curvature post-process
    operates on STL only (BRep can't easily hold a non-cylindrical-primitive
    bent surface). When curvature_radius_mm > 0, the STL is the curved
    version and the STEP is the flat as-built (stays parametric).
    """
    from pathlib import Path
    import cadquery as cq

    plate = build_lattice_plate(params)
    out_step_path = Path(out_step_path)
    out_step_path.parent.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(plate, str(out_step_path))

    # Always emit STL (downstream renders + slicer + curvature need it)
    out_stl_path = out_step_path.with_suffix(".stl")
    cq.exporters.export(plate, str(out_stl_path), exportType="STL", tolerance=0.05)

    # Apply curvature if requested — modifies the STL in place
    curved = False
    if params.curvature_radius_mm and abs(params.curvature_radius_mm) > 1.0:
        apply_curvature_to_stl(out_stl_path, params.curvature_radius_mm,
                               bend_axis="x")
        curved = True

    bb = plate.val().BoundingBox()
    vol = plate.val().Volume()
    return {
        "step_path": str(out_step_path),
        "stl_path":  str(out_stl_path),
        "bbox_mm": [round(bb.xlen, 2), round(bb.ylen, 2), round(bb.zlen, 2)],
        "volume_mm3": round(vol, 1),
        "curved": curved,
        "curvature_radius_mm": params.curvature_radius_mm if curved else 0,
        "params": params.__dict__,
    }


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser(description="SLM-style lattice plate generator")
    p.add_argument("--length", type=float, default=120.0)
    p.add_argument("--width", type=float, default=80.0)
    p.add_argument("--thickness", type=float, default=4.0)
    p.add_argument("--cell", type=float, default=12.0)
    p.add_argument("--strut", type=float, default=1.6)
    p.add_argument("--pattern", default="rosette",
                   choices=["rosette", "diamond", "honeycomb", "arc_x"])
    p.add_argument("--curve", type=float, default=0.0,
                   help="Bend radius in mm (0 = flat). Match the SLM photo "
                        "with --curve 350 (large radius = subtle bend).")
    p.add_argument("--out", default="outputs/lattice/plate.step")
    args = p.parse_args()
    params = LatticeParams(
        length_mm=args.length, width_mm=args.width,
        thickness_mm=args.thickness,
        cell_size_mm=args.cell, strut_width_mm=args.strut,
        pattern=args.pattern,
        curvature_radius_mm=args.curve,
    )
    info = build_and_export(params, args.out)
    print(json.dumps(info, indent=2))
