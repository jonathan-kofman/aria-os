"""Motors — NEMA-standard stepper motors."""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_nema_stepper


# NEMA stepper dimensions per NEMA ICS 16
# designation -> (frame_size, body_length, shaft_dia, shaft_length,
#                 mount_bolt_dia, mount_pcd, pilot_dia, pilot_height,
#                 mass_g, cost_usd, torque_Nm)
_NEMA_STEPPERS: dict[str, tuple[float, float, float, float, float, float, float, float, float, float, float]] = {
    # NEMA 14 — 35mm frame
    "NEMA14-28mm-5mm":     (35.0, 28.0, 5.0, 20.0, 3.0, 26.0, 22.0, 2.0, 130, 18.0, 0.15),
    # NEMA 17 — 42mm frame (most common in 3D printers + small robots)
    "NEMA17-34mm-5mm":     (42.3, 34.0, 5.0, 22.0, 3.0, 31.0, 22.0, 2.0, 280, 15.0, 0.35),
    "NEMA17-48mm-5mm":     (42.3, 48.0, 5.0, 22.0, 3.0, 31.0, 22.0, 2.0, 380, 18.0, 0.55),
    # NEMA 23 — 56mm frame
    "NEMA23-51mm-6.35mm":  (56.4, 51.0, 6.35, 21.0, 5.2, 47.14, 38.0, 1.6, 620, 35.0, 0.70),
    "NEMA23-56mm-8mm":     (56.4, 56.0, 8.0, 24.0, 5.2, 47.14, 38.0, 1.6, 780, 40.0, 1.10),
    "NEMA23-76mm-8mm":     (56.4, 76.0, 8.0, 24.0, 5.2, 47.14, 38.0, 1.6, 1100, 55.0, 2.00),
    # NEMA 34 — 86mm frame
    "NEMA34-80mm-12.7mm":  (86.0, 80.0, 12.7, 35.0, 6.6, 69.58, 73.0, 2.0, 2600, 95.0, 4.50),
    "NEMA34-115mm-14mm":   (86.0, 115.0, 14.0, 40.0, 6.6, 69.58, 73.0, 2.0, 4200, 130.0, 8.00),
}


def _register_steppers() -> None:
    for designation, dims in _NEMA_STEPPERS.items():
        (frame, body_len, shaft_d, shaft_l, bolt_d, pcd,
         pilot_d, pilot_h, mass, cost, torque) = dims

        spec = ComponentSpec(
            designation=designation,
            category="motor",
            subcategory="stepper",
            description=f"{designation.split('-')[0]} stepper motor, "
                       f"{body_len}mm body, {shaft_d}mm shaft, "
                       f"{torque} Nm holding torque",
            generate_fn=(lambda output_path,
                         _f=frame, _bl=body_len, _sd=shaft_d, _sl=shaft_l,
                         _bd=bolt_d, _pcd=pcd, _pd=pilot_d, _ph=pilot_h:
                         generate_nema_stepper(
                             frame_size_mm=_f, body_length_mm=_bl,
                             shaft_dia_mm=_sd, shaft_length_mm=_sl,
                             mount_bolt_dia_mm=_bd, mount_pcd_mm=_pcd,
                             pilot_dia_mm=_pd, pilot_height_mm=_ph,
                             output_path=output_path)),
            purchased=True,
            material="steel_body_alu_endbell",
            mass_g=mass,
            cost_usd=cost,
            supplier="StepperOnline / OMC",
            supplier_pn=designation,
            mating_features=[
                MatingFeature("shaft_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("shaft_tip", "point",
                              {"origin": [0, 0, pilot_h + shaft_l]}),
                MatingFeature("mount_face", "face",
                              {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
                MatingFeature("mount_bolts", "bolt_circle",
                              {"center": [0, 0, 0], "axis": [0, 0, 1],
                               "pcd_mm": pcd * 1.414,  # square pattern diagonal
                               "n_bolts": 4, "bolt_dia_mm": bolt_d}),
                MatingFeature("pilot", "hole",
                              {"origin": [0, 0, 0], "axis": [0, 0, 1],
                               "diameter_mm": pilot_d}),
            ],
            dimensions={
                "frame_size_mm": frame, "body_length_mm": body_len,
                "shaft_dia_mm": shaft_d, "shaft_length_mm": shaft_l,
                "mount_bolt_dia_mm": bolt_d, "mount_pcd_mm": pcd,
                "holding_torque_nm": torque,
            },
        )
        register_component(spec)

    # Register short aliases for common motors
    aliases = {
        "NEMA17": "NEMA17-48mm-5mm",
        "NEMA23": "NEMA23-56mm-8mm",
        "NEMA34": "NEMA34-80mm-12.7mm",
    }
    from .catalog import catalog
    for alias, full in aliases.items():
        src = catalog.get(full)
        if src is None:
            continue
        # Shallow-clone as the alias
        from dataclasses import replace
        register_component(replace(src, designation=alias))


_register_steppers()
