"""Drone-specific electronics + accessories — ESCs, LiPo batteries,
standoffs, straps. Adds the parts a drone build pulls into its BOM that
weren't covered by bldc_motors.py / propellers.py.

Module is auto-loaded via aria_os.components.__init__; entries are
registered with the global catalog at import time.
"""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component


# ---------------------------------------------------------------------------
# ESCs (Electronic Speed Controllers)
# Format: (current_a, voltage_max_v, protocol, mass_g, cost_usd, length, width, height)
# ---------------------------------------------------------------------------
_ESCS = {
    "ESC_30A_BLHeli32_4in1": (30, 25.2, "BLHeli32 / DShot1200", 8.5, 35,
                                30, 30, 7.0),
    "ESC_45A_BLHeli32_4in1": (45, 25.2, "BLHeli32 / DShot1200", 14.0, 50,
                                36, 36, 8.0),
    "ESC_60A_AM32_4in1":     (60, 50.4, "AM32 / DShot1200",     22.0, 75,
                                40, 40, 9.0),
    "ESC_30A_BLHeli32":      (30, 25.2, "BLHeli32 / DShot600",  3.5, 12,
                                17, 12, 4.5),  # single
}


def _register_escs() -> None:
    for designation, d in _ESCS.items():
        current, vmax, proto, mass, cost, l_, w_, h_ = d
        spec = ComponentSpec(
            designation=designation,
            category="electronics",
            subcategory="esc",
            description=(f"{current}A {vmax}V ESC ({proto}) — drone "
                          f"motor controller"),
            mass_g=float(mass),
            cost_usd=float(cost),
            material="PCB+passives",
            generate_fn=None,
            mating_features=[],
            dimensions={"current_a": current, "voltage_max_v": vmax,
                         "length_mm": l_, "width_mm": w_, "height_mm": h_},
        )
        register_component(spec)


# ---------------------------------------------------------------------------
# LiPo batteries
# Format: (cells, capacity_mah, c_rating, mass_g, cost_usd, length, width, height)
# ---------------------------------------------------------------------------
_LIPOS = {
    "LiPo_3S_1300mAh_75C":   (3, 1300, 75,  110, 18, 73, 35, 22),
    "LiPo_4S_1300mAh_120C":  (4, 1300, 120, 165, 26, 73, 35, 30),
    "LiPo_4S_1500mAh_120C":  (4, 1500, 120, 185, 30, 75, 36, 32),
    "LiPo_4S_1800mAh_100C":  (4, 1800, 100, 215, 35, 80, 38, 34),
    "LiPo_6S_1100mAh_120C":  (6, 1100, 120, 195, 40, 80, 38, 32),
    "LiPo_6S_1500mAh_120C":  (6, 1500, 120, 270, 50, 90, 42, 38),
    "LiPo_4S_3300mAh_60C":   (4, 3300, 60,  330, 55, 105, 45, 32),  # cinelift
    "LiPo_6S_5000mAh_50C":   (6, 5000, 50,  710, 90, 150, 50, 45),  # heavy lift
}


def _register_lipos() -> None:
    for designation, d in _LIPOS.items():
        cells, cap, c, mass, cost, l_, w_, h_ = d
        spec = ComponentSpec(
            designation=designation,
            category="power",
            subcategory="lipo_battery",
            description=(f"{cells}S {cap}mAh {c}C LiPo — drone propulsion "
                          f"battery, XT60 connector"),
            mass_g=float(mass),
            cost_usd=float(cost),
            material="LiPo (Li-Polymer)",
            dimensions={"cells": cells, "capacity_mah": cap, "c_rating": c,
                          "voltage_nominal_v": cells * 3.7,
                          "voltage_max_v":     cells * 4.2,
                          "length_mm": l_, "width_mm": w_, "height_mm": h_},
        )
        register_component(spec)


# ---------------------------------------------------------------------------
# Standoffs + straps (drone assembly hardware not covered by fasteners.py)
# ---------------------------------------------------------------------------
_DRONE_HARDWARE = [
    # designation, description, mass_g, cost_usd, bbox
    ("M3x6_brass_standoff",   "M3 brass standoff 6mm — FC stack",
     0.6, 0.30, (5, 5, 6)),
    ("M3x10_brass_standoff",  "M3 brass standoff 10mm — FC stack",
     0.9, 0.35, (5, 5, 10)),
    ("M3x20_brass_standoff",  "M3 brass standoff 20mm — FC stack",
     1.7, 0.45, (5, 5, 20)),
    ("Velcro_strap_200x20mm", "Battery strap, hook+loop, 200×20mm",
     4.0, 1.50, (200, 20, 1)),
    ("Velcro_strap_300x25mm", "Battery strap, hook+loop, 300×25mm — "
                                 "heavy lift",
     8.0, 2.00, (300, 25, 1)),
    ("XT60_connector",       "XT60 power connector pair (M+F)",
     5.0, 1.20, (16, 10, 8)),
    ("RC_receiver_ELRS_2.4G", "ExpressLRS 2.4GHz Rx — drone control link",
     1.5, 18.00, (10, 14, 4)),
    ("VTX_5.8GHz_400mW",     "5.8GHz video transmitter, 400mW MMCX",
     5.0, 28.00, (20, 20, 5)),
    ("FPV_camera_micro",     "1000TVL micro FPV camera, 19×19mm mount",
     5.5, 22.00, (19, 19, 22)),
    ("GPS_M8N_module",       "u-blox M8N GPS + compass module",
     16.0, 28.00, (32, 32, 8)),
    ("Telemetry_LoRa_433",   "433MHz LoRa SX1276 telemetry module",
     8.0, 35.00, (28, 14, 5)),
]


def _register_drone_hardware() -> None:
    for designation, desc, mass, cost, bbox in _DRONE_HARDWARE:
        cat = "electronics" if any(k in designation for k in
                                      ("RC_", "VTX", "FPV_camera",
                                       "GPS_", "Telemetry_")) \
              else "hardware"
        spec = ComponentSpec(
            designation=designation,
            category=cat,
            subcategory=("rf"      if "VTX" in designation or
                                         "RC_" in designation
                          else "sensor" if "GPS_" in designation or
                                              "FPV_camera" in designation or
                                              "Telemetry_" in designation
                          else "connector" if "XT60" in designation
                          else "standoff" if "standoff" in designation
                          else "strap"    if "strap" in designation
                          else "misc"),
            description=desc,
            mass_g=float(mass),
            cost_usd=float(cost),
            dimensions={"length_mm": float(bbox[0]),
                          "width_mm":  float(bbox[1]),
                          "height_mm": float(bbox[2])},
        )
        register_component(spec)


# Auto-register at import
_register_escs()
_register_lipos()
_register_drone_hardware()
