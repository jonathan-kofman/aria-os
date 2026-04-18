"""
Linear motion — profile rails, ballscrews, timing pulleys.

Unlocks: CNC machines, 3D printers, pick-and-place, gantries, Cartesian robots.
"""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import (
    generate_linear_rail,
    generate_linear_carriage,
    generate_ballscrew,
    generate_ballscrew_nut,
    generate_gt2_pulley,
)


# ---------------------------------------------------------------------------
# MGN profile rails (HIWIN style) + matching carriages
# (size_id -> rail_width, rail_height, bolt_pitch, bolt_dia,
#             carriage_w, carriage_l, carriage_h, mount_x, mount_y, mount_bolt_dia,
#             rail_slot_width, dynamic_load_N, static_load_N)
# ---------------------------------------------------------------------------
_MGN: dict[str, tuple[float, ...]] = {
    "MGN7":  (7,  4.8, 15, 2.4,  17, 23, 8,  13,  8,  2.5,  7.3, 880,   1300),
    "MGN9":  (9,  6.0, 20, 3.5,  20, 30, 10, 15, 10, 3.0,   9.3, 1370,  2160),
    "MGN12": (12, 8.0, 25, 3.5,  27, 45, 13, 20, 15, 3.0,  12.3, 2800,  4310),
    "MGN15": (15, 9.5, 40, 4.5,  32, 56, 16, 25, 20, 4.0,  15.3, 4600,  6970),
    "HGH20": (20, 17.5, 60, 6.0, 44, 77, 30, 32, 36, 5.0,  20.5, 17200, 25800),
    "HGH25": (23, 22.0, 60, 7.0, 48, 84, 33, 35, 40, 6.0,  23.5, 23200, 34000),
}

# Common rail lengths (mm)
_RAIL_LENGTHS = [100, 200, 300, 400, 500, 600, 750, 1000, 1500]


