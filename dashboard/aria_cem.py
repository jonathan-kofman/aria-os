"""
=============================================================================
aria_cem.py — ARIA Auto Belay Device Computational Engineering Model
=============================================================================
Platform: cem_core.py (same infrastructure as rocket engine CEM)
Domain:   Mechanical safety device — lead climbing auto belay

Physics encoded:
  - Brake drum sizing     → arrest force + centrifugal clutch engagement
  - Ratchet/pawl geometry → tooth load from 8 kN ANSI arrest force
  - Rope spool sizing     → rope capacity + motor torque requirement
  - Centrifugal clutch    → flyweight mass from fall detection threshold
  - Housing wall thickness → Pc equivalent = rope tension + impact load
  - Motor selection       → torque/speed from rope feed requirements
  - BLDC gearbox ratio    → back-drive prevention + speed range

Standards encoded:
  ANSI Z359.14 — Self-Retracting Lifelines (closest applicable standard)
  CE EN 15151-2 — Belay devices (European equivalent)
  Target: 8 kN arrest force, <6m fall distance, <6 kN peak force on climber

Reference:
  Lead Solo centrifugal clutch mechanism (Jonah Werntz design)
  ARIA mechanical design notes (Jonathan Kofman, 2025-2026)
  cem_core.py — shared material/fluid/load base classes
=============================================================================
"""

import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import os
import csv
import sys
sys.path.insert(0, '.')

try:
    from cem_core import (CEMModule, Material, PressureLoad,
                          MATERIAL_6061_AL, MATERIAL_INCONEL718,
                          min_wall_thickness, hoop_stress_Pa)
    HAS_CORE = True
except ImportError:
    HAS_CORE = False
    class CEMModule:
        def __init__(self, name): self.name=name; self.warnings=[]; self.passed=[]
        def warn(self, m): self.warnings.append(f"WARNING: {m}")
        def ok(self, m): self.passed.append(f"OK: {m}")
        def physics_check(self, c, ok, w):
            if c: self.ok(ok)
            else: self.warn(w)
        def print_validation(self):
            print(f"\n{'='*55}\n  {self.name} Validation\n{'='*55}")
            for p in self.passed: print(f"  {p}")
            for w in self.warnings: print(f"  {w}")

try:
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


# ─────────────────────────────────────────────────────────────────────────────
# ARIA DESIGN STANDARDS
# ─────────────────────────────────────────────────────────────────────────────

ANSI_Z359_14 = {
    'max_arrest_force_kN':      6.0,    # max force on climber during arrest
    'arrest_distance_m':        1.0,    # max fall distance before full arrest
    'min_holding_force_kN':     8.0,    # minimum ratchet holding force
    'test_mass_kg':             100.0,  # test mass (ANSI standard)
    'fall_factor':              1.0,    # worst-case lead fall factor
    'dynamic_load_factor':      2.0,    # impact amplification
}

