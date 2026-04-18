"""
Part contract validator — every parametric builder declares what its output
geometry should look like; the validator asserts it before the part is added
to an assembly.

Usage:

    from aria_os.validation import Contract, validate_part

    def _build_prop(params: dict):
        # ... build the propeller shape ...
        return shape

    def _contract_prop(params: dict) -> Contract:
        n_blades = int(params.get("n_blades", 3))
        dia_mm = float(params.get("dia_mm", 127.0))
        thk_mm = float(params.get("thk_mm", 3.5))
        return Contract(
            name="prop",
            expected_bbox_mm=(dia_mm, dia_mm, thk_mm),
            bbox_tol=0.10,
            expected_hole_count=1,           # hub bore
            expected_solid_count=1,
            is_watertight=True,
            radial_features={
                # tri-blade prop has 3 high-mass radial sectors and 3 low-mass gaps
                "n_blades": n_blades,
                "min_blade_to_gap_ratio": 0.5,
            },
        )

    shape = _build_prop(params)
    res = validate_part(shape, _contract_prop(params))
    if not res.passed:
        raise ValidationError(res.failures)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Contract:
    """What a builder claims its output geometry should look like."""
    name: str
    expected_bbox_mm: tuple[float, float, float] | None = None
    bbox_tol: float = 0.05  # ±5% by default
    expected_hole_count: int | None = None       # via Euler characteristic
    hole_count_tol: int = 0                       # exact match by default
    expected_solid_count: int | None = None
    is_watertight: bool | None = None
    min_volume_mm3: float | None = None
    max_volume_mm3: float | None = None
    radial_features: dict[str, Any] = field(default_factory=dict)
    custom_checks: list = field(default_factory=list)  # list of (name, fn(shape) -> bool)

    def __repr__(self) -> str:
        return f"Contract({self.name})"

    def is_empty(self) -> bool:
        """True if the contract specifies no checkable expectations."""
        return (
            self.expected_bbox_mm is None
            and self.expected_hole_count is None
            and self.expected_solid_count is None
            and self.is_watertight is None
            and self.min_volume_mm3 is None
            and self.max_volume_mm3 is None
            and not self.radial_features
            and not self.custom_checks
        )

    @classmethod
    def from_spec(
        cls,
        spec: dict,
        goal: str = "",
        *,
        bbox_tol: float = 0.10,
        hole_count_tol: int = 0,
        radial_min_ratio: float = 0.20,
        strict: bool = False,
    ) -> "Contract":
        """Derive a default contract from an ariaOS spec dict + goal text.

        Defaults are TIGHT (10% bbox, exact hole count) — loose tolerances were
        the false-PASS pattern. Templates that legitimately need looser checks
        should construct their Contract explicitly with relaxed tolerances.

        Set strict=True to require ±5% bbox and zero radial-ratio slack.
        """
        if strict:
            bbox_tol = 0.05
            hole_count_tol = 0
            radial_min_ratio = 0.30

        spec = spec or {}
        name = spec.get("part_type") or spec.get("part_id") or "part"

        # ── Bbox ─────────────────────────────────────────────────────────────
        bbox: tuple[float, float, float] | None = None
        od = spec.get("od_mm") or spec.get("diameter_mm") or spec.get("outer_dia_mm")
        # thickness_mm is dual-use in this codebase:
        #   - For plates/flanges/discs: thickness IS the bbox Z extent
        #   - For L-brackets etc. when > 20mm: it's a bbox dim (leg height)
        # spec_extractor stores it in both thickness_mm and height_mm. If only
        # thickness_mm came through, treat it as height.
        thk = spec.get("thickness_mm")
        height = spec.get("height_mm")
        if height is None and thk is not None:
            height = thk
        width = spec.get("width_mm")
        depth = spec.get("depth_mm")
        length = spec.get("length_mm")

        if od is not None and height is not None:
            bbox = (float(od), float(od), float(height))
        elif width is not None and depth is not None and height is not None:
            bbox = (float(width), float(depth), float(height))
        elif length is not None and width is not None and height is not None:
            bbox = (float(length), float(width), float(height))
        elif od is not None and length is not None:
            bbox = (float(od), float(od), float(length))

        # ── Hole count ───────────────────────────────────────────────────────
        # n_bolts → mounting holes; bore_mm → +1 center hole.
        n_holes: int | None = None
        n_bolts = spec.get("n_bolts")
        if n_bolts and int(n_bolts) > 0:
            n_holes = int(n_bolts)
        bore_mm = spec.get("bore_mm") or spec.get("id_mm")
        if bore_mm and float(bore_mm) > 0:
            n_holes = (n_holes or 0) + 1

        # ── Radial features ──────────────────────────────────────────────────
        radial: dict[str, Any] = {}
        n_blades = spec.get("n_blades") or spec.get("n_fins")
        n_spokes = spec.get("n_spokes")
        if n_blades and int(n_blades) > 0:
            radial = {
                "n_blades": int(n_blades),
                "min_blade_to_gap_ratio": radial_min_ratio,
            }
        elif n_spokes and int(n_spokes) > 0:
            radial = {
                "n_spokes": int(n_spokes),
                "min_blade_to_gap_ratio": radial_min_ratio * 0.7,
            }

        return cls(
            name=name,
            expected_bbox_mm=bbox,
            bbox_tol=bbox_tol,
            expected_hole_count=n_holes,
            hole_count_tol=hole_count_tol,
            expected_solid_count=1,
            is_watertight=True,
            radial_features=radial,
        )


@dataclass
class ValidationResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measured: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.passed


class ValidationError(Exception):
    def __init__(self, failures: list[str], result: ValidationResult | None = None):
        super().__init__("; ".join(failures))
        self.failures = failures
        self.result = result


def validate_part(shape: Any, contract: Contract) -> ValidationResult:
    """Validate *shape* (a cadquery.Workplane or Solid) against *contract*.

    Returns a ValidationResult — never raises. Caller decides whether to abort.
    All contract fields are optional; unspecified fields are not checked.
    """
    failures: list[str] = []
    warnings: list[str] = []
    measured: dict[str, Any] = {}

    try:
        import cadquery as cq
    except Exception as exc:
        return ValidationResult(
            name=contract.name, passed=False,
            failures=[f"cadquery not available: {exc}"],
        )

    # Get a Solid for measurement
    try:
        if hasattr(shape, "val"):
            solid = shape.val()
        elif hasattr(shape, "Solids"):
            solids = shape.Solids()
            solid = solids[0] if solids else None
        else:
            solid = shape
        if solid is None:
            failures.append("shape produced no solid")
            return ValidationResult(name=contract.name, passed=False, failures=failures)
    except Exception as exc:
        return ValidationResult(
            name=contract.name, passed=False,
            failures=[f"could not extract solid: {type(exc).__name__}: {exc}"],
        )

    # ── BBox check ────────────────────────────────────────────────────────────
    try:
        bb = solid.BoundingBox()
        bbox = (bb.xlen, bb.ylen, bb.zlen)
        measured["bbox_mm"] = list(bbox)
        if contract.expected_bbox_mm is not None:
            for axis, (got, want) in enumerate(zip(bbox, contract.expected_bbox_mm)):
                if want <= 0:
                    continue
                tol = contract.bbox_tol * want
                if abs(got - want) > tol:
                    axis_name = "XYZ"[axis]
                    failures.append(
                        f"bbox {axis_name}: got {got:.2f}mm, expected {want:.2f}mm "
                        f"(±{tol:.2f}mm = {contract.bbox_tol*100:.0f}%)"
                    )
    except Exception as exc:
        warnings.append(f"bbox check skipped: {exc}")

    # ── Volume check ──────────────────────────────────────────────────────────
    try:
        vol = solid.Volume()
        measured["volume_mm3"] = vol
        if contract.min_volume_mm3 is not None and vol < contract.min_volume_mm3:
            failures.append(f"volume {vol:.1f}mm³ below min {contract.min_volume_mm3:.1f}mm³")
        if contract.max_volume_mm3 is not None and vol > contract.max_volume_mm3:
            failures.append(f"volume {vol:.1f}mm³ above max {contract.max_volume_mm3:.1f}mm³")
    except Exception as exc:
        warnings.append(f"volume check skipped: {exc}")

    # ── Hole count via Euler characteristic ───────────────────────────────────
    if contract.expected_hole_count is not None:
        try:
            n_holes = _count_holes(solid)
            measured["hole_count"] = n_holes
            target = contract.expected_hole_count
            if abs(n_holes - target) > contract.hole_count_tol:
                failures.append(
                    f"hole count: got {n_holes}, expected {target} "
                    f"(tol ±{contract.hole_count_tol})"
                )
        except Exception as exc:
            warnings.append(f"hole count skipped: {exc}")

    # ── Solid count ───────────────────────────────────────────────────────────
    if contract.expected_solid_count is not None:
        try:
            if hasattr(shape, "Solids"):
                n_solids = len(shape.Solids())
            else:
                n_solids = 1
            measured["solid_count"] = n_solids
            if n_solids != contract.expected_solid_count:
                failures.append(
                    f"solid count: got {n_solids}, expected {contract.expected_solid_count}"
                )
        except Exception as exc:
            warnings.append(f"solid count skipped: {exc}")

    # ── Watertight check (via STL conversion) ─────────────────────────────────
    if contract.is_watertight is not None:
        try:
            wt = _check_watertight(solid)
            measured["is_watertight"] = wt
            if wt != contract.is_watertight:
                failures.append(
                    f"watertight: got {wt}, expected {contract.is_watertight}"
                )
        except Exception as exc:
            warnings.append(f"watertight check skipped: {exc}")

    # ── Radial features (n_blades, n_arms, n_spokes, etc.) ────────────────────
    if contract.radial_features:
        try:
            n_target = int(contract.radial_features.get("n_blades")
                           or contract.radial_features.get("n_arms")
                           or contract.radial_features.get("n_spokes")
                           or 0)
            if n_target > 0:
                n_detected, ratio = _detect_radial_lobes(solid)
                measured["radial_lobes_detected"] = n_detected
                measured["radial_lobe_to_gap_ratio"] = round(ratio, 3)
                if n_detected != n_target:
                    failures.append(
                        f"radial features: detected {n_detected} lobes, "
                        f"expected {n_target}"
                    )
                min_ratio = float(contract.radial_features.get("min_blade_to_gap_ratio", 0.0))
                if 0 < min_ratio and ratio < min_ratio:
                    failures.append(
                        f"radial uniformity: lobe/gap ratio {ratio:.2f} below "
                        f"min {min_ratio:.2f} (likely solid disc, not lobed)"
                    )
        except Exception as exc:
            warnings.append(f"radial features skipped: {exc}")

    # ── Custom checks ─────────────────────────────────────────────────────────
    for entry in contract.custom_checks:
        try:
            label, fn = entry
            ok = bool(fn(shape))
            if not ok:
                failures.append(f"custom check '{label}' failed")
        except Exception as exc:
            warnings.append(f"custom check raised: {exc}")

    return ValidationResult(
        name=contract.name,
        passed=(len(failures) == 0),
        failures=failures,
        warnings=warnings,
        measured=measured,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_holes(solid: Any) -> int:
    """Count through-holes (genus) via Euler characteristic of the tessellated mesh.

    For a closed orientable triangle mesh: V - E + F = 2 - 2g, where g is
    the genus (number of through-holes / handles). Uses trimesh-derived
    vertex/edge/face counts because OCCT face topology counts are unreliable
    for trimmed geometry — a single OCCT face can wrap multiple disconnected
    boundaries which throws off the V-E+F arithmetic.
    """
    import tempfile
    import trimesh
    import cadquery as cq
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tf:
        tmp_path = tf.name
    cq.exporters.export(solid, tmp_path, exportType="STL", tolerance=0.05)
    mesh = trimesh.load_mesh(tmp_path)
    Path(tmp_path).unlink(missing_ok=True)
    if hasattr(mesh, "dump"):
        mesh = mesh.dump(concatenate=True)

    # Merge duplicate vertices so V-E+F accounting is correct for shared edges
    mesh.merge_vertices()
    n_v = len(mesh.vertices)
    n_f = len(mesh.faces)
    n_e = len(mesh.edges_unique)
    euler = n_v - n_e + n_f
    genus = max(0, (2 - euler) // 2)
    return int(genus)


def _check_watertight(solid: Any) -> bool:
    """Convert to mesh, check trimesh's watertight property."""
    try:
        import trimesh
        mesh = _solid_to_mesh(solid, tolerance=0.05)
        if hasattr(mesh, "is_watertight"):
            return bool(mesh.is_watertight)
        return False
    except Exception:
        return False


