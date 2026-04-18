"""Miscellaneous hardware — dowel pins, retaining rings."""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_dowel_pin, generate_retaining_ring


# Dowel pins — ISO 8734 hardened, ground
# (diameter, length, mass, cost)
_DOWELS = [
    (3, 10, 0.6, 0.10), (3, 16, 1.0, 0.12), (3, 20, 1.2, 0.14),
    (4, 16, 1.6, 0.14), (4, 20, 2.0, 0.16), (4, 25, 2.5, 0.18),
    (5, 20, 3.1, 0.16), (5, 25, 3.9, 0.20), (5, 30, 4.7, 0.22),
    (6, 25, 5.6, 0.22), (6, 30, 6.7, 0.25), (6, 40, 8.9, 0.30),
    (8, 30, 11.9, 0.30), (8, 40, 15.9, 0.38), (8, 50, 19.9, 0.45),
    (10, 40, 24.9, 0.42), (10, 50, 31.2, 0.55),
]


def _register_dowels() -> None:
    for dia, length, mass, cost in _DOWELS:
        designation = f"dowel_{dia}mmx{length}mm"
        spec = ComponentSpec(
            designation=designation,
            category="hardware",
            subcategory="dowel_pin",
            description=f"ISO 8734 hardened dowel pin, {dia}mm x {length}mm",
            generate_fn=(lambda output_path, _d=dia, _l=length:
                         generate_dowel_pin(diameter_mm=_d, length_mm=_l,
                                             output_path=output_path)),
            purchased=True,
            material="hardened_steel",
            mass_g=mass,
            cost_usd=cost,
            supplier="McMaster-Carr",
            mating_features=[
                MatingFeature("axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("end_a", "point",
                              {"origin": [0, 0, 0]}),
                MatingFeature("end_b", "point",
                              {"origin": [0, 0, length]}),
            ],
            dimensions={"diameter_mm": dia, "length_mm": length},
        )
        register_component(spec)


# External retaining rings (E-clip style) — ISO 5299
_RINGS = [
    (3, 0.4, 0.05), (4, 0.4, 0.05), (5, 0.6, 0.06),
    (6, 0.7, 0.08), (8, 0.8, 0.10), (10, 1.0, 0.12),
    (12, 1.0, 0.14),
]


def _register_rings() -> None:
    for shaft_d, thickness, cost in _RINGS:
        designation = f"retaining_ring_ext_{shaft_d}mm"
        spec = ComponentSpec(
            designation=designation,
            category="hardware",
            subcategory="retaining_ring",
            description=f"External retaining ring for {shaft_d}mm shaft, {thickness}mm thick",
            generate_fn=(lambda output_path, _sd=shaft_d, _t=thickness:
                         generate_retaining_ring(shaft_dia_mm=_sd, thickness_mm=_t,
                                                  output_path=output_path)),
            purchased=True,
            material="spring_steel",
            mass_g=0.5,
            cost_usd=cost,
            supplier="McMaster-Carr",
            mating_features=[
                MatingFeature("shaft_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("shaft_bore", "hole",
                              {"origin": [0, 0, 0], "axis": [0, 0, 1],
                               "diameter_mm": shaft_d}),
            ],
            dimensions={"shaft_dia_mm": shaft_d, "thickness_mm": thickness},
        )
        register_component(spec)


_register_dowels()
_register_rings()