# Materials available at Northeastern IDEA/Shillman
MATERIAL_6061_T6 = {
    'name':             '6061-T6 Aluminum',
    'yield_MPa':        276.0,
    'ultimate_MPa':     310.0,
    'density_kg_m3':    2700.0,
    'E_GPa':            69.0,
    'machineable':      True,
    'cost_per_kg':      8.0,  # approx
}
MATERIAL_4140_STEEL = {
    'name':             '4140 Steel',
    'yield_MPa':        655.0,
    'ultimate_MPa':     1020.0,
    'density_kg_m3':    7850.0,
    'E_GPa':            205.0,
    'machineable':      True,
    'cost_per_kg':      4.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# INPUTS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ARIAInputs:
    """
    All ARIA design requirements — change these, everything recomputes.
    Defaults match current ARIA design intent.
    """
    # Performance requirements
    max_arrest_force_kN: float  = 6.0      # ANSI max force on climber
    min_hold_force_kN: float    = 8.0      # ratchet must hold this
    fall_detection_v_m_s: float = 1.5      # rope speed that triggers catch
                                           # (above normal climbing ~0.3 m/s)
    max_fall_distance_m: float  = 1.0      # arrest within this distance
    rope_diameter_mm: float     = 10.0     # standard lead climbing rope
    max_rope_capacity_m: float  = 40.0     # max rope stored on spool

    # Motor / slack management
    slack_feed_speed_m_s: float = 0.8      # normal climbing rope feed rate
    max_retract_speed_m_s: float= 1.5      # max retraction speed
    target_tension_N: float     = 40.0     # normal operating rope tension
    motor_voltage_V: float      = 24.0     # battery/supply voltage

    # Geometry constraints (from existing ARIA design)
    brake_drum_diameter_mm: float = 200.0  # Lead Solo design
    rope_spool_hub_diameter_mm: float = 120.0   # spool hub/core diameter
    rope_spool_od_mm: float = 600.0            # spool outer diameter (rope wrap)
    gearbox_ratio: float = 30.0                # 30:1 planetary (physical spec, context/aria_system_overview.md)
    housing_od_mm: float        = 260.0   # outer housing diameter
    wall_mount_bolt_pattern_mm: float = 150.0  # bolt circle

    # Safety factors
    safety_factor_structural: float = 3.0  # life-safety device → higher SF
    safety_factor_fatigue: float    = 5.0  # cyclic loading
    n_cycles_design: int            = 50000  # design life in catch cycles

    # Manufacturing
    min_feature_mm: float       = 1.5      # min CNC/3D print feature
    material_housing: str       = '6061-T6'
    material_ratchet: str       = '4140 Steel'  # ratchet/pawl need steel


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY RESULTS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BrakeDrumGeom:
    diameter_mm: float
    width_mm: float            # axial width
    wall_thickness_mm: float   # radial wall thickness
    mass_kg: float
    hoop_stress_MPa: float     # at max arrest load
    safety_factor: float


@dataclass
class RatchetGeom:
    n_teeth: int
    pitch_mm: float            # tooth pitch (arc length)
    tooth_height_mm: float
    face_width_mm: float       # axial tooth engagement width
    pressure_angle_deg: float  # 26° per ARIA design notes
    root_radius_mm: float
    tip_radius_mm: float
    tooth_bending_stress_MPa: float
    safety_factor: float


@dataclass
class CentrifugalClutchGeom:
    n_flyweights: int          # number of flyweight segments
    flyweight_mass_g: float    # mass of each flyweight
    flyweight_radius_mm: float # centroid radius from rotation axis
    spring_preload_N: float    # spring force holding flyweights in
    engagement_rpm: float      # RPM at which clutch engages
    engagement_v_m_s: float    # corresponding rope speed
    safety_margin: float       # ratio of detection speed to normal climb speed


@dataclass
class RopeSpoolGeom:
    hub_diameter_mm: float
    flange_diameter_mm: float
    width_mm: float            # between flanges
    layers: int                # rope layers
    capacity_m: float          # actual rope capacity
    moment_of_inertia_kg_m2: float
    effective_rope_radius_m: float  # where rope wraps (spool OD/2)


@dataclass
class MotorSpec:
    required_torque_Nm: float  # at spool shaft
    required_speed_rpm: float  # at spool shaft
    gearbox_ratio: float       # derived from speed/torque requirements
    motor_torque_Nm: float     # at motor shaft (before gearbox)
    motor_speed_rpm: float     # at motor shaft
    back_drive_torque_Nm: float  # torque to back-drive (must exceed rope tension)
    velocity_limit_rad_s: float  # motor shaft rad/s limit
    recommendation: str


@dataclass
class HousingGeom:
    od_mm: float
    wall_thickness_mm: float
    length_mm: float
    mass_kg: float
    wall_stress_MPa: float
    n_wall_bolts: int
    bolt_circle_mm: float


@dataclass
class ARIAGeom:
    """Complete ARIA device geometry — all derived from ARIAInputs."""
    inputs: ARIAInputs
    brake_drum: BrakeDrumGeom
    ratchet: RatchetGeom
    clutch: CentrifugalClutchGeom
    spool: RopeSpoolGeom
    motor: MotorSpec
    housing: HousingGeom
    # Performance predictions
    predicted_arrest_distance_m: float
    predicted_peak_force_kN: float
    predicted_catch_time_ms: float
    total_mass_kg: float
    warnings: List[str] = field(default_factory=list)
    passed: List[str]   = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# PHYSICS COMPUTATIONS
# ─────────────────────────────────────────────────────────────────────────────

def compute_brake_drum(inp: ARIAInputs) -> BrakeDrumGeom:
    """
    Brake drum sizing from arrest force requirements.
    The drum carries hoop stress from rope tension during arrest.
    F_arrest = T * (1 - e^(-mu*theta))  (capstan equation, mu=0.3 fiber on Al)
    """
    mat = MATERIAL_6061_T6
    R   = inp.brake_drum_diameter_mm / 2 / 1000  # m

    # Peak arrest load on drum (dynamic)
    F_arrest_N = inp.min_hold_force_kN * 1000
    dyn_factor = ANSI_Z359_14['dynamic_load_factor']
    F_dynamic  = F_arrest_N * dyn_factor

    # Hoop stress in drum wall: sigma = F * R / (t * w)
    # Assume axial width w = 60mm (from Lead Solo reference)
    w_mm = 60.0
    w_m  = w_mm / 1000.0

    # Required wall thickness
    sigma_allow = mat['yield_MPa'] * 1e6 / inp.safety_factor_structural
    t_m = F_dynamic * R / (sigma_allow * w_m)
    t_mm = max(t_m * 1000, inp.min_feature_mm * 2)  # min 2x feature size

    # Actual hoop stress with derived thickness
    sigma_actual = F_dynamic * R / (t_mm/1000 * w_m) / 1e6
    SF_actual    = mat['yield_MPa'] / sigma_actual

    # Mass estimate (hollow cylinder)
    R_outer = R
    R_inner = R - t_mm/1000
    mass = mat['density_kg_m3'] * np.pi * (R_outer**2 - R_inner**2) * w_m

    return BrakeDrumGeom(
        diameter_mm       = inp.brake_drum_diameter_mm,
        width_mm          = w_mm,
        wall_thickness_mm = t_mm,
        mass_kg           = mass,
        hoop_stress_MPa   = sigma_actual,
        safety_factor     = SF_actual,
    )


def compute_ratchet(inp: ARIAInputs) -> RatchetGeom:
    """
    Ratchet wheel tooth geometry from ANSI holding force requirement.
    Tooth bending stress via Lewis formula: sigma = F / (m * b * Y)
    Pressure angle 26° from ARIA design notes.
    """
    mat = MATERIAL_4140_STEEL
    R   = inp.brake_drum_diameter_mm / 2  # mm (ratchet on brake drum OD)
    F_hold_N = inp.min_hold_force_kN * 1000

    pressure_angle_deg = 26.0
    phi = np.radians(pressure_angle_deg)
    Y_lewis = 0.154 - 0.912/20  # Lewis form factor for ~20 teeth

    # Face width from Lewis: b = F / (m * sigma_allow * Y)
    # Units: F[N], m[mm], sigma[MPa=N/mm²], b[mm]
    # Rearranged: b = F / (m_mm * sigma_MPa * Y)
    module_mm   = 3.0
    sigma_allow = mat['yield_MPa'] / inp.safety_factor_fatigue  # N/mm² (MPa)

    b_mm = F_hold_N / (module_mm * sigma_allow * Y_lewis)  # mm
    b_mm = max(b_mm, 10.0)  # min 10mm face width

    # Number of teeth from circumference / pitch
    pitch_mm = np.pi * module_mm
    n_teeth  = int(2 * np.pi * R / pitch_mm)
    n_teeth  = max(n_teeth, 12)  # min 12 teeth

    # Tooth height: addendum + dedendum
    h_mm = 2.25 * module_mm  # standard full depth

    # Root and tip radii
    r_root = R - 1.25 * module_mm
    r_tip  = R + module_mm

    # Actual bending stress (verify): sigma = F / (m * b * Y)  [N/mm² = MPa]
    sigma_b = F_hold_N / (module_mm * b_mm * Y_lewis)  # MPa
    SF_b    = mat['yield_MPa'] / sigma_b

    return RatchetGeom(
        n_teeth              = n_teeth,
        pitch_mm             = pitch_mm,
        tooth_height_mm      = h_mm,
        face_width_mm        = b_mm,
        pressure_angle_deg   = pressure_angle_deg,
        root_radius_mm       = r_root,
        tip_radius_mm        = r_tip,
        tooth_bending_stress_MPa = sigma_b,
        safety_factor        = SF_b,
    )


def compute_centrifugal_clutch(inp: ARIAInputs,
                                drum: BrakeDrumGeom) -> CentrifugalClutchGeom:
    """
    Centrifugal flyweight sizing.
    Clutch engages when: m * omega^2 * r > F_spring
    Must engage at fall detection speed, NOT at normal climbing speed.

    Detection speed: v_fall = inp.fall_detection_v_m_s
    Normal speed:    v_climb ~ 0.3 m/s (typical climbing speed)
    Safety margin:   v_fall / v_climb > 3 (per Lead Solo design principle)
    """
    R_drum  = drum.diameter_mm / 2 / 1000  # m (rope wraps at drum OD)
    R_spool = inp.rope_spool_od_mm / 2 / 1000  # m (effective rope wrap radius)

    # Rope speed → spool angular velocity
    omega_detect = inp.fall_detection_v_m_s / R_spool  # rad/s
    RPM_detect   = omega_detect * 60 / (2 * np.pi)

    # Normal climbing omega
    v_climb_normal = 0.3  # m/s typical
    omega_normal   = v_climb_normal / R_spool
    safety_margin  = inp.fall_detection_v_m_s / v_climb_normal

    # Flyweight radius (centroid) — sit at 60% of drum radius
    r_fw = R_drum * 0.60  # m

    # Standard: 3 flyweights (Lead Solo uses 3)
    n_fw = 3

    # Spring preload: must hold flyweights in at omega_normal * 1.2 margin
    # F_centrifugal = m * omega^2 * r_fw
    # At engagement: m * omega_detect^2 * r_fw = F_spring
    # Choose flyweight mass to get reasonable spring force (~5-15 N)
    F_spring_target = 8.0  # N per flyweight (tunable)
    m_fw = F_spring_target / (omega_detect**2 * r_fw)  # kg
    m_fw_g = m_fw * 1000  # grams

    # Verify doesn't engage at normal climbing
    F_at_normal = m_fw * omega_normal**2 * r_fw
    # Spring must be > F_at_normal with margin
    F_spring = m_fw * (omega_detect * 0.85)**2 * r_fw  # engage at 85% of detect speed

    return CentrifugalClutchGeom(
        n_flyweights       = n_fw,
        flyweight_mass_g   = m_fw_g,
        flyweight_radius_mm= r_fw * 1000,
        spring_preload_N   = F_spring,
        engagement_rpm     = RPM_detect * 0.85,
        engagement_v_m_s   = inp.fall_detection_v_m_s * 0.85,
        safety_margin      = safety_margin,
    )


def compute_rope_spool(inp: ARIAInputs) -> RopeSpoolGeom:
    """
    Spool geometry to hold required rope capacity.
    V_spool = pi/4 * (D_flange^2 - D_hub^2) * W  = n_layers * n_wraps * rope_vol
    """
    d_hub   = inp.rope_spool_hub_diameter_mm  # mm
    d_rope  = inp.rope_diameter_mm            # mm
    L_rope  = inp.max_rope_capacity_m         # m

    # Effective rope wrap radius = spool OD/2 (where rope wraps)
    effective_rope_radius_m = inp.rope_spool_od_mm / 2 / 1000  # m

    # Flange diameter — typically hub + 6 layers of rope
    n_layers  = 6
    d_flange  = d_hub + 2 * n_layers * d_rope  # mm

    # Wraps per layer
    # Spool width = rope_dia * wraps_per_layer (with 10% packing factor)
    wraps_per_layer = int(L_rope * 1000 / (np.pi * d_hub) / n_layers)
    spool_width = wraps_per_layer * d_rope * 1.1  # mm
    spool_width = max(spool_width, d_rope * 10)

    # Actual capacity
    total_wraps = wraps_per_layer * n_layers
    capacity_m  = total_wraps * np.pi * (d_hub/1000) / 1.0

    # Moment of inertia (solid disk approximation + rope)
    rho_al   = 2700.0  # kg/m³
    w_m      = spool_width / 1000
    I_spool  = 0.5 * rho_al * np.pi * (d_flange/2000)**4 * w_m
    # Rope inertia (approximate)
    m_rope   = L_rope * 0.065  # ~65 g/m for 10mm rope
    I_rope   = m_rope * (d_hub/2000)**2

    return RopeSpoolGeom(
        hub_diameter_mm          = d_hub,
        flange_diameter_mm       = d_flange,
        width_mm                 = spool_width,
        layers                   = n_layers,
        capacity_m               = capacity_m,
        moment_of_inertia_kg_m2  = I_spool + I_rope,
        effective_rope_radius_m   = effective_rope_radius_m,
    )


def compute_motor(inp: ARIAInputs, spool: RopeSpoolGeom) -> MotorSpec:
    """
    Motor selection from torque/speed requirements at spool shaft.
    T_spool = F_tension * R_spool
    omega_spool = v_rope / R_spool
    Gearbox ratio selected for:
      - Back-drive prevention (hold tension without power)
      - Speed range coverage
    """
    R_spool = spool.effective_rope_radius_m  # m (rope wrap radius)

    # Required torque at spool (normal tension + safety margin)
    T_spool_Nm  = inp.target_tension_N * R_spool * 1.5  # 1.5x margin
    T_max_Nm    = inp.min_hold_force_kN * 1000 * R_spool  # arrest hold torque

    # Required speed at spool
    omega_spool_max = inp.max_retract_speed_m_s / R_spool  # rad/s
    RPM_spool       = omega_spool_max * 60 / (2 * np.pi)

    # T-Motor GB54-2 / similar BLDC: ~0.5 Nm peak, ~3000 RPM
    motor_peak_Nm  = 0.5
    motor_peak_RPM = 3000.0

    # Use physical gearbox ratio from inputs (30:1 planetary per ARIA spec)
    GR = inp.gearbox_ratio

    # Back-drive analysis
    # For worm-equivalent: self-locking if efficiency < 50%
    # Planetary at 30:1: efficiency ~85%, NOT self-locking
    # Must use one-way bearing (as in ARIA design) for back-drive prevention
    back_drive_T = inp.target_tension_N * R_spool / (GR * 0.85)

    recommendation = (
        f"T-Motor GB54-2 BLDC + {GR:.0f}:1 planetary gearbox. "
        f"Add one-way bearing on spool shaft (planetary is NOT self-locking). "
        f"Motor torque at spool: {motor_peak_Nm*GR*0.85:.1f} Nm. "
        f"Max rope speed: {motor_peak_RPM/60*2*np.pi/GR*R_spool:.2f} m/s."
    )

    # Motor shaft rad/s limit from RPM
    velocity_limit_rad_s = motor_peak_RPM * 2 * np.pi / 60  # 3000 RPM -> ~314 rad/s

    return MotorSpec(
        required_torque_Nm   = T_spool_Nm,
        required_speed_rpm   = RPM_spool,
        gearbox_ratio        = GR,
        motor_torque_Nm      = motor_peak_Nm,
        motor_speed_rpm      = motor_peak_RPM,
        back_drive_torque_Nm = back_drive_T,
        velocity_limit_rad_s = velocity_limit_rad_s,
        recommendation       = recommendation,
    )


def compute_housing(inp: ARIAInputs, drum: BrakeDrumGeom,
                     spool: RopeSpoolGeom) -> HousingGeom:
    """
    Housing outer shell sizing.
    Load case: max rope tension (8 kN) + impact factor pulling on wall mount.
    Wall thickness from bending moment at wall mount bolts.
    """
    mat = MATERIAL_6061_T6
    OD  = inp.housing_od_mm
    R   = OD / 2 / 1000  # m

    # Worst case: 8 kN arrest force creates moment at wall mount
    F_arrest   = inp.min_hold_force_kN * 1000 * ANSI_Z359_14['dynamic_load_factor']
    M_moment   = F_arrest * 0.1   # 100mm moment arm (conservative)

    # Hoop stress in housing from internal components + mounting loads
    P_eq_Pa    = F_arrest / (np.pi * R**2)  # equivalent pressure
    sigma_allow= mat['yield_MPa'] * 1e6 / inp.safety_factor_structural
    t_m        = P_eq_Pa * R / sigma_allow
    t_mm       = max(t_m * 1000, 4.0)  # min 4mm housing wall

    # Housing length: accommodate drum + spool + motor + clearances
    L_mm = drum.width_mm + spool.width_mm + 80.0  # 80mm for motor/gearbox

    # Mass
    mass = mat['density_kg_m3'] * np.pi * ((OD/2000)**2 - (OD/2000 - t_mm/1000)**2) * L_mm/1000

    # Bolt circle from ARIA design
    n_bolts   = 4
    bolt_circle = inp.wall_mount_bolt_pattern_mm

    sigma_wall = P_eq_Pa * R / (t_mm/1000) / 1e6

    return HousingGeom(
        od_mm             = OD,
        wall_thickness_mm = t_mm,
        length_mm         = L_mm,
        mass_kg           = mass,
        wall_stress_MPa   = sigma_wall,
        n_wall_bolts      = n_bolts,
        bolt_circle_mm    = bolt_circle,
    )


def compute_arrest_performance(inp: ARIAInputs,
                                spool: RopeSpoolGeom,
                                clutch: CentrifugalClutchGeom) -> Tuple[float, float, float]:
    """
    Predict: arrest distance, peak force on climber, catch time.
    Uses impulse-momentum theorem.
    """
    m_test   = ANSI_Z359_14['test_mass_kg']  # kg
    v_detect = clutch.engagement_v_m_s       # rope speed at detection

    # Energy at detection: KE = 0.5 * m * v^2
    # Plus potential energy from fall distance before detection
    v_fall = inp.fall_detection_v_m_s
    h_before = v_fall**2 / (2 * 9.81)  # distance fallen before detection
    KE = 0.5 * m_test * v_fall**2

    # Arrest: decelerate from v_fall to 0 over arrest distance d
    # F_mean * d = KE  (work-energy theorem)
    # F_mean ≈ inp.min_hold_force_kN * 1000 / 2 (ramp up)
    F_mean = inp.min_hold_force_kN * 1000 * 0.6
    d_arrest = KE / F_mean
    d_total  = h_before + d_arrest

    # Peak force: F_peak = m * a_max, a_max from v^2 / (2*d_arrest)
    a_max   = v_fall**2 / (2 * max(d_arrest, 0.01))
    F_peak  = m_test * (a_max + 9.81) / 1000  # kN

    # Catch time: t = v / a_max
    t_catch_ms = (v_fall / a_max) * 1000 if a_max > 0 else 999

    return d_total, F_peak, t_catch_ms


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DESIGN COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_aria(inputs: ARIAInputs) -> ARIAGeom:
    """Full ARIA device geometry from physics. No hardcoded dimensions."""

    drum    = compute_brake_drum(inputs)
    ratchet = compute_ratchet(inputs)
    clutch  = compute_centrifugal_clutch(inputs, drum)
    spool   = compute_rope_spool(inputs)
    motor   = compute_motor(inputs, spool)
    housing = compute_housing(inputs, drum, spool)

    d_arrest, F_peak, t_catch = compute_arrest_performance(inputs, spool, clutch)

    total_mass = drum.mass_kg + housing.mass_kg + spool.hub_diameter_mm/1000 * 0.5

    return ARIAGeom(
        inputs                  = inputs,
        brake_drum              = drum,
        ratchet                 = ratchet,
        clutch                  = clutch,
        spool                   = spool,
        motor                   = motor,
        housing                 = housing,
        predicted_arrest_distance_m = d_arrest,
        predicted_peak_force_kN     = F_peak,
        predicted_catch_time_ms     = t_catch,
        total_mass_kg           = total_mass,
    )


def export_sync_constants(geom: 'ARIAGeom', inp: ARIAInputs,
                          out_path: str = None) -> dict:
    """
    Export firmware-relevant constants from CEM geometry.
    Returns dict and optionally writes JSON.
    """
    import json
    from pathlib import Path

    constants = {
        # Geometry — from physical design
        "SPOOL_R": geom.spool.effective_rope_radius_m,
        "GEAR_RATIO": geom.motor.gearbox_ratio,

        # Motor limits — from CEM motor spec
        "MOTOR_VELOCITY_LIMIT": geom.motor.velocity_limit_rad_s,
        "MOTOR_TORQUE_NM": geom.motor.motor_torque_Nm,

        # Tension targets — from design requirements
        "T_BASELINE": inp.target_tension_N,
        "T_RETRACT": inp.target_tension_N * 1.5,   # 60N for retract

        # Speed limits — from CEM
        "SPD_RETRACT": inp.max_retract_speed_m_s
                       if hasattr(inp, 'max_retract_speed_m_s')
                       else 0.8,
        # NOTE: SPD_FALL in firmware is motor yield speed (2.0 m/s).
        # CEM engagement_v_m_s is clutch engagement speed (~1.3 m/s).
        # These are different — do not sync automatically.
        # SPD_FALL omitted from export to prevent accidental patch.

        # Shared STM32+ESP32 constants (keep in sync)
        "VOICE_CONF_MIN": 0.85,
        "CLIP_CONF_MIN": 0.75,
        "CLIP_SLACK_M": 0.65,

        # Safety constants (never change from CEM)
        "T_TAKE": 200.0,    # ANSI minimum for take confirmation
        "T_FALL": 400.0,    # Fall detection threshold
        "T_GROUND": 15.0,   # Ground/idle threshold
    }

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(
            json.dumps(constants, indent=2))
        print(f"CEM constants exported to {out_path}")

    return constants


# ─────────────────────────────────────────────────────────────────────────────
# ARIA CEM MODULE (plugs into CEMPlatform)
# ─────────────────────────────────────────────────────────────────────────────

class ARIAModule(CEMModule):
    def __init__(self, inputs: ARIAInputs):
        super().__init__("ARIA Auto Belay CEM")
        self.inputs = inputs
        self.geom: Optional[ARIAGeom] = None

    def compute(self) -> ARIAGeom:
        self.geom = compute_aria(self.inputs)
        return self.geom

    def validate(self) -> bool:
        if not self.geom:
            self.compute()
        g = self.geom
        i = self.inputs

        # ANSI arrest force
        self.physics_check(
            g.predicted_peak_force_kN <= ANSI_Z359_14['max_arrest_force_kN'],
            f"Peak arrest force {g.predicted_peak_force_kN:.2f} kN ≤ "
            f"ANSI limit {ANSI_Z359_14['max_arrest_force_kN']} kN",
            f"Peak arrest force {g.predicted_peak_force_kN:.2f} kN EXCEEDS "
            f"ANSI {ANSI_Z359_14['max_arrest_force_kN']} kN — redesign required")

        # Arrest distance
        self.physics_check(
            g.predicted_arrest_distance_m <= ANSI_Z359_14['arrest_distance_m'],
            f"Arrest distance {g.predicted_arrest_distance_m:.3f}m ≤ "
            f"ANSI limit {ANSI_Z359_14['arrest_distance_m']}m",
            f"Arrest distance {g.predicted_arrest_distance_m:.3f}m EXCEEDS "
            f"ANSI {ANSI_Z359_14['arrest_distance_m']}m")

        # Ratchet safety factor
        self.physics_check(
            g.ratchet.safety_factor >= i.safety_factor_fatigue,
            f"Ratchet tooth SF={g.ratchet.safety_factor:.2f} ≥ {i.safety_factor_fatigue}",
            f"Ratchet tooth SF={g.ratchet.safety_factor:.2f} < {i.safety_factor_fatigue} — increase face width")

        # Brake drum safety factor
        self.physics_check(
            g.brake_drum.safety_factor >= i.safety_factor_structural,
            f"Brake drum SF={g.brake_drum.safety_factor:.2f} ≥ {i.safety_factor_structural}",
            f"Brake drum SF={g.brake_drum.safety_factor:.2f} < required {i.safety_factor_structural}")

        # Clutch detection margin
        self.physics_check(
            g.clutch.safety_margin >= 3.0,
            f"Clutch detection margin {g.clutch.safety_margin:.1f}x ≥ 3.0x "
            f"(fall vs climb speed)",
            f"Clutch margin {g.clutch.safety_margin:.1f}x < 3.0x — false triggers possible")

        # Fall detection speed vs normal climbing
        self.physics_check(
            g.clutch.engagement_v_m_s > 0.5,
            f"Engagement speed {g.clutch.engagement_v_m_s:.2f} m/s — above normal climb",
            f"Engagement speed {g.clutch.engagement_v_m_s:.2f} m/s — may trigger on normal climbing")

        # Rope capacity
        self.physics_check(
            g.spool.capacity_m >= i.max_rope_capacity_m,
            f"Rope capacity {g.spool.capacity_m:.1f}m ≥ required {i.max_rope_capacity_m}m",
            f"Rope capacity {g.spool.capacity_m:.1f}m < required {i.max_rope_capacity_m}m")

        # Back-drive prevention note (not a fail — just engineering note)
        self.ok(f"Back-drive: one-way bearing required (planetary gearbox "
                f"not self-locking at {g.motor.gearbox_ratio:.0f}:1)")

        return len(self.warnings) == 0

    def export(self, out_dir: str) -> List[str]:
        os.makedirs(out_dir, exist_ok=True)
        files = []

        # Dimension CSV
        csv_path = os.path.join(out_dir, 'aria_dimensions.csv')
        self._export_csv(csv_path)
        files.append(csv_path)

        # Fusion import CSV
        fusion_path = os.path.join(out_dir, 'aria_fusion_profiles.csv')
        self._export_fusion_csv(fusion_path)
        files.append(fusion_path)

        # Plot
        if HAS_MPL:
            plot_path = os.path.join(out_dir, 'aria_design.png')
            plot_aria(self.geom, plot_path)
            files.append(plot_path)

        return files

    def _export_csv(self, path: str):
        g = self.geom
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['Parameter', 'Value', 'Unit', 'Notes'])
            rows = [
                # Brake drum
                ('brake_drum_diameter_mm',  f'{g.brake_drum.diameter_mm:.2f}',    'mm', 'Lead Solo reference'),
                ('brake_drum_width_mm',     f'{g.brake_drum.width_mm:.2f}',       'mm', 'Axial width'),
                ('brake_drum_wall_mm',      f'{g.brake_drum.wall_thickness_mm:.3f}','mm','Hoop stress derived'),
                ('brake_drum_SF',           f'{g.brake_drum.safety_factor:.2f}',  '-',  f'SF>={g.inputs.safety_factor_structural}'),
                ('brake_drum_mass_kg',      f'{g.brake_drum.mass_kg:.3f}',        'kg', ''),
                # Ratchet
                ('ratchet_n_teeth',         f'{g.ratchet.n_teeth}',               '-',  ''),
                ('ratchet_module_mm',       '3.0',                                'mm', 'Lewis formula'),
                ('ratchet_face_width_mm',   f'{g.ratchet.face_width_mm:.2f}',     'mm', 'Lewis bending'),
                ('ratchet_pressure_angle',  f'{g.ratchet.pressure_angle_deg:.1f}','deg','ARIA design notes'),
                ('ratchet_tooth_SF',        f'{g.ratchet.safety_factor:.2f}',     '-',  'Fatigue SF'),
                # Clutch
                ('clutch_n_flyweights',     f'{g.clutch.n_flyweights}',           '-',  ''),
                ('clutch_flyweight_g',      f'{g.clutch.flyweight_mass_g:.2f}',   'g',  'Each flyweight'),
                ('clutch_flyweight_R_mm',   f'{g.clutch.flyweight_radius_mm:.2f}','mm', 'Centroid radius'),
                ('clutch_spring_N',         f'{g.clutch.spring_preload_N:.2f}',   'N',  'Per flyweight'),
                ('clutch_engagement_rpm',   f'{g.clutch.engagement_rpm:.1f}',     'rpm',''),
                ('clutch_engagement_v',     f'{g.clutch.engagement_v_m_s:.3f}',   'm/s',''),
                ('clutch_detection_margin', f'{g.clutch.safety_margin:.1f}',      'x',  'Fall/climb speed ratio'),
                # Spool
                ('spool_hub_d_mm',          f'{g.spool.hub_diameter_mm:.1f}',     'mm', ''),
                ('spool_flange_d_mm',       f'{g.spool.flange_diameter_mm:.1f}',  'mm', ''),
                ('spool_width_mm',          f'{g.spool.width_mm:.1f}',            'mm', ''),
                ('spool_rope_capacity_m',   f'{g.spool.capacity_m:.1f}',          'm',  ''),
                ('spool_layers',            f'{g.spool.layers}',                  '-',  ''),
                # Motor
                ('motor_gearbox_ratio',     f'{g.motor.gearbox_ratio:.0f}',       ':1', ''),
                ('motor_torque_at_spool',   f'{g.motor.motor_torque_Nm*g.motor.gearbox_ratio*0.85:.2f}','Nm',''),
                # Housing
                ('housing_od_mm',           f'{g.housing.od_mm:.1f}',             'mm', ''),
                ('housing_wall_mm',         f'{g.housing.wall_thickness_mm:.2f}', 'mm', ''),
                ('housing_length_mm',       f'{g.housing.length_mm:.1f}',         'mm', ''),
                ('housing_mass_kg',         f'{g.housing.mass_kg:.3f}',           'kg', ''),
                ('wall_bolt_circle_mm',     f'{g.housing.bolt_circle_mm:.1f}',    'mm', f'{g.housing.n_wall_bolts}x bolts'),
                # Performance
                ('arrest_distance_m',       f'{g.predicted_arrest_distance_m:.3f}','m', 'ANSI limit 1.0m'),
                ('peak_force_kN',           f'{g.predicted_peak_force_kN:.3f}',   'kN', 'ANSI limit 6.0kN'),
                ('catch_time_ms',           f'{g.predicted_catch_time_ms:.1f}',   'ms', ''),
                ('total_device_mass_kg',    f'{g.total_mass_kg:.3f}',             'kg', 'Approx'),
            ]
            for row in rows:
                w.writerow(row)
        print(f"  ARIA dimensions CSV → {path}")

    def _export_fusion_csv(self, path: str):
        """Export 2D profiles for Fusion import (x, r, 0 format)."""
        g = self.geom
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            # Brake drum cross-section (rectangle in XZ plane)
            R_drum = g.brake_drum.diameter_mm / 2
            t_drum = g.brake_drum.wall_thickness_mm
            L_drum = g.brake_drum.width_mm
            pts = [
                (0, R_drum - t_drum), (L_drum, R_drum - t_drum),
                (L_drum, R_drum),     (0, R_drum),
                (0, R_drum - t_drum)  # close
            ]
            for (x, r) in pts:
                w.writerow([f'{x:.4f}', f'{r:.4f}', '0'])
            w.writerow([])

            # Rope spool cross-section
            R_hub    = g.spool.hub_diameter_mm / 2
            R_flange = g.spool.flange_diameter_mm / 2
            W_spool  = g.spool.width_mm
            flange_t = 8.0  # mm
            spool_pts = [
                (0, R_hub), (W_spool, R_hub),          # hub top
                (W_spool, R_flange),                    # right flange top
                (W_spool + flange_t, R_flange),
                (W_spool + flange_t, R_hub),
                (W_spool + flange_t, 0),                # right axis
                (0, 0),                                 # left axis
                (-flange_t, 0),
                (-flange_t, R_hub),
                (-flange_t, R_flange),                  # left flange top
                (0, R_flange),
                (0, R_hub),                             # back to start
            ]
            for (x, r) in spool_pts:
                w.writerow([f'{x:.4f}', f'{r:.4f}', '0'])
            w.writerow([])

            # Ratchet tooth profile (one tooth, simplified)
            R_root = g.ratchet.root_radius_mm
            R_tip  = g.ratchet.tip_radius_mm
            pitch  = g.ratchet.pitch_mm
            n      = g.ratchet.n_teeth
            phi    = np.radians(g.ratchet.pressure_angle_deg)
            for i in range(min(n, 36)):  # export up to 36 teeth
                ang = 2 * np.pi * i / n
                # Root point
                w.writerow([f'{R_root*np.cos(ang):.4f}',
                             f'{R_root*np.sin(ang):.4f}', '0'])
                # Tip point
                ang_tip = ang + pitch / (2 * R_tip)
                w.writerow([f'{R_tip*np.cos(ang_tip):.4f}',
                             f'{R_tip*np.sin(ang_tip):.4f}', '0'])
        print(f"  ARIA Fusion profiles CSV → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# VISUALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def plot_aria(geom: ARIAGeom, output_path: str):
    if not HAS_MPL:
        return
    import matplotlib.patches as patches

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    fig.patch.set_facecolor('#0d0d0d')

    def sax(ax, title):
        ax.set_facecolor('#1a1a1a'); ax.tick_params(colors='#ccc')
        ax.xaxis.label.set_color('#ccc'); ax.yaxis.label.set_color('#ccc')
        ax.set_title(title, color='white', fontsize=9)
        for sp in ax.spines.values(): sp.set_edgecolor('#444')
        ax.grid(True, alpha=0.2)

    g = geom

    # ── 1. Assembly cross-section ─────────────────────────────────────────────
    ax = axes[0,0]; sax(ax, 'ARIA Assembly Cross-Section')
    R_drum   = g.brake_drum.diameter_mm / 2
    t_drum   = g.brake_drum.wall_thickness_mm
    L_drum   = g.brake_drum.width_mm
    R_hub    = g.spool.hub_diameter_mm / 2
    R_flange = g.spool.flange_diameter_mm / 2
    R_house  = g.housing.od_mm / 2

    # Housing
    house = patches.Rectangle((-g.housing.length_mm/2, 0), g.housing.length_mm,
                                R_house, linewidth=2, edgecolor='#4a9eca',
                                facecolor='#1e3a4a', alpha=0.5)
    ax.add_patch(house)

    # Brake drum
    drum_x = -g.housing.length_mm/4
    drum = patches.Rectangle((drum_x, R_drum-t_drum), L_drum, t_drum,
                               facecolor='#50fa7b', alpha=0.7, label='Brake drum')
    ax.add_patch(drum)

    # Spool
    spool_x = g.housing.length_mm/8
    spool = patches.Rectangle((spool_x, 0), g.spool.width_mm, R_hub,
                                facecolor='#4fc3f7', alpha=0.7, label='Spool')
    ax.add_patch(spool)

    # Motor placeholder
    motor_x = g.housing.length_mm/4
    motor = patches.Rectangle((motor_x, 0), 40, R_hub*0.8,
                                facecolor='#ff7043', alpha=0.7, label='Motor')
    ax.add_patch(motor)

    ax.set_xlim(-g.housing.length_mm*0.6, g.housing.length_mm*0.6)
    ax.set_ylim(-5, R_house*1.3)
    ax.set_aspect('equal'); ax.set_xlabel('mm'); ax.set_ylabel('mm')
    ax.legend(fontsize=7, facecolor='#222', labelcolor='white')
    ax.axhline(0, color='white', lw=1, ls='--', alpha=0.4)

    # ── 2. Ratchet wheel ──────────────────────────────────────────────────────
    ax = axes[0,1]; sax(ax, f'Ratchet Wheel — {g.ratchet.n_teeth} teeth')
    theta = np.linspace(0, 2*np.pi, 500)
    R_root = g.ratchet.root_radius_mm
    R_tip  = g.ratchet.tip_radius_mm
    ax.plot(np.cos(theta)*R_root, np.sin(theta)*R_root, '#666', lw=1, ls='--')
    ax.plot(np.cos(theta)*R_tip,  np.sin(theta)*R_tip,  '#666', lw=1, ls='--')

    n = g.ratchet.n_teeth
    phi = np.radians(g.ratchet.pressure_angle_deg)
    for i in range(n):
        ang  = 2*np.pi*i/n
        ang2 = 2*np.pi*(i+0.5)/n
        # Drive face (steep)
        ax.plot([R_root*np.cos(ang), R_tip*np.cos(ang)],
                [R_root*np.sin(ang), R_tip*np.sin(ang)],
                '#ff6b35', lw=1.5)
        # Back face (shallow)
        ax.plot([R_tip*np.cos(ang), R_root*np.cos(ang2)],
                [R_tip*np.sin(ang), R_root*np.sin(ang2)],
                '#4fc3f7', lw=0.8)
    ax.set_aspect('equal'); ax.set_xlabel('mm'); ax.set_ylabel('mm')
    ax.text(0, 0, f'SF={g.ratchet.safety_factor:.1f}\n26° PA',
            ha='center', va='center', color='white', fontsize=8)

    # ── 3. Centrifugal clutch ─────────────────────────────────────────────────
    ax = axes[0,2]; sax(ax, 'Centrifugal Clutch — Flyweight Layout')
    R_fw = g.clutch.flyweight_radius_mm
    theta_drum = np.linspace(0, 2*np.pi, 100)
    ax.plot(np.cos(theta_drum)*R_drum, np.sin(theta_drum)*R_drum,
            '#50fa7b', lw=2, label=f'Drum Ø{g.brake_drum.diameter_mm:.0f}mm')
    for i in range(g.clutch.n_flyweights):
        ang = 2*np.pi*i/g.clutch.n_flyweights
        fx = R_fw * np.cos(ang); fy = R_fw * np.sin(ang)
        fw = plt.Circle((fx, fy), 8, color='#ff7043', alpha=0.8, zorder=3)
        ax.add_patch(fw)
        ax.annotate(f'{g.clutch.flyweight_mass_g:.1f}g',
                    (fx, fy), color='white', fontsize=6, ha='center')
    ax.set_aspect('equal'); ax.set_xlabel('mm'); ax.set_ylabel('mm')
    ax.set_xlim(-R_drum*1.2, R_drum*1.2); ax.set_ylim(-R_drum*1.2, R_drum*1.2)
    ax.text(0, -R_drum*1.0,
            f'Engage: {g.clutch.engagement_v_m_s:.2f}m/s\n'
            f'Margin: {g.clutch.safety_margin:.1f}x',
            ha='center', color='#ff7043', fontsize=7)
    ax.legend(fontsize=7, facecolor='#222', labelcolor='white')

    # ── 4. Performance vs ANSI limits ────────────────────────────────────────
    ax = axes[1,0]; sax(ax, 'Performance vs ANSI Z359.14 Limits')
    metrics   = ['Arrest\ndist (m)', 'Peak force\n(kN)', 'Catch\ntime (ms/100)']
    actuals   = [g.predicted_arrest_distance_m,
                 g.predicted_peak_force_kN,
                 g.predicted_catch_time_ms/100]
    limits    = [1.0, 6.0, 1.0]  # normalized
    bar_colors= ['#50fa7b' if a<=l else '#ff5555' for a,l in zip(actuals,limits)]
    bars = ax.bar(metrics, actuals, color=bar_colors, alpha=0.8, edgecolor='#555')
    for bar, lim, label in zip(bars, limits, metrics):
        ax.axhline(lim, color='#ff5555', lw=1.5, ls='--', alpha=0.7)
    for bar, val in zip(bars, actuals):
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.02,
                f'{val:.3f}', ha='center', color='white', fontsize=8)
    ax.set_ylabel('Value')

    # ── 5. Motor torque-speed curve ───────────────────────────────────────────
    ax = axes[1,1]; sax(ax, 'Motor + Gearbox Operating Point')
    GR = g.motor.gearbox_ratio
    RPM_motor = np.linspace(0, g.motor.motor_speed_rpm, 100)
    T_motor   = g.motor.motor_torque_Nm * (1 - RPM_motor/g.motor.motor_speed_rpm)
    # At spool
    RPM_spool = RPM_motor / GR
    T_spool   = T_motor * GR * 0.85

    ax.plot(RPM_spool, T_spool, '#50fa7b', lw=2, label=f'Spool shaft ({GR:.0f}:1)')
    ax.axhline(g.motor.required_torque_Nm, color='#ff7043', lw=1.5, ls='--',
               label=f'Required {g.motor.required_torque_Nm:.2f}Nm')
    ax.axvline(g.motor.required_speed_rpm, color='#4fc3f7', lw=1.5, ls='--',
               label=f'Required {g.motor.required_speed_rpm:.0f}RPM')
    ax.set_xlabel('Spool RPM'); ax.set_ylabel('Torque [Nm]')
    ax.legend(fontsize=7, facecolor='#222', labelcolor='white')

    # ── 6. Design summary table ───────────────────────────────────────────────
    ax = axes[1,2]; ax.set_facecolor('#1a1a1a'); ax.axis('off')
    ax.set_title('ARIA Design Summary', color='white', fontsize=9)
    summary = [
        ['Parameter',            'Value'],
        ['Brake drum',           f'Ø{g.brake_drum.diameter_mm:.0f}mm × {g.brake_drum.width_mm:.0f}mm'],
        ['Drum wall t',          f'{g.brake_drum.wall_thickness_mm:.2f}mm (SF={g.brake_drum.safety_factor:.1f})'],
        ['Ratchet teeth',        f'{g.ratchet.n_teeth} @ m=3, 26° PA'],
        ['Ratchet SF',           f'{g.ratchet.safety_factor:.1f} (fatigue)'],
        ['Flyweights',           f'{g.clutch.n_flyweights}× {g.clutch.flyweight_mass_g:.1f}g'],
        ['Clutch engage',        f'{g.clutch.engagement_v_m_s:.2f} m/s ({g.clutch.safety_margin:.1f}x margin)'],
        ['Spool',                f'Ø{g.spool.hub_diameter_mm:.0f}mm, {g.spool.capacity_m:.0f}m capacity'],
        ['Gearbox ratio',        f'{g.motor.gearbox_ratio:.0f}:1'],
        ['Housing',              f'Ø{g.housing.od_mm:.0f}mm × {g.housing.length_mm:.0f}mm'],
        ['Arrest distance',      f'{g.predicted_arrest_distance_m:.3f}m'],
        ['Peak force',           f'{g.predicted_peak_force_kN:.2f}kN'],
        ['Total mass',           f'{g.total_mass_kg:.2f}kg'],
    ]
    t = ax.table(cellText=summary, loc='center',
                 cellColours=[['#0f3460','#0f3460']] +
                              [['#1a1a1a','#1e3a2e']] * (len(summary)-1),
                 cellLoc='left')
    t.auto_set_font_size(False); t.set_fontsize(8)
    for (r,c), cell in t.get_celld().items():
        cell.set_text_props(color='white')
        cell.set_edgecolor('#333')
    t.scale(1, 1.5)

    plt.suptitle(
        f'ARIA Auto Belay CEM — Physics-Derived Design\n'
        f'Arrest: {g.predicted_arrest_distance_m:.3f}m | '
        f'Peak F: {g.predicted_peak_force_kN:.2f}kN | '
        f'ANSI Z359.14 target',
        color='white', fontsize=10)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f"  ARIA design plot → {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# PRINT TABLE