def _solid_to_mesh(solid: Any, tolerance: float = 0.05):
    """Convert a cadquery solid to a trimesh mesh, with caching by solid bytes.

    The cache key is the SHA-256 of the STEP-serialized solid. Same solid +
    same tolerance → cache hit, no re-tessellation. Used by every place that
    needs a mesh for genus / radial / watertight checks.
    """
    import hashlib
    import tempfile
    import trimesh
    import cadquery as cq
    from pathlib import Path
    from aria_os.caching import _cache_root

    # Serialize solid to STEP bytes for hashing
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as tf:
        step_tmp = tf.name
    try:
        cq.exporters.export(solid, step_tmp)
        digest = hashlib.sha256(Path(step_tmp).read_bytes()).hexdigest()
        cache_dir = _cache_root() / "solid_mesh"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_stl = cache_dir / f"{digest}_t{int(tolerance * 1000):04d}.stl"
        if not cached_stl.is_file() or cached_stl.stat().st_size == 0:
            cq.exporters.export(solid, str(cached_stl), exportType="STL", tolerance=tolerance)
        return trimesh.load_mesh(str(cached_stl))
    finally:
        Path(step_tmp).unlink(missing_ok=True)


def _detect_radial_lobes(solid: Any) -> tuple[int, float]:
    """Detect rotational lobes (blades, arms, spokes) by sampling cross-section.

    Method:
    1. Convert to STL mesh
    2. Find the solid's bounding box; pick the axis perpendicular to the lobe
       plane (typically Z for props, Y for impellers — we use the axis with
       smallest extent as the "thin" axis)
    3. Take a midplane cross-section perpendicular to that axis
    4. Sample radii at N=360 angles around the centroid
    5. Count peaks above the mean (= lobes); compute peak/trough amplitude ratio

    Returns (n_lobes_detected, lobe_to_gap_amplitude_ratio).
    """
    import tempfile
    import trimesh
    import numpy as np
    import cadquery as cq
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tf:
        tmp_path = tf.name
    cq.exporters.export(solid, tmp_path, exportType="STL")
    mesh = trimesh.load_mesh(tmp_path)
    Path(tmp_path).unlink(missing_ok=True)

    if hasattr(mesh, "dump"):
        mesh = mesh.dump(concatenate=True)

    # Pick thinnest axis (the prop is thin along Z, impeller along Z, etc.)
    extents = mesh.extents  # (x, y, z) sizes
    thin_axis = int(np.argmin(extents))
    # Two in-plane axes:
    in_plane = [a for a in range(3) if a != thin_axis]

    # Cross-section at midplane
    plane_origin = mesh.centroid.copy()
    plane_normal = np.zeros(3)
    plane_normal[thin_axis] = 1.0
    section = mesh.section(plane_origin=plane_origin, plane_normal=plane_normal)
    if section is None:
        return (0, 0.0)
    # Project to 2D
    section_2d, _ = section.to_planar()
    # Use raw discrete polylines (no shapely dep). discrete returns a list
    # of (N, 2) arrays — one per closed loop in the cross-section.
    try:
        loops = section_2d.discrete
    except Exception:
        return (0, 0.0)
    if not loops:
        return (0, 0.0)
    pts = np.vstack([np.asarray(loop) for loop in loops if len(loop) >= 3])
    if len(pts) < 8:
        return (0, 0.0)
    # Compute centroid (in 2D)
    c = pts.mean(axis=0)
    rel = pts - c
    angles = np.arctan2(rel[:, 1], rel[:, 0])
    radii = np.linalg.norm(rel, axis=1)

    # Bin by angle (360 bins, 1 deg each), take MAX radius per bin
    n_bins = 360
    bin_idx = ((angles + math.pi) / (2 * math.pi) * n_bins).astype(int) % n_bins
    profile = np.zeros(n_bins)
    for i, r in zip(bin_idx, radii):
        if r > profile[i]:
            profile[i] = r
    # Fill empty bins with neighbor
    if (profile == 0).any():
        nz = profile > 0
        if nz.sum() < 8:
            return (0, 0.0)
        idx_nz = np.where(nz)[0]
        for i in range(n_bins):
            if profile[i] == 0:
                near = idx_nz[np.argmin(np.abs(idx_nz - i))]
                profile[i] = profile[near]

    # Heavy smoothing — kill tessellation noise, keep real lobes
    kernel = np.ones(15) / 15.0
    smoothed = np.convolve(profile, kernel, mode="same")
    # Re-smooth wraparound by applying twice on padded signal
    padded = np.concatenate([smoothed[-15:], smoothed, smoothed[:15]])
    padded = np.convolve(padded, kernel, mode="same")
    smoothed = padded[15:-15]

    # FFT-based lobe detection — most reliable for periodic lobes
    spectrum = np.abs(np.fft.rfft(smoothed - smoothed.mean()))
    spectrum[0] = 0  # ignore DC
    if len(spectrum) <= 1 or spectrum.max() < 1e-6:
        return (0, 0.0)
    dominant_freq = int(np.argmax(spectrum))
    # FFT bin = number of full sinusoidal cycles in 360 deg = number of lobes
    n_lobes = int(dominant_freq)

    # Lobe-to-gap ratio: (max - min) / max, normalized
    span = smoothed.max() - smoothed.min()
    ratio = float(span / max(smoothed.max(), 1e-6))

    return (n_lobes, ratio)
