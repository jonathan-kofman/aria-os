"""
SDF analysis — volume, mass, centre of gravity, printability checks.

Why these matter for pro quality:
  Volume → material cost estimation, CAM stock sizing
  Mass   → flight sim TWR, shipping cost, structural credibility
  CoG    → balance, vibration, assembly design
  Printability → catch parts that can't physically print (overhangs, thin
                 walls) before wasting 8 hours of Centauri Carbon time.

Minimum feature size = thinnest wall a printer/CNC can actually produce.
Overhang angle     = max angle from vertical that doesn't need support.
"""
from __future__ import annotations

import numpy as np


def compute_volume(sdf_func, bounds: tuple,
                   resolution: float = 0.5) -> float:
    """Voxel-count the interior cells (d < 0). Returns volume in mm³.

    bounds = ((x0,y0,z0), (x1,y1,z1)). Pick bounds that enclose the
    actual geometry; too-tight bounds clip it, too-loose wastes RAM.
    """
    (x0, y0, z0), (x1, y1, z1) = bounds
    x = np.arange(x0, x1 + resolution, resolution)
    y = np.arange(y0, y1 + resolution, resolution)
    z = np.arange(z0, z1 + resolution, resolution)
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    field = sdf_func(X, Y, Z)
    n_inside = int(np.sum(field < 0))
    return n_inside * (resolution ** 3)


def compute_mass(sdf_func, bounds: tuple,
                 density_kg_m3: float = 2700.0,
                 resolution: float = 0.5) -> float:
    """Mass in grams. density_kg_m3: standard material density.
    aluminum 6061 = 2700, steel 1018 = 7870, titanium Ti-6Al-4V = 4430."""
    volume_mm3 = compute_volume(sdf_func, bounds, resolution)
    volume_m3 = volume_mm3 * 1e-9
    return volume_m3 * density_kg_m3 * 1000.0  # grams


def compute_cog(sdf_func, bounds: tuple,
                resolution: float = 0.5) -> tuple[float, float, float]:
    """Centre of gravity (assumes uniform density). Returns (x, y, z) mm.
    Computed as the centroid of interior voxels."""
    (x0, y0, z0), (x1, y1, z1) = bounds
    x = np.arange(x0, x1 + resolution, resolution)
    y = np.arange(y0, y1 + resolution, resolution)
    z = np.arange(z0, z1 + resolution, resolution)
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    mask = sdf_func(X, Y, Z) < 0
    n = int(np.sum(mask))
    if n == 0:
        return (0.0, 0.0, 0.0)
    cx = float(np.sum(X[mask])) / n
    cy = float(np.sum(Y[mask])) / n
    cz = float(np.sum(Z[mask])) / n
    return (cx, cy, cz)


def compute_bbox(sdf_func, bounds: tuple,
                 resolution: float = 0.5) -> tuple[tuple, tuple]:
    """Actual bounding box of the material (vs the query-bounds above,
    which are the eval envelope)."""
    (x0, y0, z0), (x1, y1, z1) = bounds
    x = np.arange(x0, x1 + resolution, resolution)
    y = np.arange(y0, y1 + resolution, resolution)
    z = np.arange(z0, z1 + resolution, resolution)
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    mask = sdf_func(X, Y, Z) < 0
    if not np.any(mask):
        return ((0, 0, 0), (0, 0, 0))
    return (
        (float(X[mask].min()), float(Y[mask].min()), float(Z[mask].min())),
        (float(X[mask].max()), float(Y[mask].max()), float(Z[mask].max())),
    )


# ---------------------------------------------------------------------------
# Printability checks
# ---------------------------------------------------------------------------

def check_min_feature_size(sdf_func, bounds: tuple,
                           min_feature_mm: float = 0.8,
                           resolution: float = 0.25) -> dict:
    """Detect walls thinner than min_feature_mm. Approx: the minimum
    positive gradient distance between neighbouring inside voxels —
    voxels within min_feature_mm of the surface on all sides are flagged.
    """
    (x0, y0, z0), (x1, y1, z1) = bounds
    x = np.arange(x0, x1 + resolution, resolution)
    y = np.arange(y0, y1 + resolution, resolution)
    z = np.arange(z0, z1 + resolution, resolution)
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    field = sdf_func(X, Y, Z)
    inside = field < 0
    thin = inside & (np.abs(field) < min_feature_mm / 2)
    n_thin = int(np.sum(thin))
    n_inside = int(np.sum(inside))
    frac = n_thin / max(n_inside, 1)
    return {
        "passed": frac < 0.01,  # < 1% thin voxels is acceptable
        "thin_voxel_fraction": round(frac, 4),
        "n_thin_voxels": n_thin,
        "min_feature_mm": min_feature_mm,
        "message": (f"{n_thin} voxels < {min_feature_mm}mm thick "
                    f"({frac * 100:.2f}% of interior volume)"),
    }


