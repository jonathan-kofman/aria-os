"""FeatureExtractionAgent — detect geometric primitives from cleaned meshes."""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from .. import event_bus
from ..models.scan_models import CleanedMesh, DetectedPrimitive, PartFeatureSet


# Minimum inlier fraction for a primitive to be accepted
_MIN_INLIER_RATIO = 0.02  # 2% of total points

# RANSAC parameters
_RANSAC_DISTANCE_THRESHOLD = 0.5  # mm
_RANSAC_N_POINTS = 3
_RANSAC_ITERATIONS = 1000


class FeatureExtractionAgent:
    """
    Detect geometric primitives (planes, cylinders, spheres) from a cleaned mesh
    using RANSAC segmentation. Classify overall topology and compute a
    backend-agnostic parametric description.
    """

    def run(self, cleaned_mesh: CleanedMesh) -> PartFeatureSet:
        import trimesh

        mesh = trimesh.load(cleaned_mesh.file_path, force="mesh")

        # Sample surface points for RANSAC — raw vertices are too sparse
        # on simple meshes (a box has only 8 vertices but 12 face triangles).
        # Seed RNG for deterministic results across runs.
        n_samples = max(2000, len(mesh.vertices) * 10)
        np.random.seed(42)
        points, face_indices = trimesh.sample.sample_surface(mesh, n_samples)
        points = np.asarray(points, dtype=np.float64)
        total_area = cleaned_mesh.surface_area_mm2

        event_bus.emit("scan", f"[FeatureExtract] Analyzing {len(points)} sampled points...")

        primitives: List[DetectedPrimitive] = []
        remaining_indices = np.arange(len(points))

        # Iteratively extract primitives until coverage plateaus
        for iteration in range(20):
            if len(remaining_indices) < 100:
                break

            subset = points[remaining_indices]
            best = self._fit_best_primitive(subset, remaining_indices, total_area)
            if best is None:
                break

            primitives.append(best)

            # Remove inlier points from remaining set
            inlier_set = set(best._inlier_indices_raw)
            remaining_indices = np.array([i for i in remaining_indices if i not in inlier_set])

        # Strip internal indices before returning
        for p in primitives:
            if hasattr(p, "_inlier_indices_raw"):
                del p._inlier_indices_raw

        # Compute coverage
        explained_area = sum(p.surface_area_mm2 for p in primitives)
        coverage = min(explained_area / max(total_area, 1e-9), 1.0)

        # Classify topology
        topology = self._classify_topology(primitives, coverage)

        # Build parametric description
        parametric = self._build_parametric_description(
            primitives, topology, cleaned_mesh.bounding_box
        )

        confidence = self._compute_confidence(primitives, coverage, topology)

        event_bus.emit("scan",
                       f"[FeatureExtract] Found {len(primitives)} primitives, "
                       f"coverage={coverage:.0%}, topology={topology}, "
                       f"confidence={confidence:.0%}",
                       {"n_primitives": len(primitives), "topology": topology,
                        "coverage": coverage, "confidence": confidence})

        return PartFeatureSet(
            primitives=primitives,
            topology=topology,
            coverage=coverage,
            confidence=confidence,
            parametric_description=parametric,
        )

    def _fit_best_primitive(
        self, points: np.ndarray, global_indices: np.ndarray, total_area: float
    ) -> Optional[DetectedPrimitive]:
        """Try fitting plane, cylinder, sphere — return the best fit."""
        candidates = []

        plane = self._fit_plane(points, global_indices, total_area)
        if plane:
            candidates.append(plane)

        cylinder = self._fit_cylinder(points, global_indices, total_area)
        if cylinder:
            candidates.append(cylinder)

        sphere = self._fit_sphere(points, global_indices, total_area)
        if sphere:
            candidates.append(sphere)

        if not candidates:
            return None

        # Pick the candidate with the most inliers
        return max(candidates, key=lambda p: p.inlier_count)

    def _fit_plane(
        self, points: np.ndarray, global_indices: np.ndarray, total_area: float
    ) -> Optional[DetectedPrimitive]:
        """RANSAC plane fitting."""
        if len(points) < 3:
            return None

        best_inliers = None
        best_normal = None
        best_d = None
        n_pts = len(points)
        min_inliers = max(int(n_pts * _MIN_INLIER_RATIO), 10)

        rng = np.random.RandomState(42)
        for _ in range(_RANSAC_ITERATIONS):
            idx = rng.choice(n_pts, size=3, replace=False)
            p0, p1, p2 = points[idx]
            normal = np.cross(p1 - p0, p2 - p0)
            norm_len = np.linalg.norm(normal)
            if norm_len < 1e-12:
                continue
            normal = normal / norm_len
            d = -np.dot(normal, p0)

            distances = np.abs(points @ normal + d)
            inlier_mask = distances < _RANSAC_DISTANCE_THRESHOLD
            n_inliers = int(inlier_mask.sum())

            if n_inliers > min_inliers and (best_inliers is None or n_inliers > len(best_inliers)):
                best_inliers = np.where(inlier_mask)[0]
                best_normal = normal
                best_d = d

        if best_inliers is None:
            return None

        # Estimate surface area from inlier convex hull
        inlier_pts = points[best_inliers]
        area = self._estimate_patch_area(inlier_pts, best_normal)

        # Compute extents in the plane
        u, v = self._plane_basis(best_normal)
        proj = inlier_pts @ np.column_stack([u, v])
        extents = proj.max(axis=0) - proj.min(axis=0)

        prim = DetectedPrimitive(
            type="plane",
            parameters={
                "normal": best_normal.tolist(),
                "offset": float(best_d),
                "extent_u_mm": round(float(extents[0]), 2),
                "extent_v_mm": round(float(extents[1]), 2),
            },
            surface_area_mm2=round(area, 2),
            inlier_count=len(best_inliers),
            confidence=len(best_inliers) / n_pts,
        )
        prim._inlier_indices_raw = set(global_indices[best_inliers].tolist())
        return prim

    def _fit_cylinder(
        self, points: np.ndarray, global_indices: np.ndarray, total_area: float
    ) -> Optional[DetectedPrimitive]:
        """Approximate cylinder fitting via PCA + radius estimation."""
        if len(points) < 20:
            return None

        # PCA to find dominant axis
        centroid = points.mean(axis=0)
        centered = points - centroid
        cov = centered.T @ centered / len(points)
        eigvals, eigvecs = np.linalg.eigh(cov)
        # Largest eigenvalue = axis direction (most variance)
        axis = eigvecs[:, -1]
        axis = axis / np.linalg.norm(axis)

        # Project points onto axis and perpendicular plane
        along = centered @ axis
        perp = centered - np.outer(along, axis)
        radii = np.linalg.norm(perp, axis=1)

        # Estimate radius from median (robust to outliers)
        radius = float(np.median(radii))
        if radius < 0.5:  # too small to be meaningful
            return None

        # Inliers: points within threshold of the estimated cylinder surface
        residuals = np.abs(radii - radius)
        inlier_mask = residuals < _RANSAC_DISTANCE_THRESHOLD
        n_inliers = int(inlier_mask.sum())

        if n_inliers < max(int(len(points) * _MIN_INLIER_RATIO), 10):
            return None

        # Reject cylinders with poor fit quality — if the standard deviation
        # of radial distances is high relative to radius, it's not a real cylinder
        # (e.g., a box inscribed in a cylinder has high radial variance)
        inlier_radii = radii[inlier_mask]
        radial_std = float(np.std(inlier_radii))
        if radial_std > radius * 0.15:  # >15% variation = not a cylinder
            return None

        height = float(along[inlier_mask].max() - along[inlier_mask].min())
        center = centroid + axis * float(along[inlier_mask].mean())
        area = 2 * np.pi * radius * height  # lateral surface area

        prim = DetectedPrimitive(
            type="cylinder",
            parameters={
                "axis": axis.tolist(),
                "center": center.tolist(),
                "radius_mm": round(radius, 2),
                "height_mm": round(height, 2),
            },
            surface_area_mm2=round(area, 2),
            inlier_count=n_inliers,
            confidence=n_inliers / len(points),
        )
        prim._inlier_indices_raw = set(global_indices[np.where(inlier_mask)[0]].tolist())
        return prim

    def _fit_sphere(
        self, points: np.ndarray, global_indices: np.ndarray, total_area: float
    ) -> Optional[DetectedPrimitive]:
        """Least-squares sphere fitting."""
        if len(points) < 10:
            return None

        # Algebraic sphere fit: ||p - c||^2 = r^2
        # Linearize: 2*cx*x + 2*cy*y + 2*cz*z + (r^2 - cx^2 - cy^2 - cz^2) = x^2 + y^2 + z^2
        A = np.column_stack([2 * points, np.ones(len(points))])
        b = np.sum(points ** 2, axis=1)
        try:
            result, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
        except np.linalg.LinAlgError:
            return None

        center = result[:3]
        r_sq = result[3] + np.sum(center ** 2)
        if r_sq <= 0:
            return None
        radius = float(np.sqrt(r_sq))

        if radius < 0.5 or radius > 500:  # sanity bounds
            return None

        distances = np.abs(np.linalg.norm(points - center, axis=1) - radius)
        inlier_mask = distances < _RANSAC_DISTANCE_THRESHOLD
        n_inliers = int(inlier_mask.sum())

        if n_inliers < max(int(len(points) * _MIN_INLIER_RATIO), 10):
            return None

        area = 4 * np.pi * radius ** 2

        prim = DetectedPrimitive(
            type="sphere",
            parameters={
                "center": center.tolist(),
                "radius_mm": round(radius, 2),
            },
            surface_area_mm2=round(area, 2),
            inlier_count=n_inliers,
            confidence=n_inliers / len(points),
        )
        prim._inlier_indices_raw = set(global_indices[np.where(inlier_mask)[0]].tolist())
        return prim

    def _classify_topology(self, primitives: List[DetectedPrimitive], coverage: float) -> str:
        """Classify part topology based on detected primitives."""
        if coverage < 0.4:
            return "freeform"

        type_areas = {"plane": 0.0, "cylinder": 0.0, "sphere": 0.0, "cone": 0.0}
        total = 0.0
        for p in primitives:
            type_areas[p.type] = type_areas.get(p.type, 0.0) + p.surface_area_mm2
            total += p.surface_area_mm2

        if total < 1e-9:
            return "freeform"

        cyl_frac = type_areas["cylinder"] / total
        plane_frac = type_areas["plane"] / total

        # Check if cylinders share a common axis (turned part)
        cylinders = [p for p in primitives if p.type == "cylinder"]
        if cyl_frac > 0.4 and len(cylinders) >= 1:
            if len(cylinders) == 1 or self._axes_aligned(cylinders):
                return "turned_part"

        if plane_frac > 0.5:
            return "prismatic"

        return "freeform"

    def _axes_aligned(self, cylinders: List[DetectedPrimitive], tol: float = 0.2) -> bool:
        """Check if cylinder axes are approximately parallel."""
        if len(cylinders) < 2:
            return True
        ref_axis = np.array(cylinders[0].parameters["axis"])
        for c in cylinders[1:]:
            axis = np.array(c.parameters["axis"])
            dot = abs(float(np.dot(ref_axis, axis)))
            if dot < (1.0 - tol):
                return False
        return True

    def _build_parametric_description(
        self, primitives: List[DetectedPrimitive], topology: str, bbox: "BoundingBox"
    ) -> dict:
        """Build a backend-agnostic parametric description of the part."""
        desc = {
            "topology": topology,
            "bounding_box_mm": {"x": bbox.x, "y": bbox.y, "z": bbox.z},
            "primitives": [],
        }

        for p in primitives:
            entry = {"type": p.type, **p.parameters}
            desc["primitives"].append(entry)

        # Topology-specific derived parameters
        if topology == "turned_part":
            cylinders = [p for p in primitives if p.type == "cylinder"]
            if cylinders:
                max_r = max(c.parameters["radius_mm"] for c in cylinders)
                total_h = max(c.parameters["height_mm"] for c in cylinders)
                desc["od_mm"] = round(max_r * 2, 2)
                desc["length_mm"] = round(total_h, 2)
                desc["n_diameters"] = len(cylinders)
                desc["diameters_mm"] = sorted(
                    set(round(c.parameters["radius_mm"] * 2, 2) for c in cylinders),
                    reverse=True,
                )

        elif topology == "prismatic":
            planes = [p for p in primitives if p.type == "plane"]
            desc["n_faces"] = len(planes)
            # Detect holes (small cylinders perpendicular to dominant plane)
            cylinders = [p for p in primitives if p.type == "cylinder"]
            holes = [c for c in cylinders if c.parameters["radius_mm"] < 20]
            if holes:
                desc["holes"] = [
                    {"diameter_mm": round(h.parameters["radius_mm"] * 2, 2),
                     "depth_mm": round(h.parameters["height_mm"], 2)}
                    for h in holes
                ]

        return desc

    def _compute_confidence(
        self, primitives: List[DetectedPrimitive], coverage: float, topology: str
    ) -> float:
        """Overall confidence score."""
        if not primitives:
            return 0.0

        # Weight: coverage (50%), average primitive confidence (30%), topology clarity (20%)
        avg_prim_conf = sum(p.confidence for p in primitives) / len(primitives)
        topo_score = 1.0 if topology != "freeform" else 0.3

        return round(0.5 * coverage + 0.3 * avg_prim_conf + 0.2 * topo_score, 3)

    @staticmethod
    def _plane_basis(normal: np.ndarray):
        """Compute two orthonormal vectors in the plane defined by normal."""
        n = normal / np.linalg.norm(normal)
        # Pick a vector not parallel to n
        ref = np.array([1, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1, 0])
        u = np.cross(n, ref)
        u = u / np.linalg.norm(u)
        v = np.cross(n, u)
        return u, v

    @staticmethod
    def _estimate_patch_area(points: np.ndarray, normal: np.ndarray) -> float:
        """Estimate the area of a planar point patch via 2D convex hull."""
        u, v = FeatureExtractionAgent._plane_basis(normal)
        proj = points @ np.column_stack([u, v])
        try:
            from scipy.spatial import ConvexHull
            hull = ConvexHull(proj)
            return float(hull.volume)  # 2D hull "volume" is area
        except Exception:
            # Fallback: bounding rectangle
            extents = proj.max(axis=0) - proj.min(axis=0)
            return float(extents[0] * extents[1])