def _register_rails_carriages() -> None:
    for size_id, d in _MGN.items():
        (rail_w, rail_h, pitch, bolt_dia,
         car_w, car_l, car_h, mx, my, car_bolt_dia,
         slot_w, dyn, stat) = d

        # Rails at various lengths
        for length in _RAIL_LENGTHS:
            designation = f"{size_id}_rail_{length}mm"
            mass_g = rail_w * rail_h * length * 7.85 / 1000  # rough steel mass
            # Cost — roughly $1 per 100mm for small, $2/100mm for HGH
            cost_per_100 = 2.5 if size_id.startswith("HGH") else 1.2
            cost_usd = (length / 100) * cost_per_100 + 8
            spec = ComponentSpec(
                designation=designation,
                category="linear_motion",
                subcategory="profile_rail",
                description=f"{size_id} profile rail, {length}mm long",
                generate_fn=(lambda output_path, _rw=rail_w, _rh=rail_h,
                                      _l=length, _p=pitch, _bd=bolt_dia:
                             generate_linear_rail(
                                 width_mm=_rw, height_mm=_rh, length_mm=_l,
                                 bolt_pitch_mm=_p, bolt_dia_mm=_bd,
                                 output_path=output_path)),
                purchased=True,
                material="hardened_steel_rail",
                mass_g=round(mass_g, 1),
                cost_usd=round(cost_usd, 2),
                supplier="HIWIN / Misumi / Misumi",
                supplier_pn=f"{size_id}H-{length}",
                mating_features=[
                    MatingFeature("rail_axis", "axis",
                                  {"origin": [0, 0, 0], "direction": [0, 1, 0]}),
                    MatingFeature("bottom_face", "face",
                                  {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
                    MatingFeature("top_face", "face",
                                  {"origin": [0, 0, rail_h], "normal": [0, 0, 1]}),
                ],
                dimensions={"rail_width_mm": rail_w, "rail_height_mm": rail_h,
                            "length_mm": length, "bolt_pitch_mm": pitch,
                            "bolt_dia_mm": bolt_dia},
            )
            register_component(spec)

        # Matching carriage block
        designation = f"{size_id}H_block"
        car_mass_g = car_w * car_l * car_h * 7.85 / 1000
        car_cost = {
            "MGN7": 6.0, "MGN9": 8.0, "MGN12": 10.0, "MGN15": 15.0,
            "HGH20": 25.0, "HGH25": 35.0,
        }[size_id]
        spec = ComponentSpec(
            designation=designation,
            category="linear_motion",
            subcategory="linear_carriage",
            description=f"{size_id}H carriage block, {dyn:.0f}N dynamic load",
            generate_fn=(lambda output_path, _cw=car_w, _cl=car_l, _ch=car_h,
                                  _mx=mx, _my=my, _mbd=car_bolt_dia, _sw=slot_w:
                         generate_linear_carriage(
                             width_mm=_cw, length_mm=_cl, height_mm=_ch,
                             mount_pattern_x_mm=_mx, mount_pattern_y_mm=_my,
                             mount_bolt_dia_mm=_mbd,
                             rail_slot_width_mm=_sw,
                             output_path=output_path)),
            purchased=True,
            material="hardened_steel_block",
            mass_g=round(car_mass_g, 1),
            cost_usd=car_cost,
            dynamic_load_n=float(dyn),
            static_load_n=float(stat),
            supplier="HIWIN / Misumi",
            mating_features=[
                MatingFeature("rail_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 1, 0]}),
                MatingFeature("top_face", "face",
                              {"origin": [0, 0, car_h], "normal": [0, 0, 1]}),
                MatingFeature("mount_pattern", "bolt_circle",
                              {"center": [0, 0, car_h], "axis": [0, 0, 1],
                               "pcd_mm": ((mx**2 + my**2) ** 0.5),
                               "n_bolts": 4, "bolt_dia_mm": car_bolt_dia}),
            ],
            dimensions={"carriage_width_mm": car_w, "carriage_length_mm": car_l,
                        "carriage_height_mm": car_h,
                        "mount_pattern_x_mm": mx, "mount_pattern_y_mm": my},
        )
        register_component(spec)


# ---------------------------------------------------------------------------
# SFU ballscrews (HIWIN/TBI pattern)
# (designation -> screw_dia, lead, nut_dia, nut_length, dynamic_C_N, static_C0_N, max_rpm)
# ---------------------------------------------------------------------------
_BALLSCREWS = {
    "SFU1204":  (12, 4,  24, 39, 7.1e3, 13.9e3, 4000),
    "SFU1605":  (16, 5,  28, 42, 10.4e3, 23.2e3, 3500),
    "SFU1610":  (16, 10, 28, 42, 10.4e3, 23.2e3, 3500),
    "SFU2005":  (20, 5,  36, 50, 18.7e3, 46.9e3, 3000),
    "SFU2010":  (20, 10, 36, 50, 15.4e3, 41.1e3, 3000),
    "SFU2505":  (25, 5,  40, 56, 25.3e3, 68.7e3, 2500),
    "SFU2510":  (25, 10, 40, 56, 24.1e3, 68.7e3, 2500),
    "SFU3210":  (32, 10, 52, 70, 38.5e3, 111e3,  2000),
}

_SCREW_LENGTHS = [300, 500, 750, 1000, 1500, 2000]


def _register_ballscrews() -> None:
    for designation, (screw_d, lead, nut_d, nut_l, dyn, stat, max_rpm) in _BALLSCREWS.items():
        for length in _SCREW_LENGTHS:
            full_designation = f"{designation}_L{length}mm"
            end_j_dia = screw_d - 3
            end_j_len = 20
            mass_g = (3.14159 * (screw_d / 2) ** 2 * length * 7.85 / 1000)
            # Ballscrew cost ≈ $0.15/mm + $50 base
            cost_usd = length * 0.15 + 50

            spec = ComponentSpec(
                designation=full_designation,
                category="linear_motion",
                subcategory="ballscrew",
                description=f"{designation} ballscrew, {length}mm long, lead {lead}mm, "
                           f"{dyn:.0f}N dynamic load",
                generate_fn=(lambda output_path,
                                      _sd=screw_d, _l=lead, _tl=length,
                                      _ej_d=end_j_dia, _ej_l=end_j_len:
                             generate_ballscrew(
                                 screw_dia_mm=_sd, lead_mm=_l,
                                 total_length_mm=_tl,
                                 end_journal_dia_mm=_ej_d,
                                 end_journal_length_mm=_ej_l,
                                 output_path=output_path)),
                purchased=True,
                material="hardened_steel_rolled",
                mass_g=round(mass_g, 1),
                cost_usd=round(cost_usd, 2),
                dynamic_load_n=float(dyn),
                static_load_n=float(stat),
                max_rpm=float(max_rpm),
                supplier="HIWIN / TBI Motion",
                mating_features=[
                    MatingFeature("screw_axis", "axis",
                                  {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                    MatingFeature("end_journal_bottom", "point",
                                  {"origin": [0, 0, -end_j_len]}),
                    MatingFeature("end_journal_top", "point",
                                  {"origin": [0, 0, length + end_j_len]}),
                ],
                dimensions={"screw_dia_mm": screw_d, "lead_mm": lead,
                            "length_mm": length, "nut_dia_mm": nut_d,
                            "nut_length_mm": nut_l},
            )
            register_component(spec)

            # Matching nut
            nut_designation = f"{designation}_nut"
            if nut_designation not in [s.designation for s in []]:  # dedupe via catalog already
                # Only register the nut once per screw family, not per length
                pass
        # Register nut once per family
        nut_des = f"{designation}_nut"
        # Check if already registered
        from .catalog import catalog as _cat
        if _cat.get(nut_des) is None:
            # Bolt clearance math: each M6 bolt is 6mm dia, needs ≥1mm wall on
            # each side. So bolt center must satisfy:
            #   body_R + bolt_R + safety ≤ pcd_R ≤ flange_R - bolt_R - safety
            # That requires (flange_R - body_R) ≥ 2*bolt_R + 2*safety = 8mm,
            # i.e. flange_dia ≥ body_dia + 16. Old `+12` left zero clearance →
            # bolt cutter tangent to body wall → non-watertight mesh.
            bolt_dia_mm = 6.0
            safety_mm = 1.0
            flange_dia = nut_d + 2 * (bolt_dia_mm + 2 * safety_mm)  # nut_d + 16
            flange_thick = 10
            pcd = (nut_d + flange_dia) / 2  # centered between body OD and flange OD
            spec = ComponentSpec(
                designation=nut_des,
                category="linear_motion",
                subcategory="ballscrew_nut",
                description=f"Flanged ballscrew nut for {designation}",
                generate_fn=(lambda output_path, _sd=screw_d, _fd=flange_dia,
                                      _bd=nut_d, _tl=nut_l + flange_thick,
                                      _ft=flange_thick, _pcd=pcd:
                             generate_ballscrew_nut(
                                 bore_dia_mm=_sd, flange_dia_mm=_fd,
                                 body_dia_mm=_bd, total_length_mm=_tl,
                                 flange_thickness_mm=_ft, flange_bolt_pcd_mm=_pcd,
                                 flange_bolt_dia_mm=6.0, n_bolts=4,
                                 output_path=output_path)),
                purchased=True,
                material="hardened_steel",
                mass_g=round((nut_d / 10) ** 3 * 7, 1),
                cost_usd=60 + screw_d,
                dynamic_load_n=float(dyn),
                static_load_n=float(stat),
                supplier="HIWIN / TBI Motion",
                mating_features=[
                    MatingFeature("screw_axis", "axis",
                                  {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                    MatingFeature("flange_face", "face",
                                  {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
                    MatingFeature("mount_bolts", "bolt_circle",
                                  {"center": [0, 0, 0], "axis": [0, 0, 1],
                                   "pcd_mm": pcd, "n_bolts": 4,
                                   "bolt_dia_mm": 6.0}),
                ],
                dimensions={"bore_mm": screw_d, "flange_dia_mm": flange_dia,
                            "body_dia_mm": nut_d},
            )
            register_component(spec)


# ---------------------------------------------------------------------------
# GT2 timing pulleys — common sizes
# (teeth, bore, belt_width, flange_dia)
# ---------------------------------------------------------------------------
_GT2_PULLEYS = [
    # 16T
    (16, 5.0, 6.0, 14.0),
    (16, 5.0, 10.0, 14.0),
    (16, 8.0, 6.0, 14.0),
    # 20T
    (20, 5.0, 6.0, 17.0),
    (20, 5.0, 10.0, 17.0),
    (20, 8.0, 6.0, 17.0),
    (20, 8.0, 10.0, 17.0),
    # 32T
    (32, 8.0, 10.0, 25.0),
    (32, 12.0, 10.0, 25.0),
    # 40T
    (40, 8.0, 10.0, 31.0),
    (40, 12.0, 10.0, 31.0),
    # 60T
    (60, 12.0, 10.0, 42.0),
]


def _register_pulleys() -> None:
    for n_teeth, bore, belt_width, flange_dia in _GT2_PULLEYS:
        designation = f"GT2_{n_teeth}T_bore{int(bore)}_w{int(belt_width)}"
        total_length = belt_width + 3.0
        # Aluminum density ~2.7 g/cm³
        mass_g = 3.14159 * (flange_dia / 2) ** 2 * total_length * 2.7 / 1000

        spec = ComponentSpec(
            designation=designation,
            category="linear_motion",
            subcategory="timing_pulley",
            description=f"GT2 timing pulley, {n_teeth} teeth, {bore}mm bore, "
                       f"{belt_width}mm belt width",
            generate_fn=(lambda output_path, _n=n_teeth, _b=bore,
                                  _bw=belt_width, _fd=flange_dia, _tl=total_length:
                         generate_gt2_pulley(
                             n_teeth=_n, bore_mm=_b, belt_width_mm=_bw,
                             flange_dia_mm=_fd, total_length_mm=_tl,
                             output_path=output_path)),
            purchased=True,
            material="6061_aluminum",
            mass_g=round(mass_g, 1),
            cost_usd=round(3 + n_teeth * 0.08, 2),
            supplier="Misumi / Gates / SDP-SI",
            mating_features=[
                MatingFeature("bore_axis", "axis",
                              {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                MatingFeature("shaft_bore", "hole",
                              {"origin": [0, 0, 0], "axis": [0, 0, 1],
                               "diameter_mm": bore}),
                MatingFeature("belt_engagement", "face",
                              {"origin": [0, 0, total_length / 2],
                               "normal": [1, 0, 0]}),
            ],
            dimensions={"n_teeth": n_teeth, "bore_mm": bore,
                        "belt_width_mm": belt_width, "flange_dia_mm": flange_dia},
        )
        register_component(spec)


_register_rails_carriages()
_register_ballscrews()
_register_pulleys()
