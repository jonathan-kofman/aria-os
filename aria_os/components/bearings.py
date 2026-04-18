"""Bearings — deep-groove ball bearings (6000/6200/6300 series)."""
from __future__ import annotations

from .catalog import ComponentSpec, MatingFeature, register_component
from ._cq_helpers import generate_deep_groove_bearing


# Standard deep-groove ball bearings
# designation -> (bore, od, width, mass_g, cost_usd, dynamic_C_N, static_C0_N, max_rpm_grease)
# Load ratings from SKF catalog (typical values; verify for critical applications)
_BEARINGS: dict[str, tuple[float, float, float, float, float, float, float, float]] = {
    # 6000 series — extra-light
    "6000":     (10, 26,  8,  19,  5.0,  4.75e3,  1.96e3, 34000),
    "6001":     (12, 28,  8,  22,  5.5,  5.40e3,  2.36e3, 30000),
    "6002":     (15, 32,  9,  30,  6.0,  5.60e3,  2.85e3, 26000),
    "6003":     (17, 35, 10,  39,  6.5,  6.05e3,  3.25e3, 24000),
    "6004":     (20, 42, 12,  68,  7.5,  9.36e3,  5.00e3, 20000),
    "6005":     (25, 47, 12,  79,  8.5, 11.9e3,   7.35e3, 17000),
    "6006":     (30, 55, 13, 118, 10.0, 13.8e3,   8.30e3, 15000),
    "6007":     (35, 62, 14, 155, 12.0, 16.8e3,  10.2e3,  13000),
    "6008":     (40, 68, 15, 190, 14.0, 17.8e3,  11.6e3,  11000),
    # 6200 series — light (most common)
    "6200":     (10, 30,  9,  32,  5.5,  5.07e3,  2.24e3, 32000),
    "6201":     (12, 32, 10,  37,  6.0,  6.89e3,  3.10e3, 28000),
    "6202":     (15, 35, 11,  46,  6.5,  7.80e3,  3.75e3, 26000),
    "6203":     (17, 40, 12,  66,  7.0,  9.56e3,  4.75e3, 22000),
    "6204":     (20, 47, 14, 105,  8.0, 13.5e3,   6.55e3, 19000),
    "6205":     (25, 52, 15, 130,  9.0, 14.0e3,   7.80e3, 16000),
    "6206":     (30, 62, 16, 195, 11.0, 20.3e3,  11.2e3,  13000),
    "6207":     (35, 72, 17, 286, 14.0, 27.0e3,  15.3e3,  11000),
    # 6300 series — heavy
    "6300":     (10, 35, 11,  55,  7.0,  8.06e3,  3.40e3, 28000),
    "6301":     (12, 37, 12,  67,  7.5,  9.75e3,  4.15e3, 26000),
    "6302":     (15, 42, 13,  84,  8.0, 11.9e3,   5.40e3, 22000),
    "6303":     (17, 47, 14, 113,  9.0, 14.3e3,   6.55e3, 19000),
    "6304":     (20, 52, 15, 150, 10.0, 16.8e3,   7.80e3, 17000),
    "6305":     (25, 62, 17, 232, 12.0, 23.4e3,  11.6e3,  14000),
}


def _register_bearings() -> None:
    for base, (bore, od, width, mass, cost, dyn_c, stat_c, max_rpm) in _BEARINGS.items():
        # Register multiple seal variants of each bearing
        for seal_suffix, seal_label in (("", "open"), ("-2RS", "rubber seal"),
                                         ("-ZZ", "metal shield")):
            designation = f"{base}{seal_suffix}"
            spec = ComponentSpec(
                designation=designation,
                category="bearing",
                subcategory="deep_groove",
                description=f"Deep-groove ball bearing {base} ({seal_label}), "
                           f"bore {bore}mm x OD {od}mm x W {width}mm",
                generate_fn=(lambda output_path, _b=bore, _o=od, _w=width:
                             generate_deep_groove_bearing(
                                 bore_mm=_b, od_mm=_o, width_mm=_w,
                                 output_path=output_path)),
                purchased=True,
                material="chrome_steel",
                mass_g=mass,
                cost_usd=cost * (1.1 if seal_suffix else 1.0),  # sealed slightly more
                supplier="SKF / NTN / Timken",
                supplier_pn=designation,
                mating_features=[
                    MatingFeature("inner_axis", "axis",
                                  {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                    MatingFeature("inner_bore", "hole",
                                  {"origin": [0, 0, 0], "axis": [0, 0, 1],
                                   "diameter_mm": bore}),
                    MatingFeature("outer_face", "face",
                                  {"origin": [0, 0, 0], "normal": [0, 0, -1]}),
                    MatingFeature("outer_od", "axis",
                                  {"origin": [0, 0, 0], "direction": [0, 0, 1]}),
                    MatingFeature("top_face", "face",
                                  {"origin": [0, 0, width], "normal": [0, 0, 1]}),
                ],
                dimensions={"bore_mm": bore, "od_mm": od, "width_mm": width},
                dynamic_load_n=dyn_c,
                static_load_n=stat_c,
                max_rpm=max_rpm,
            )
            register_component(spec)


_register_bearings()


# ---------------------------------------------------------------------------
# Load-based selection — L10 bearing life calculation
# ---------------------------------------------------------------------------

def select_bearing(
    bore_mm: float,
    *,
    load_radial_n: float = 0,
    rpm: float = 100,
    target_life_hours: float = 20000,
    sealed: bool = True,
):
    """
    Pick the smallest bearing that meets the load case + life requirement.

    Uses L10 life formula for ball bearings:
        L10 = (C / P)^3 * (1e6 / (60 * rpm))   [hours]
    C = dynamic load capacity (N), P = equivalent dynamic load (N).

    Returns (ComponentSpec, life_hours) or (None, 0) if nothing fits.
    """
    from .catalog import catalog

    bore = float(bore_mm)
    candidates = [c for c in catalog.list_subcategory("deep_groove")
                  if c.dimensions.get("bore_mm") == bore]
    if sealed:
        candidates = [c for c in candidates
                      if c.designation.endswith("-2RS") or c.designation.endswith("-ZZ")]

    viable: list[tuple[float, object]] = []
    for c in candidates:
        if c.dynamic_load_n is None or c.dynamic_load_n <= 0:
            continue
        if load_radial_n <= 0:
            life = float("inf")
        else:
            life_revs = (c.dynamic_load_n / load_radial_n) ** 3
            life = life_revs * 1e6 / (60 * max(rpm, 1))
        if life >= target_life_hours and rpm <= (c.max_rpm or 1e9):
            viable.append((life, c))

    if not viable:
        return None, 0
    viable.sort(key=lambda x: x[1].dimensions.get("od_mm", 999))
    life, spec = viable[0]
    return spec, life
