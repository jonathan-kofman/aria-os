"""
Propellers — fixed-pitch multicopter, pusher, tractor.

Mated to BLDC motor shafts via through-hole or M-style threaded hub.
Sized by diameter × pitch notation (e.g. 5x4.3 = 5" dia, 4.3" pitch).
"""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_propeller


# ---------------------------------------------------------------------------
# Propeller sizes — (diameter_mm, pitch_mm, n_blades, hub_dia, hub_height,
#                    shaft_bore, mass_g, cost_usd)
# ---------------------------------------------------------------------------
# Diameter in inches (then converted): 5" = 127mm, 7" = 178mm, 10" = 254mm, etc.
_PROPELLERS = {
    # Micro quads (3")
    "3x3_2blade":   (76,  76,  2, 10, 4.0, 5.0,  2.5, 2.0),
    # 5" quads (most common racing/freestyle)
    "5x4.3_2blade": (127, 109, 2, 12, 5.0, 5.0,  5.5, 3.5),
    "5x4.3_3blade": (127, 109, 3, 12, 5.0, 5.0,  7.0, 4.0),
    "5.1x4.5_3blade": (130, 114, 3, 12, 5.0, 5.0, 7.5, 4.5),
    # 7" (freestyle / cinema)
    "7x4_2blade":   (178, 102, 2, 14, 6.0, 6.0, 12.0, 5.0),
    "7x4.3_3blade": (178, 109, 3, 14, 6.0, 6.0, 15.0, 6.0),
    # 10" (larger freestyle, heavy lift racers)
    "10x5_2blade":  (254, 127, 2, 18, 8.0, 6.0, 22.0, 8.0),
    "10x5.5_3blade": (254, 140, 3, 18, 8.0, 6.0, 28.0, 10.0),
    # 13" (cinelifter)
    "13x4.4_2blade": (330, 112, 2, 22, 10.0, 8.0, 40.0, 15.0),
    "13x5.5_3blade": (330, 140, 3, 22, 10.0, 8.0, 55.0, 20.0),
    # 15" (heavy lift, eVTOL)
    "15x5_2blade":   (381, 127, 2, 25, 12.0, 8.0, 70.0, 25.0),
    # 18" (large UAV)
    "18x6.1_2blade": (457, 155, 2, 28, 14.0, 10.0, 110.0, 35.0),
    # 24" (heavy eVTOL / cargo)
    "24x7.2_2blade": (610, 183, 2, 35, 18.0, 12.0, 220.0, 65.0),
}


def _register_propellers() -> None:
    for designation, d in _PROPELLERS.items():
        (dia_mm, pitch_mm, n_blades, hub_d, hub_h, shaft_d, mass, cost) = d

        spec = ComponentSpec(
            designation=designation,
            category="propulsion",
            subcategory="propeller",
            description=f"Fixed-pitch {n_blades}-blade propeller, "
                       f"{dia_mm/25.4:.1f}\" x {pitch_mm/25.4:.1f}\" pitch",
            generate_fn=(lambda output_path, _d=dia_mm, _p=pitch_mm,
                                  _hd=hub_d, _hh=hub_h, _sd=shaft_d, _nb=n_blades:
                         generate_propeller(
                             diameter_mm=_d, pitch_mm=_p,
                             hub_dia_mm=_hd, hub_height_mm=_hh,
                             shaft_dia_mm=_sd, n_blades=_nb,
                             output_path=output_path)),
            purchased=True,
            material="carbon_fiber_or_glass_nylon",
            mass_g=float(mass),
            cost_usd=float(cost),
            max_rpm=30000 if dia_mm < 150 else (12000 if dia_mm < 300 else 6000),
            # Commercial hobby props are EAR99. Larger aerospace/UAV props
            # (typically >24" or specialty military) may be EAR-controlled.
            export_control="EAR99" if dia_mm < 600 else "EAR-9A991",
            supplier="HQProp / Gemfan / T-Motor / APC",
            mating_features=[
                MatingFeature("hub_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("shaft_bore", "hole",
                              {"origin": [0, 0, 0], "axis": [0, 0, 1],
                               "diameter_mm": float(shaft_d)}),
                MatingFeature("disk_plane", "face",
                              {"origin": [0, 0, hub_h / 2], "normal": [0, 0, 1]}),
                MatingFeature("tip_radius", "point",
                              {"origin": [float(dia_mm / 2), 0, hub_h / 2]}),
            ],
            dimensions={"diameter_mm": dia_mm, "pitch_mm": pitch_mm,
                        "n_blades": n_blades, "hub_dia_mm": hub_d,
                        "shaft_bore_mm": shaft_d},
        )
        register_component(spec)


_register_propellers()
