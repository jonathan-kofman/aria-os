"""Shaft couplings — rigid and flexible."""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_rigid_coupling, generate_flexible_coupling


# designation -> (bore_a, bore_b, od, length, mass_g, cost_usd)
_RIGID_COUPLINGS = {
    "rigid_5mm_5mm_D18L25":   (5.0,  5.0,  18.0, 25.0,  30, 12.0),
    "rigid_6.35mm_8mm_D19L25": (6.35, 8.0, 19.0, 25.0,  35, 14.0),
    "rigid_8mm_8mm_D25L30":   (8.0,  8.0,  25.0, 30.0,  60, 18.0),
    "rigid_8mm_10mm_D25L30":  (8.0, 10.0,  25.0, 30.0,  60, 20.0),
    "rigid_12.7mm_14mm_D32L35": (12.7, 14.0, 32.0, 35.0, 110, 28.0),
}

_FLEX_COUPLINGS = {
    "flex_beam_5mm_5mm_D19L25":  (5.0,  5.0,  19.0, 25.0,  20, 18.0),
    "flex_beam_6.35mm_8mm_D19L25": (6.35, 8.0, 19.0, 25.0, 22, 22.0),
    "flex_beam_8mm_8mm_D25L30":  (8.0,  8.0,  25.0, 30.0,  40, 28.0),
    "flex_beam_8mm_10mm_D25L30": (8.0, 10.0,  25.0, 30.0,  40, 32.0),
}


def _register_couplings() -> None:
    def _features(length: float):
        return [
            MatingFeature("axis", "axis",
                          {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
            MatingFeature("bore_a", "hole",
                          {"origin": [0, 0, length], "axis": [0, 0, -1],
                           "diameter_mm": 0}),  # filled below per component
            MatingFeature("bore_b", "hole",
                          {"origin": [0, 0, 0], "axis": [0, 0, 1],
                           "diameter_mm": 0}),
            MatingFeature("face_a", "face",
                          {"origin": [0, 0, length], "normal": [0, 0, 1]}),
            MatingFeature("face_b", "face",
                          {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
        ]

    for designation, (ba, bb, od, length, mass, cost) in _RIGID_COUPLINGS.items():
        feats = _features(length)
        feats[1].params["diameter_mm"] = ba
        feats[2].params["diameter_mm"] = bb
        spec = ComponentSpec(
            designation=designation,
            category="coupling",
            subcategory="rigid",
            description=f"Rigid clamping shaft coupling, "
                       f"bore A={ba}mm, bore B={bb}mm, OD={od}mm, L={length}mm",
            generate_fn=(lambda output_path, _ba=ba, _bb=bb, _od=od, _l=length:
                         generate_rigid_coupling(bore_a_mm=_ba, bore_b_mm=_bb,
                                                  od_mm=_od, length_mm=_l,
                                                  output_path=output_path)),
            purchased=True,
            material="6061_aluminum",
            mass_g=mass,
            cost_usd=cost,
            supplier="Ruland",
            mating_features=feats,
            dimensions={"bore_a_mm": ba, "bore_b_mm": bb, "od_mm": od, "length_mm": length},
        )
        register_component(spec)

    for designation, (ba, bb, od, length, mass, cost) in _FLEX_COUPLINGS.items():
        feats = _features(length)
        feats[1].params["diameter_mm"] = ba
        feats[2].params["diameter_mm"] = bb
        spec = ComponentSpec(
            designation=designation,
            category="coupling",
            subcategory="flexible",
            description=f"Beam-style flexible shaft coupling, "
                       f"bore A={ba}mm, bore B={bb}mm, OD={od}mm, L={length}mm",
            generate_fn=(lambda output_path, _ba=ba, _bb=bb, _od=od, _l=length:
                         generate_flexible_coupling(bore_a_mm=_ba, bore_b_mm=_bb,
                                                     od_mm=_od, length_mm=_l,
                                                     output_path=output_path)),
            purchased=True,
            material="6061_aluminum",
            mass_g=mass,
            cost_usd=cost,
            supplier="Ruland / Misumi",
            mating_features=feats,
            dimensions={"bore_a_mm": ba, "bore_b_mm": bb, "od_mm": od, "length_mm": length},
        )
        register_component(spec)


_register_couplings()
