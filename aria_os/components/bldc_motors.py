"""
BLDC motors + propellers — drone + small robot propulsion.

Covers common outrunner BLDC sizes by KV rating and frame class:
  - Multicopter (22xx–28xx frame, ~1000–4000 KV)
  - Cinelifter (35xx–40xx, 400–900 KV)
  - Fixed-wing / eVTOL (42xx–62xx, 200–600 KV)
"""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_bldc_outrunner, generate_propeller


# ---------------------------------------------------------------------------
# BLDC outrunner motors
# (designation -> stator_od, can_od, height, shaft_d, shaft_l,
#                 mount_pcd, mount_bolt_d, n_bolts, kv_rating, peak_w,
#                 mass_g, cost_usd)
# ---------------------------------------------------------------------------
_BLDC_MOTORS = {
    # 2204 — small racing quad
    "2207-1750KV":  (22, 28,  7.0, 5.0,  10, 16, 3.0, 4, 1750, 600,  30,  28),
    "2306-2450KV":  (23, 28.5, 6.0, 5.0, 9,  16, 3.0, 4, 2450, 700,  32,  30),
    # 2306 — 5" racing
    "2306-1800KV":  (23, 28.5, 6.0, 5.0, 9,  16, 3.0, 4, 1800, 750,  32,  30),
    # 2812 — 7-10" freestyle/cinematic
    "2812-900KV":   (28, 34.5, 12.0, 6.0, 14, 19, 3.0, 4, 900, 1400, 105, 58),
    "2812-1450KV":  (28, 34.5, 12.0, 6.0, 14, 19, 3.0, 4, 1450, 1400, 105, 55),
    # 35xx — cinelifter / 10" props
    "3510-580KV":   (35, 42,  10, 8.0, 16, 25, 4.0, 4, 580, 1800, 160, 75),
    "3510-700KV":   (35, 42,  10, 8.0, 16, 25, 4.0, 4, 700, 1800, 160, 75),
    # 5010 — heavy lift / 13-15" props
    "5010-340KV":   (50, 58,  10, 10.0, 18, 30, 4.0, 6, 340, 2400, 260, 110),
    "5010-620KV":   (50, 58,  10, 10.0, 18, 30, 4.0, 6, 620, 2400, 260, 110),
    # 6215 — eVTOL / large drone
    "6215-250KV":   (62, 70,  15, 12.0, 22, 40, 5.0, 6, 250, 4500, 540, 220),
}


def _register_bldc_motors() -> None:
    for designation, d in _BLDC_MOTORS.items():
        (stator_od, can_od, height, shaft_d, shaft_l,
         mount_pcd, mount_bolt_d, n_bolts, kv, peak_w, mass, cost) = d

        # Rough max torque from peak watts + KV
        # Torque_Nm ≈ peak_W / (RPM_no_load_nominal * 2pi / 60) — approximate
        # Using KV × 7V nominal (3S) as reference RPM
        nominal_rpm = kv * 14  # 4S battery ~14V
        max_rpm = nominal_rpm * 1.1
        max_torque = peak_w / (nominal_rpm * 0.1047) if nominal_rpm else None

        spec = ComponentSpec(
            designation=designation,
            category="motor",
            subcategory="bldc_outrunner",
            description=f"BLDC outrunner {designation}, {kv}KV, "
                       f"{peak_w}W peak, {mass}g",
            generate_fn=(lambda output_path, _so=stator_od, _co=can_od, _h=height,
                                  _sd=shaft_d, _sl=shaft_l,
                                  _mp=mount_pcd, _mbd=mount_bolt_d, _nb=n_bolts:
                         generate_bldc_outrunner(
                             stator_od_mm=_so, can_od_mm=_co, height_mm=_h,
                             shaft_dia_mm=_sd, shaft_length_mm=_sl,
                             mount_pcd_mm=_mp, mount_bolt_dia_mm=_mbd,
                             n_mount_bolts=_nb,
                             output_path=output_path)),
            purchased=True,
            material="neodymium_magnets_aluminum_can_copper_windings",
            mass_g=float(mass),
            cost_usd=float(cost),
            max_rpm=float(max_rpm),
            max_torque_nm=max_torque,
            # Commercial drone motors are typically EAR99 unless ITAR-classified
            # (e.g. specific military systems). Default EAR99; user can override.
            export_control="EAR99",
            supplier="T-Motor / iFlight / Sunnysky",
            mating_features=[
                MatingFeature("shaft_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("shaft_tip", "point",
                              {"origin": [0, 0, height + shaft_l]}),
                MatingFeature("mount_face", "face",
                              {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
                MatingFeature("mount_bolts", "bolt_circle",
                              {"center": [0, 0, 0], "axis": [0, 0, 1],
                               "pcd_mm": float(mount_pcd),
                               "n_bolts": n_bolts,
                               "bolt_dia_mm": float(mount_bolt_d)}),
            ],
            dimensions={"stator_od_mm": stator_od, "can_od_mm": can_od,
                        "height_mm": height, "shaft_dia_mm": shaft_d,
                        "shaft_length_mm": shaft_l, "kv": kv,
                        "peak_watts": peak_w,
                        "mount_pcd_mm": mount_pcd,
                        "mount_bolt_dia_mm": mount_bolt_d},
        )
        register_component(spec)


_register_bldc_motors()