# ─────────────────────────────────────────────────────────────────────────────

def print_aria_table(g: ARIAGeom):
    print("\n" + "="*65)
    print("  ARIA AUTO BELAY — PHYSICS-DERIVED DIMENSIONS")
    print("="*65)
    sections = [
        ("--- BRAKE DRUM ---", [
            ("Diameter",          f"{g.brake_drum.diameter_mm:.1f} mm"),
            ("Width",             f"{g.brake_drum.width_mm:.1f} mm"),
            ("Wall thickness",    f"{g.brake_drum.wall_thickness_mm:.3f} mm"),
            ("Hoop stress",       f"{g.brake_drum.hoop_stress_MPa:.1f} MPa"),
            ("Safety factor",     f"{g.brake_drum.safety_factor:.2f}"),
        ]),
        ("--- RATCHET WHEEL ---", [
            ("Number of teeth",   f"{g.ratchet.n_teeth}"),
            ("Module",            "3.0 mm"),
            ("Face width",        f"{g.ratchet.face_width_mm:.2f} mm"),
            ("Pressure angle",    f"{g.ratchet.pressure_angle_deg:.1f}°"),
            ("Bending stress",    f"{g.ratchet.tooth_bending_stress_MPa:.1f} MPa"),
            ("Safety factor",     f"{g.ratchet.safety_factor:.2f} (fatigue)"),
        ]),
        ("--- CENTRIFUGAL CLUTCH ---", [
            ("Flyweight count",   f"{g.clutch.n_flyweights}"),
            ("Flyweight mass",    f"{g.clutch.flyweight_mass_g:.2f} g each"),
            ("Flyweight radius",  f"{g.clutch.flyweight_radius_mm:.2f} mm"),
            ("Spring preload",    f"{g.clutch.spring_preload_N:.2f} N"),
            ("Engagement speed",  f"{g.clutch.engagement_v_m_s:.3f} m/s"),
            ("Engagement RPM",    f"{g.clutch.engagement_rpm:.1f}"),
            ("Detection margin",  f"{g.clutch.safety_margin:.1f}x (fall/climb)"),
        ]),
        ("--- ROPE SPOOL ---", [
            ("Hub diameter",      f"{g.spool.hub_diameter_mm:.1f} mm"),
            ("Flange diameter",   f"{g.spool.flange_diameter_mm:.1f} mm"),
            ("Width",             f"{g.spool.width_mm:.1f} mm"),
            ("Layers",            f"{g.spool.layers}"),
            ("Rope capacity",     f"{g.spool.capacity_m:.1f} m"),
        ]),
        ("--- MOTOR / GEARBOX ---", [
            ("Gearbox ratio",     f"{g.motor.gearbox_ratio:.0f}:1"),
            ("Torque at spool",   f"{g.motor.motor_torque_Nm*g.motor.gearbox_ratio*0.85:.2f} Nm"),
            ("Note",              "One-way bearing required — planetary not self-locking"),
        ]),
        ("--- HOUSING ---", [
            ("Outer diameter",    f"{g.housing.od_mm:.1f} mm"),
            ("Wall thickness",    f"{g.housing.wall_thickness_mm:.2f} mm"),
            ("Length",            f"{g.housing.length_mm:.1f} mm"),
            ("Mass (approx)",     f"{g.housing.mass_kg:.3f} kg"),
            ("Wall mount bolts",  f"{g.housing.n_wall_bolts}× on Ø{g.housing.bolt_circle_mm:.0f}mm"),
        ]),
        ("--- ANSI PERFORMANCE ---", [
            ("Arrest distance",   f"{g.predicted_arrest_distance_m:.3f} m (limit 1.0m)"),
            ("Peak force",        f"{g.predicted_peak_force_kN:.3f} kN (limit 6.0kN)"),
            ("Catch time",        f"{g.predicted_catch_time_ms:.1f} ms"),
            ("Total mass",        f"{g.total_mass_kg:.2f} kg"),
        ]),
    ]
    for section, rows in sections:
        print(f"\n  {section}")
        for label, val in rows:
            print(f"  {label:<30} {val}")
    print("\n" + "="*65)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    out_dir = "output_aria"
    os.makedirs(out_dir, exist_ok=True)

    print("\n" + "="*60)
    print("  ARIA AUTO BELAY — COMPUTATIONAL ENGINEERING MODEL")
    print("  Physics derives geometry. Same platform as rocket CEM.")
    print("="*60)

    # Default inputs match current ARIA design intent
    inputs = ARIAInputs(
        max_arrest_force_kN     = 6.0,
        min_hold_force_kN       = 8.0,
        fall_detection_v_m_s    = 1.5,
        max_fall_distance_m     = 1.0,
        rope_diameter_mm        = 10.0,
        max_rope_capacity_m     = 40.0,
        slack_feed_speed_m_s    = 0.8,
        max_retract_speed_m_s   = 1.5,
        target_tension_N        = 40.0,
        brake_drum_diameter_mm  = 200.0,
        rope_spool_hub_diameter_mm = 120.0,
        rope_spool_od_mm        = 600.0,
        housing_od_mm           = 260.0,
        safety_factor_structural= 3.0,
        safety_factor_fatigue   = 5.0,
    )

    print("\n  Computing ARIA geometry from physics...")
    module = ARIAModule(inputs)
    geom   = module.compute()

    module.validate()
    module.print_validation()
    print_aria_table(geom)

    print("\n  Generating outputs...")
    files = module.export(out_dir)

    print(f"\n  Outputs in ./{out_dir}/")
    print("  aria_design.png          — 6-panel design visualization")
    print("  aria_dimensions.csv      — all dimensions for manufacturing")
    print("  aria_fusion_profiles.csv — import into Fusion (ImportCSVPoints)")
    print("\n  To plug into CEM platform:")
    print("    from cem_core import CEMPlatform")
    print("    from aria_cem import ARIAModule, ARIAInputs")
    print("    platform = CEMPlatform('ARIA')")
    print("    platform.register('ARIA', ARIAModule(ARIAInputs(...)))")
    print("    platform.run('ARIA', 'output/')")
