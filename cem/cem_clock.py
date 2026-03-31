"""
cem_clock.py — Mechanical clock gear train CEM module.

Derives a complete, dimensionally consistent gear train from top-level
requirements (beat rate, run time). Also computes pendulum length,
arbor diameters, mainspring barrel dimensions, and safety factors.

Gear train (5-wheel, going barrel):
  Barrel → Center (2nd) → Third → Fourth (seconds) → Escape wheel
  + Motion works: Cannon pinion → Minute wheel → Hour wheel (12:1)

Default design point (verified):
  Beat: 7200 BPH (half-second beat, 248mm pendulum)
  Run:  8 days
  Barrel: 96t  makes 16 turns in 8 days
  Center: p8  80t   1 rev/hr  (minute hand)
  Third:  p8  48t   10 rev/hr
  Fourth: p8  64t   60 rev/hr = 1 rev/min (seconds hand)
  Escape: p8  15t   480 rev/hr = 8 rev/min → 120 BPM = 7200 BPH ✓
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import math


@dataclass
class ClockInputs:
    beat_rate_bph: float  = 7200.0   # beats per hour
    run_days: float       = 8.0      # power reserve
    mainspring_turns: int = 16       # spring turns over full run
    material_yield_mpa: float = 400.0  # brass (CZ121): ~400 MPa yield
    module_escape: float = 0.5       # escape wheel module (mm)
    module_pinion: float = 0.5       # pinion module
    module_wheel:  float = 1.0       # wheel module (center, third, fourth)
    module_barrel: float = 1.5       # barrel module


@dataclass
class GearStage:
    name: str
    wheel_teeth: int
    pinion_leaves: int
    module_mm: float
    speed_rev_per_hr: float
    pitch_diameter_mm: float
    pinion_pitch_diameter_mm: float
    arbor_diameter_mm: float
    face_width_mm: float


def compute_clock(inp: ClockInputs | None = None) -> dict[str, Any]:
    if inp is None:
        inp = ClockInputs()

    bph  = inp.beat_rate_bph
    bpm  = bph / 60.0
    g    = 9806.65  # mm/s²

    # --- Pendulum ---
    # Half-period = 1 beat; full period T = 2/bpm minutes = 120/bpm seconds
    T_s = 120.0 / bpm                          # period in seconds
    L_mm = g * (T_s / (2 * math.pi))**2       # pendulum length mm
    beat_rate_hz = bpm / 60.0

    # --- Escape wheel ---
    ew_teeth  = 15
    ew_speed  = bpm / ew_teeth                 # rev/min

    # --- Fourth wheel (seconds hand = 1 rev/min) ---
    # Fourth drives escape pinion (p_e leaves)
    # ew_speed = fourth_speed * (N_4 / p_e)
    # fourth_speed = 1 rev/min → N_4 / p_e = ew_speed / 1 = ew_speed
    p_e  = 8
    N_4  = round(ew_speed * p_e)              # should be 64 for default
    fourth_speed = 1.0                         # rev/min

    # --- Third wheel ---
    # third_speed * (N_3 / p_4) = fourth_speed
    # center_speed * (N_2 / p_3) = third_speed
    # We need center = 1 rev/hr = 1/60 rev/min
    # (N_2/p_3)*(N_3/p_4) = fourth/center = 60
    # Use p_3=p_4=8 → (N_2/8)*(N_3/8)=60 → N_2*N_3=3840
    # Standard: N_2=80, N_3=48 → 80*48=3840 ✓
    p_3  = 8
    p_4  = 8
    N_2  = 80
    N_3  = 48
    third_speed  = fourth_speed * p_4 / N_3   # rev/min (= 1/6 rev/min for defaults)
    center_speed = third_speed  * p_3 / N_2   # rev/min (= 1/60 rev/min ✓)

    # --- Barrel ---
    # center_speed = barrel_speed * (N_barrel / p_center)
    # barrel makes inp.mainspring_turns in inp.run_days
    run_min  = inp.run_days * 24 * 60
    barrel_speed = inp.mainspring_turns / run_min   # rev/min
    # ratio needed = center_speed / barrel_speed
    ratio_needed = center_speed / barrel_speed
    # Use p_center = 8 → N_barrel = ratio_needed * 8
    p_center = 8
    N_barrel = round(ratio_needed * p_center)       # ~96 for defaults

    # Pitch diameters
    def pd(N, m): return N * m

    stages: list[GearStage] = []

    def arbor_d(pd_mm, sf=3.0):
        # Simple: arbor diameter ~ pd/6 minimum, but at least 3mm
        return max(3.0, round(pd_mm / 8, 1))

    def fw(m): return max(4.0, round(m * 6, 1))  # face width = 6× module

    barrel_pd = pd(N_barrel, inp.module_barrel)
    stages.append(GearStage(
        name="barrel",
        wheel_teeth=N_barrel, pinion_leaves=p_center,
        module_mm=inp.module_barrel,
        speed_rev_per_hr=barrel_speed * 60,
        pitch_diameter_mm=barrel_pd,
        pinion_pitch_diameter_mm=pd(p_center, inp.module_wheel),
        arbor_diameter_mm=arbor_d(barrel_pd),
        face_width_mm=fw(inp.module_barrel),
    ))

    center_pd = pd(N_2, inp.module_wheel)
    stages.append(GearStage(
        name="center (2nd)",
        wheel_teeth=N_2, pinion_leaves=p_3,
        module_mm=inp.module_wheel,
        speed_rev_per_hr=center_speed * 60,
        pitch_diameter_mm=center_pd,
        pinion_pitch_diameter_mm=pd(p_3, inp.module_pinion),
        arbor_diameter_mm=arbor_d(center_pd),
        face_width_mm=fw(inp.module_wheel),
    ))

    third_pd = pd(N_3, inp.module_wheel)
    stages.append(GearStage(
        name="third",
        wheel_teeth=N_3, pinion_leaves=p_4,
        module_mm=inp.module_wheel,
        speed_rev_per_hr=third_speed * 60,
        pitch_diameter_mm=third_pd,
        pinion_pitch_diameter_mm=pd(p_4, inp.module_pinion),
        arbor_diameter_mm=arbor_d(third_pd),
        face_width_mm=fw(inp.module_wheel),
    ))

    fourth_pd = pd(N_4, inp.module_wheel)
    stages.append(GearStage(
        name="fourth (seconds)",
        wheel_teeth=N_4, pinion_leaves=p_e,
        module_mm=inp.module_wheel,
        speed_rev_per_hr=fourth_speed * 60,
        pitch_diameter_mm=fourth_pd,
        pinion_pitch_diameter_mm=pd(p_e, inp.module_escape),
        arbor_diameter_mm=arbor_d(fourth_pd),
        face_width_mm=fw(inp.module_wheel),
    ))

    escape_pd = pd(ew_teeth, inp.module_escape)
    stages.append(GearStage(
        name="escape",
        wheel_teeth=ew_teeth, pinion_leaves=p_e,
        module_mm=inp.module_escape,
        speed_rev_per_hr=ew_speed * 60,
        pitch_diameter_mm=escape_pd,
        pinion_pitch_diameter_mm=pd(p_e, inp.module_escape),
        arbor_diameter_mm=arbor_d(escape_pd),
        face_width_mm=fw(inp.module_escape),
    ))

    # --- Tooth bending safety factor (Lewis equation approximation) ---
    # SF = (sigma_y * b * m * Y) / (W_t)
    # W_t = torque / r_pitch  — simplified check for center wheel only
    # (full analysis deferred to FEA)
    lewis_Y = 0.32    # Lewis form factor for 40-tooth gear at 20° PA
    sigma_y = inp.material_yield_mpa
    m       = inp.module_wheel
    b       = fw(m)
    # Approximate tangential load on center wheel from mainspring torque estimate
    mainspring_torque_nmm = barrel_pd * 0.5 * 0.8   # rough: 0.8N at pitch radius
    W_t_center = mainspring_torque_nmm / (center_pd / 2)
    sf_tooth   = (sigma_y * b * m * lewis_Y) / max(W_t_center, 0.01)

    # --- Mainspring barrel dimensions ---
    barrel_od_mm = barrel_pd + 2 * inp.module_barrel   # approximate
    barrel_id_mm = barrel_od_mm * 0.6
    barrel_depth_mm = fw(inp.module_barrel) + 2.0

    # --- Motion works (12:1 hour hand) ---
    # Cannon pinion (on center arbor): 10t
    # Minute wheel: 40t (pinion 4 leaves) → intermediate 40/10=4
    # Actually simplest: cannon 10t → minute wheel 30t → minute pinion 4t → hour wheel 48t?
    # Ratio needed: 12:1
    # 12 = (N_mw / N_cp) * (N_hw / N_mwp) where N_cp=cannon teeth
    # Standard: cannon=12, minute_wheel=48, minute_pinion=4, hour_wheel=48
    # ratio = (48/12) * (48/4) = 4 * 12 = 48 (too many)
    # Try: cannon=12, minute_wheel=36, minute_pinion=3, hour_wheel=36
    # (36/12)*(36/3) = 3*12 = 36 still too many
    # Standard 12:1: cannon 10t, minute wheel 40t (ratio 4), minute pinion 10t, hour wheel 30t (ratio 3)
    # 4*3=12 ✓
    motion_cannon   = 10
    motion_mw       = 40
    motion_mwp      = 10
    motion_hour     = 30

    summary = {
        # Pendulum
        "pendulum_length_mm":    round(L_mm, 1),
        "beat_rate_bph":         bph,
        "beat_rate_hz":          round(beat_rate_hz, 4),
        "pendulum_period_s":     round(T_s, 4),

        # Run
        "run_days":              inp.run_days,
        "mainspring_turns":      inp.mainspring_turns,
        "barrel_od_mm":          round(barrel_od_mm, 1),
        "barrel_id_mm":          round(barrel_id_mm, 1),
        "barrel_depth_mm":       round(barrel_depth_mm, 1),

        # Gear train stages
        "stages":                [vars(s) for s in stages],

        # Key tooth counts (flat, for easy access by cadquery_generator)
        "barrel_teeth":          N_barrel,
        "center_teeth":          N_2,   "center_pinion":  p_3,
        "third_teeth":           N_3,   "third_pinion":   p_4,
        "fourth_teeth":          N_4,   "fourth_pinion":  p_e,
        "escape_teeth":          ew_teeth,

        # Pitch diameters
        "barrel_pd_mm":          round(barrel_pd, 2),
        "center_pd_mm":          round(center_pd, 2),
        "third_pd_mm":           round(third_pd, 2),
        "fourth_pd_mm":          round(fourth_pd, 2),
        "escape_pd_mm":          round(escape_pd, 2),

        # Motion works
        "cannon_pinion_teeth":   motion_cannon,
        "minute_wheel_teeth":    motion_mw,
        "minute_pinion_leaves":  motion_mwp,
        "hour_wheel_teeth":      motion_hour,

        # Safety factors
        "sf_tooth_center":       round(sf_tooth, 2),
        "sf_ok":                 sf_tooth >= 2.0,

        # Module sizes
        "module_wheel_mm":       inp.module_wheel,
        "module_escape_mm":      inp.module_escape,
        "module_barrel_mm":      inp.module_barrel,
    }
    return summary


def compute_for_goal(goal: str, params: dict | None = None) -> dict:
    """Entry point used by the CEM pipeline orchestrator."""
    inp_kwargs: dict = {}
    if params:
        field_map = {
            "beat_rate_bph":       float,
            "run_days":            float,
            "mainspring_turns":    int,
            "module_wheel":        float,
            "module_escape":       float,
            "module_barrel":       float,
        }
        for k, cast in field_map.items():
            if k in params and params[k] is not None:
                try:
                    inp_kwargs[k] = cast(params[k])
                except (TypeError, ValueError):
                    pass
    inp = ClockInputs(**inp_kwargs)
    result = compute_clock(inp)
    return {"part_family": "clock", **result}


if __name__ == "__main__":
    import json
    result = compute_for_goal("skeleton clock")
    print(json.dumps({k: v for k, v in result.items() if k != "stages"}, indent=2))
    print("\nGear train:")
    for stage in result["stages"]:
        print(f"  {stage['name']:22s}  {stage['wheel_teeth']}t / p{stage['pinion_leaves']}  "
              f"  PD={stage['pitch_diameter_mm']:.1f}mm  "
              f"  {stage['speed_rev_per_hr']:.2f} rev/hr")