def check_overhangs(sdf_func, bounds: tuple,
                    max_angle_deg: float = 45.0,
                    resolution: float = 0.5,
                    print_direction: tuple = (0, 0, 1)) -> dict:
    """Flag surface facets whose normal points further from the print
    direction than max_angle_deg. Approximation: we use the SDF gradient
    as the surface normal and evaluate at a shell of surface voxels.
    """
    (x0, y0, z0), (x1, y1, z1) = bounds
    x = np.arange(x0, x1 + resolution, resolution)
    y = np.arange(y0, y1 + resolution, resolution)
    z = np.arange(z0, z1 + resolution, resolution)
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    field = sdf_func(X, Y, Z)
    # Central-difference gradient = surface normal for surface voxels.
    gx = np.gradient(field, resolution, axis=0)
    gy = np.gradient(field, resolution, axis=1)
    gz = np.gradient(field, resolution, axis=2)
    mag = np.sqrt(gx * gx + gy * gy + gz * gz) + 1e-9
    nx, ny, nz = gx / mag, gy / mag, gz / mag
    # Surface voxels: close to zero
    surface = np.abs(field) < resolution
    # Angle from print direction (invert — surface normals point outward,
    # overhangs have NEGATIVE dot product with print direction)
    pdx, pdy, pdz = print_direction
    dot = nx * pdx + ny * pdy + nz * pdz
    angle_cos_threshold = np.cos(np.radians(90.0 - max_angle_deg))
    overhang = surface & (dot < -angle_cos_threshold)
    n_overhang = int(np.sum(overhang))
    n_surface = int(np.sum(surface))
    frac = n_overhang / max(n_surface, 1)
    return {
        "passed": frac < 0.05,  # < 5% overhang is printable with minimal support
        "overhang_fraction": round(frac, 4),
        "n_overhang_voxels": n_overhang,
        "max_angle_deg": max_angle_deg,
        "message": (f"{n_overhang} surface voxels at > {max_angle_deg}° "
                    f"overhang ({frac * 100:.2f}% of surface)"),
    }


def mesh_stats_full(mesh_data: tuple,
                    material_density_kg_m3: float = 2700.0) -> dict:
    """Comprehensive stats from a (verts, faces, normals) mesh tuple.
    Volume via divergence theorem; mass; bbox; CoG; surface area."""
    verts, faces, _ = mesh_data
    verts = np.asarray(verts)
    faces = np.asarray(faces)
    v0 = verts[faces[:, 0]]
    v1 = verts[faces[:, 1]]
    v2 = verts[faces[:, 2]]
    # Signed volume of each tet (origin, v0, v1, v2)
    signed_vol = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    volume_mm3 = abs(float(np.sum(signed_vol)))
    # Centroid weighted by tet volume
    centroids = (v0 + v1 + v2) / 4.0  # includes origin vertex at 0
    cog_weighted = np.sum(centroids * signed_vol[:, None], axis=0)
    cog = tuple((cog_weighted / np.sum(signed_vol)).tolist()) if abs(
        np.sum(signed_vol)) > 1e-9 else (0.0, 0.0, 0.0)
    # Surface area via cross-product
    tri_areas = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    surface_mm2 = float(np.sum(tri_areas))
    bbox_min = verts.min(axis=0).tolist()
    bbox_max = verts.max(axis=0).tolist()
    return {
        "n_vertices": len(verts),
        "n_faces": len(faces),
        "volume_mm3": round(volume_mm3, 2),
        "surface_mm2": round(surface_mm2, 2),
        "mass_g": round(volume_mm3 * 1e-9 * material_density_kg_m3 * 1000, 3),
        "cog_mm": [round(c, 3) for c in cog],
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
        "bbox_mm": [round(b - a, 3) for a, b in zip(bbox_min, bbox_max)],
    }
