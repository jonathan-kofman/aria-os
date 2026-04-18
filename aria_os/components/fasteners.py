"""Fasteners — ISO/metric bolts, nuts, washers."""
from __future__ import annotations

from functools import partial

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_socket_head_bolt, generate_hex_nut, generate_flat_washer


# ---------------------------------------------------------------------------
# Socket head cap screws — ISO 4762
# Dims from ISO 4762 tables. grade 12.9 steel, black oxide.
# (thread_dia, head_dia, head_height, socket_dia, socket_depth)
# ---------------------------------------------------------------------------
_SHCS_DIMS: dict[str, tuple[float, float, float, float, float]] = {
    "M3":  (3.0,  5.5,  3.0,  2.5, 1.3),
    "M4":  (4.0,  7.0,  4.0,  3.0, 2.0),
    "M5":  (5.0,  8.5,  5.0,  4.0, 2.5),
    "M6":  (6.0, 10.0,  6.0,  5.0, 3.0),
    "M8":  (8.0, 13.0,  8.0,  6.0, 4.0),
    "M10": (10.0, 16.0, 10.0,  8.0, 5.0),
    "M12": (12.0, 18.0, 12.0, 10.0, 6.0),
}

# Common lengths per size (mm)
_SHCS_LENGTHS: dict[str, list[int]] = {
    "M3":  [8, 10, 12, 16, 20],
    "M4":  [10, 12, 16, 20, 25],
    "M5":  [10, 12, 16, 20, 25, 30],
    "M6":  [12, 16, 20, 25, 30, 40],
    "M8":  [16, 20, 25, 30, 40, 50],
    "M10": [20, 25, 30, 40, 50, 60],
    "M12": [25, 30, 40, 50, 60, 80],
}

# Rough mass (grams) and cost (USD) per size at common length
_SHCS_REFERENCE_MASS = {  # at the 20mm length
    "M3": 1.1, "M4": 2.0, "M5": 3.3, "M6": 5.5, "M8": 12.0, "M10": 22.0, "M12": 38.0,
}
_SHCS_COST_USD = {
    "M3": 0.15, "M4": 0.20, "M5": 0.25, "M6": 0.30, "M8": 0.45, "M10": 0.75, "M12": 1.10,
}


