"""
Composite layup module — ply-by-ply stackup definition for CFRP/GFRP parts.

Used for hypercar body panels, drone arms, missile airframes — anywhere carbon
or glass fiber laminates are the primary structural material.

Provides:
- Ply-by-ply stackup definition (material, orientation, thickness)
- Laminate property homogenization (Classical Laminate Theory)
- Stacking sequence validation (symmetry, balance, 10% rule)
- Export to simulation formats (Abaqus/Nastran/OptiStruct)
- Common fabric/fiber catalog (IM7/5320, T800S/3900, E-glass/epoxy)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Fiber / fabric / matrix catalog — unidirectional tape unless noted
# Properties: E1 (fiber dir), E2 (trans), G12 (shear), v12 (Poisson),
#             density, cured ply thickness, max strain
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompositeMaterial:
    """A single prepreg or wet-layup material."""
    designation: str
    description: str

    # Engineering properties (GPa for moduli, unitless for Poisson)
    E1_gpa: float       # longitudinal modulus (fiber direction)
    E2_gpa: float       # transverse modulus
    G12_gpa: float      # in-plane shear modulus
    v12: float          # major Poisson's ratio

    # Strength (MPa)
    Xt_mpa: float       # tensile strength, longitudinal
    Xc_mpa: float       # compressive strength, longitudinal
    Yt_mpa: float       # tensile strength, transverse
    Yc_mpa: float       # compressive strength, transverse
    S_mpa: float        # in-plane shear strength

    density_g_cm3: float
    cured_ply_thickness_mm: float
    cure_temp_c: float = 120.0
    cure_time_hr: float = 2.0

    # Cost and availability
    cost_usd_per_kg: float = 60.0
    supplier: str = ""

    # Max operating temp before matrix degrades
    max_operating_temp_c: float = 120.0


MATERIALS: dict[str, CompositeMaterial] = {
    # Aerospace-grade IM7 carbon / 5320 toughened epoxy (Cytec)
    "IM7/5320-1": CompositeMaterial(
        designation="IM7/5320-1",
        description="Intermediate-modulus carbon UD, aerospace-grade toughened epoxy",
        E1_gpa=165.0, E2_gpa=8.4, G12_gpa=4.1, v12=0.34,
        Xt_mpa=2800, Xc_mpa=1600, Yt_mpa=60, Yc_mpa=200, S_mpa=90,
        density_g_cm3=1.58, cured_ply_thickness_mm=0.125,
        cure_temp_c=177, cure_time_hr=2,
        cost_usd_per_kg=180, supplier="Cytec Solvay",
        max_operating_temp_c=150,
    ),
    # High-strength T800S / 3900 (Toray) — race car / aerospace primary structure
    "T800S/3900-2": CompositeMaterial(
        designation="T800S/3900-2",
        description="High-strength carbon UD, toughened epoxy; F1 chassis + aerostructure",
        E1_gpa=155.0, E2_gpa=8.3, G12_gpa=4.1, v12=0.33,
        Xt_mpa=2950, Xc_mpa=1650, Yt_mpa=65, Yc_mpa=210, S_mpa=95,
        density_g_cm3=1.59, cured_ply_thickness_mm=0.131,
        cure_temp_c=177, cure_time_hr=2,
        cost_usd_per_kg=220, supplier="Toray",
        max_operating_temp_c=150,
    ),
    # Consumer-grade 3K 2x2 twill carbon / resin infusion — drones, hobbyist
    "3K_2x2_twill_epoxy": CompositeMaterial(
        designation="3K_2x2_twill_epoxy",
        description="3K 2x2 twill weave carbon / room-temp epoxy (wet layup)",
        E1_gpa=55.0, E2_gpa=55.0,  # balanced woven, near-isotropic in-plane
        G12_gpa=4.0, v12=0.10,
        Xt_mpa=600, Xc_mpa=570, Yt_mpa=600, Yc_mpa=570, S_mpa=90,
        density_g_cm3=1.50, cured_ply_thickness_mm=0.25,
        cure_temp_c=25, cure_time_hr=24,
        cost_usd_per_kg=55, supplier="Fibre Glast / Soller",
        max_operating_temp_c=80,
    ),
    # E-glass / vinyl ester — industrial panels, wind turbine blades
    "Eglass_VE": CompositeMaterial(
        designation="Eglass_VE",
        description="E-glass UD / vinyl ester — industrial + marine",
        E1_gpa=45.0, E2_gpa=12.0, G12_gpa=5.5, v12=0.28,
        Xt_mpa=1020, Xc_mpa=620, Yt_mpa=40, Yc_mpa=140, S_mpa=70,
        density_g_cm3=1.95, cured_ply_thickness_mm=0.3,
        cure_temp_c=25, cure_time_hr=12,
        cost_usd_per_kg=8, supplier="Owens Corning / PPG",
        max_operating_temp_c=90,
    ),
    # Kevlar 49 / epoxy — ballistic, high-strain applications
    "Kevlar49_epoxy": CompositeMaterial(
        designation="Kevlar49_epoxy",
        description="Kevlar 49 aramid UD / epoxy — impact + ballistic",
        E1_gpa=76.0, E2_gpa=5.5, G12_gpa=2.3, v12=0.34,
        Xt_mpa=1380, Xc_mpa=280, Yt_mpa=30, Yc_mpa=140, S_mpa=60,
        density_g_cm3=1.38, cured_ply_thickness_mm=0.13,
        cure_temp_c=120, cure_time_hr=2,
        cost_usd_per_kg=95, supplier="DuPont",
        max_operating_temp_c=140,
    ),
}


# ---------------------------------------------------------------------------
# Stackup definition
# ---------------------------------------------------------------------------

@dataclass
class Ply:
    """A single ply in a laminate stackup."""
    material: str       # key into MATERIALS
    angle_deg: float    # fiber orientation relative to part X axis
    thickness_mm: float | None = None   # None -> use material's cured ply thickness

    def get_thickness(self) -> float:
        if self.thickness_mm is not None:
            return self.thickness_mm
        mat = MATERIALS.get(self.material)
        return mat.cured_ply_thickness_mm if mat else 0.125


@dataclass
class Stackup:
    """A laminate stackup (ordered list of plies bottom → top)."""
    name: str
    plies: list[Ply] = field(default_factory=list)
    description: str = ""

    def total_thickness_mm(self) -> float:
        return sum(p.get_thickness() for p in self.plies)

    def total_mass_per_area_g_m2(self) -> float:
        m = 0.0
        for p in self.plies:
            mat = MATERIALS.get(p.material)
            if mat is None:
                continue
            m += mat.density_g_cm3 * p.get_thickness() * 1000  # g/m²
        return m

    def is_symmetric(self) -> bool:
        """True if the stackup is symmetric about the mid-plane."""
        n = len(self.plies)
        for i in range(n // 2):
            top = self.plies[i]
            bot = self.plies[n - 1 - i]
            if top.material != bot.material or top.angle_deg != bot.angle_deg:
                return False
            if abs(top.get_thickness() - bot.get_thickness()) > 1e-6:
                return False
        return True

    def is_balanced(self) -> bool:
        """True if every +θ ply has a matching -θ ply (θ != 0, 90)."""
        counts: dict[tuple[str, float], int] = {}
        for p in self.plies:
            key = (p.material, p.angle_deg)
            counts[key] = counts.get(key, 0) + 1
        for (mat, ang), n in counts.items():
            if ang in (0.0, 90.0, -90.0):
                continue
            opposite = (mat, -ang)
            if counts.get(opposite, 0) != n:
                return False
        return True

    def meets_10_percent_rule(self) -> bool:
        """Classical aerospace rule: at least 10% of plies in each of 0/45/-45/90."""
        n = len(self.plies)
        if n == 0:
            return False
        counts = {"0": 0, "45": 0, "-45": 0, "90": 0}
        for p in self.plies:
            a = p.angle_deg % 180
            if abs(a) < 1:
                counts["0"] += 1
            elif abs(a - 45) < 1:
                counts["45"] += 1
            elif abs(a - 135) < 1:
                counts["-45"] += 1
            elif abs(a - 90) < 1:
                counts["90"] += 1
        return all(c / n >= 0.10 for c in counts.values())

    def validate(self) -> list[str]:
        """Return list of validation warnings (empty = clean)."""
        warnings: list[str] = []
        if not self.plies:
            warnings.append("Stackup has no plies")
            return warnings
        if not self.is_symmetric():
            warnings.append("Stackup is not symmetric — expect bend-stretch coupling")
        if not self.is_balanced():
            warnings.append("Stackup is not balanced — expect in-plane shear-normal coupling")
        if not self.meets_10_percent_rule():
            warnings.append("Stackup does not meet 10% rule in all four families (0/±45/90)")
        # Material consistency — warn if mixing cure temperatures
        cure_temps = {MATERIALS[p.material].cure_temp_c for p in self.plies
                      if p.material in MATERIALS}
        if len(cure_temps) > 1:
            warnings.append(f"Mixed cure temperatures: {sorted(cure_temps)}°C — "
                           "may require autoclave tuning")
        return warnings

    def to_bom_entries(self, part_area_m2: float) -> list[dict[str, Any]]:
        """Per-material BOM entries for the area of this part."""
        area_per_material: dict[str, float] = {}
        mass_per_material: dict[str, float] = {}
        for p in self.plies:
            area_per_material[p.material] = area_per_material.get(p.material, 0) + part_area_m2
            mat = MATERIALS.get(p.material)
            if mat:
                mass = mat.density_g_cm3 * p.get_thickness() * 1000 * part_area_m2
                mass_per_material[p.material] = mass_per_material.get(p.material, 0) + mass

        entries = []
        for mat_key, area in area_per_material.items():
            mat = MATERIALS.get(mat_key)
            mass_g = mass_per_material.get(mat_key, 0)
            cost = (mass_g / 1000) * (mat.cost_usd_per_kg if mat else 100)
            entries.append({
                "material": mat_key,
                "area_m2": round(area, 3),
                "mass_g": round(mass_g, 1),
                "total_cost_usd": round(cost, 2),
                "description": mat.description if mat else "unknown material",
                "cure_temp_c": mat.cure_temp_c if mat else None,
            })
        return entries


# ---------------------------------------------------------------------------
# Common preset stackups
# ---------------------------------------------------------------------------

def quasi_isotropic_8ply(material: str = "IM7/5320-1") -> Stackup:
    """Classic [0/45/-45/90]s quasi-iso laminate — 8 plies, symmetric & balanced."""
    angles = [0, 45, -45, 90, 90, -45, 45, 0]
    return Stackup(
        name=f"QI_8ply_{material}",
        plies=[Ply(material, a) for a in angles],
        description="Quasi-isotropic 8-ply [0/45/-45/90]s",
    )


def cross_ply_4(material: str = "IM7/5320-1") -> Stackup:
    """Cross-ply [0/90]s — anisotropic in-plane, for stiffened panels."""
    return Stackup(
        name=f"cross_ply_4_{material}",
        plies=[Ply(material, a) for a in (0, 90, 90, 0)],
        description="4-ply symmetric cross-ply [0/90]s",
    )


def racing_monocoque_layup(n_plies: int = 16, material: str = "T800S/3900-2") -> Stackup:
    """Typical F1/race car tub layup — heavy on 0° + ±45° for torsional stiffness."""
    n_0 = n_plies // 2  # 50% zero-degree
    n_45 = n_plies // 4
    n_m45 = n_plies // 4
    angles = [0] * n_0 + [45] * n_45 + [-45] * n_m45
    # Mirror for symmetry
    half = angles[:n_plies // 2]
    full = half + list(reversed(half))
    return Stackup(
        name=f"race_tub_{n_plies}ply",
        plies=[Ply(material, a) for a in full],
        description=f"Race monocoque layup, {n_plies} plies of {material}",
    )


def drone_arm_layup(n_plies: int = 6) -> Stackup:
    """Thin drone arm — 3K twill weave, 6 plies at 0/45/0/0/45/0."""
    angles = [0, 45, 0, 0, 45, 0][:n_plies]
    return Stackup(
        name=f"drone_arm_{n_plies}ply",
        plies=[Ply("3K_2x2_twill_epoxy", a) for a in angles],
        description=f"Drone arm layup, {n_plies} plies of 2x2 twill",
    )


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def stackup_to_dict(stackup: Stackup) -> dict[str, Any]:
    """Serialize a stackup for JSON persistence or external simulation tools."""
    return {
        "name": stackup.name,
        "description": stackup.description,
        "total_thickness_mm": round(stackup.total_thickness_mm(), 3),
        "mass_per_area_g_m2": round(stackup.total_mass_per_area_g_m2(), 1),
        "n_plies": len(stackup.plies),
        "is_symmetric": stackup.is_symmetric(),
        "is_balanced": stackup.is_balanced(),
        "meets_10_percent_rule": stackup.meets_10_percent_rule(),
        "plies": [
            {"material": p.material, "angle_deg": p.angle_deg,
             "thickness_mm": p.get_thickness()}
            for p in stackup.plies
        ],
    }


def stackup_to_abaqus_comp_layup(stackup: Stackup) -> str:
    """
    Generate an Abaqus *COMPOSITE LAYUP block for FE simulation import.
    Returns a string that can be pasted into an Abaqus .inp file.
    """
    lines = [f"*COMPOSITE LAYUP, NAME={stackup.name}, SYMMETRIC"
             if stackup.is_symmetric() else
             f"*COMPOSITE LAYUP, NAME={stackup.name}"]
    plies_to_write = stackup.plies
    if stackup.is_symmetric():
        plies_to_write = stackup.plies[:len(stackup.plies) // 2]
    for i, p in enumerate(plies_to_write, 1):
        lines.append(f"  {p.get_thickness():.4f}, 3, {p.material.replace('/', '_')}, "
                     f"{p.angle_deg:g}, Ply-{i}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Classical Laminate Theory — simple A-matrix homogenization
# ---------------------------------------------------------------------------

def homogenized_in_plane_moduli(stackup: Stackup) -> dict[str, float]:
    """
    Compute homogenized in-plane laminate moduli (Ex, Ey, Gxy, vxy) via CLT.

    Returns dict with GPa values. Assumes thin-laminate plane-stress.
    This is a simplification — for full CLT including bending (D-matrix),
    use a dedicated tool. Good enough for layup sizing and material selection.
    """
    # Build A-matrix by summing Qbar * thickness for each ply
    A = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    total_t = 0.0
    for ply in stackup.plies:
        mat = MATERIALS.get(ply.material)
        if mat is None:
            continue
        t = ply.get_thickness()
        total_t += t
        Q = _reduced_stiffness(mat)
        Qbar = _rotate_stiffness(Q, math.radians(ply.angle_deg))
        for i in range(3):
            for j in range(3):
                A[i][j] += Qbar[i][j] * t

    if total_t <= 0:
        return {"Ex_gpa": 0, "Ey_gpa": 0, "Gxy_gpa": 0, "vxy": 0}

    # Invert A to get in-plane compliance
    # Normalized by total thickness (back to GPa)
    a = _invert_3x3(A)
    h = total_t
    Ex = 1.0 / (a[0][0] * h)
    Ey = 1.0 / (a[1][1] * h)
    Gxy = 1.0 / (a[2][2] * h)
    vxy = -a[0][1] / a[0][0]
    return {
        "Ex_gpa": round(Ex, 2), "Ey_gpa": round(Ey, 2),
        "Gxy_gpa": round(Gxy, 2), "vxy": round(vxy, 4),
    }


def _reduced_stiffness(mat: CompositeMaterial) -> list[list[float]]:
    """Plane-stress reduced stiffness matrix Q for a single lamina (GPa)."""
    E1, E2, G12, v12 = mat.E1_gpa, mat.E2_gpa, mat.G12_gpa, mat.v12
    v21 = v12 * E2 / E1
    denom = 1.0 - v12 * v21
    Q11 = E1 / denom
    Q22 = E2 / denom
    Q12 = v12 * E2 / denom
    return [[Q11, Q12, 0.0],
            [Q12, Q22, 0.0],
            [0.0, 0.0, G12]]


def _rotate_stiffness(Q: list[list[float]], theta_rad: float) -> list[list[float]]:
    """Rotate Q matrix by angle theta (radians) -> Q-bar."""
    c = math.cos(theta_rad)
    s = math.sin(theta_rad)
    c2, s2 = c * c, s * s
    cs = c * s

    Q11, Q12, Q22, Q66 = Q[0][0], Q[0][1], Q[1][1], Q[2][2]
    Q11b = Q11 * c2 * c2 + 2 * (Q12 + 2 * Q66) * c2 * s2 + Q22 * s2 * s2
    Q22b = Q11 * s2 * s2 + 2 * (Q12 + 2 * Q66) * c2 * s2 + Q22 * c2 * c2
    Q12b = (Q11 + Q22 - 4 * Q66) * c2 * s2 + Q12 * (c2 * c2 + s2 * s2)
    Q66b = (Q11 + Q22 - 2 * Q12 - 2 * Q66) * c2 * s2 + Q66 * (c2 * c2 + s2 * s2)
    Q16b = (Q11 - Q12 - 2 * Q66) * c2 * cs - (Q22 - Q12 - 2 * Q66) * s2 * cs
    Q26b = (Q11 - Q12 - 2 * Q66) * s2 * cs - (Q22 - Q12 - 2 * Q66) * c2 * cs
    return [[Q11b, Q12b, Q16b],
            [Q12b, Q22b, Q26b],
            [Q16b, Q26b, Q66b]]


def _invert_3x3(M: list[list[float]]) -> list[list[float]]:
    """Invert a 3x3 matrix (no numpy dependency).

    Raises ValueError on singular / near-singular input. Previously returned
    a zero matrix silently — that propagated NaN/inf into "moduli" without
    warning. Singular A-matrix means the laminate is degenerate (e.g. all
    plies at the same angle so the in-plane stiffness tensor is rank-deficient).
    """
    a, b, c = M[0]
    d, e, f = M[1]
    g, h, i = M[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError(
            f"Laminate stiffness matrix is singular (det={det:.2e}). "
            "Likely cause: degenerate stackup (plies at the same angle "
            "without orthogonal contribution). Add ±45 or 90 plies for balance."
        )
    inv = [
        [(e * i - f * h) / det, -(b * i - c * h) / det, (b * f - c * e) / det],
        [-(d * i - f * g) / det, (a * i - c * g) / det, -(a * f - c * d) / det],
        [(d * h - e * g) / det, -(a * h - b * g) / det, (a * e - b * d) / det],
    ]
    return inv