def _register_shcs() -> None:
    for size, (td, hd, hh, sd, sdepth) in _SHCS_DIMS.items():
        for length in _SHCS_LENGTHS[size]:
            designation = f"{size}x{length}_12.9"
            mass = _SHCS_REFERENCE_MASS[size] * (length / 20.0)
            spec = ComponentSpec(
                designation=designation,
                category="fastener",
                subcategory="bolt",
                description=f"ISO 4762 socket head cap screw, {size}, {length}mm, grade 12.9",
                generate_fn=partial(
                    generate_socket_head_bolt,
                    thread_dia_mm=td, length_mm=length,
                    head_dia_mm=hd, head_height_mm=hh,
                    socket_dia_mm=sd, socket_depth_mm=sdepth,
                    output_path=None,
                ).__class__(
                    generate_socket_head_bolt,
                    thread_dia_mm=td, length_mm=length,
                    head_dia_mm=hd, head_height_mm=hh,
                    socket_dia_mm=sd, socket_depth_mm=sdepth,
                ) if False else (
                    # Wrap so generate_fn takes (output_path) as its only positional arg
                    lambda output_path, _td=td, _l=length, _hd=hd, _hh=hh, _sd=sd, _sdepth=sdepth:
                        generate_socket_head_bolt(
                            thread_dia_mm=_td, length_mm=_l,
                            head_dia_mm=_hd, head_height_mm=_hh,
                            socket_dia_mm=_sd, socket_depth_mm=_sdepth,
                            output_path=output_path,
                        )
                ),
                purchased=True,
                material="steel_12.9",
                mass_g=mass,
                cost_usd=_SHCS_COST_USD[size],
                supplier="McMaster-Carr",
                mating_features=[
                    MatingFeature("shaft_axis", "axis",
                                  {"origin": [0, 0, 0], "direction": [0, 0, -1]}),
                    MatingFeature("head_top", "face",
                                  {"origin": [0, 0, hh], "normal": [0, 0, 1]}),
                    MatingFeature("head_under", "face",
                                  {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
                    MatingFeature("shaft_tip", "point",
                                  {"origin": [0, 0, -length]}),
                ],
                dimensions={
                    "thread_dia_mm": td, "length_mm": length,
                    "head_dia_mm": hd, "head_height_mm": hh,
                },
            )
            register_component(spec)


# ---------------------------------------------------------------------------
# Hex nuts — ISO 4032
# (thread_dia, across_flats, thickness)
# ---------------------------------------------------------------------------
_HEX_NUT_DIMS: dict[str, tuple[float, float, float]] = {
    "M3":  (3.0,  5.5, 2.4),
    "M4":  (4.0,  7.0, 3.2),
    "M5":  (5.0,  8.0, 4.0),
    "M6":  (6.0, 10.0, 5.0),
    "M8":  (8.0, 13.0, 6.5),
    "M10": (10.0, 17.0, 8.0),
    "M12": (12.0, 19.0, 10.0),
}

_HEX_NUT_MASS = {"M3": 0.4, "M4": 0.8, "M5": 1.2, "M6": 2.3, "M8": 5.0, "M10": 10.0, "M12": 15.0}
_HEX_NUT_COST = {"M3": 0.05, "M4": 0.07, "M5": 0.08, "M6": 0.10, "M8": 0.15, "M10": 0.25, "M12": 0.40}


def _register_hex_nuts() -> None:
    for size, (td, af, t) in _HEX_NUT_DIMS.items():
        designation = f"{size}_hex_nut_8"
        spec = ComponentSpec(
            designation=designation,
            category="fastener",
            subcategory="nut",
            description=f"ISO 4032 hex nut, {size}, grade 8",
            generate_fn=(lambda output_path, _td=td, _af=af, _t=t:
                         generate_hex_nut(thread_dia_mm=_td, across_flats_mm=_af,
                                          thickness_mm=_t, output_path=output_path)),
            purchased=True,
            material="steel_8",
            mass_g=_HEX_NUT_MASS[size],
            cost_usd=_HEX_NUT_COST[size],
            supplier="McMaster-Carr",
            mating_features=[
                MatingFeature("thread_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("top_face", "face",
                              {"origin": [0, 0, t], "normal": [0, 0, 1]}),
                MatingFeature("bottom_face", "face",
                              {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
            ],
            dimensions={"thread_dia_mm": td, "across_flats_mm": af, "thickness_mm": t},
        )
        register_component(spec)


# ---------------------------------------------------------------------------
# Flat washers — ISO 7089
# (bore, od, thickness)
# ---------------------------------------------------------------------------
_WASHER_DIMS: dict[str, tuple[float, float, float]] = {
    "M3":  (3.2,  7.0,  0.5),
    "M4":  (4.3,  9.0,  0.8),
    "M5":  (5.3, 10.0,  1.0),
    "M6":  (6.4, 12.0,  1.6),
    "M8":  (8.4, 16.0,  1.6),
    "M10": (10.5, 20.0, 2.0),
    "M12": (13.0, 24.0, 2.5),
}


def _register_washers() -> None:
    for size, (bore, od, t) in _WASHER_DIMS.items():
        designation = f"{size}_flat_washer"
        spec = ComponentSpec(
            designation=designation,
            category="fastener",
            subcategory="washer",
            description=f"ISO 7089 flat washer, {size}, 200HV steel",
            generate_fn=(lambda output_path, _bore=bore, _od=od, _t=t:
                         generate_flat_washer(bore_dia_mm=_bore, od_mm=_od,
                                              thickness_mm=_t, output_path=output_path)),
            purchased=True,
            material="steel_200HV",
            mass_g=(od * od - bore * bore) * t * 0.006,  # rough mass estimate
            cost_usd=0.03,
            supplier="McMaster-Carr",
            mating_features=[
                MatingFeature("bore_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("top_face", "face",
                              {"origin": [0, 0, t], "normal": [0, 0, 1]}),
                MatingFeature("bottom_face", "face",
                              {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
            ],
            dimensions={"bore_mm": bore, "od_mm": od, "thickness_mm": t},
        )
        register_component(spec)


# Register at import time
_register_shcs()
_register_hex_nuts()
_register_washers()
