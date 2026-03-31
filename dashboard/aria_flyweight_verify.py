"""
aria_flyweight_verify.py — Flyweight Engagement Speed Verification
==================================================================
Solves the PETG prototype problem: PETG flyweights are ~6× lighter than
17-4PH steel, so the clutch won't engage at the correct speed during
prototype testing. This tool tells you:

  1. What speed your PETG prototype WILL engage at
  2. What to add (steel washers/nuts) to tune it to the right speed
  3. A test protocol for verifying engagement on a drill/lathe
  4. How to confirm the corrected assembly before committing to metal

Usage:
    python3 tools/aria_flyweight_verify.py
    python3 tools/aria_flyweight_verify.py --added-mass-g 85

Background
----------
Centrifugal clutch engagement condition:
    F_centrifugal = m * omega^2 * r > F_spring (preload)

where:
    m     = flyweight mass (kg)
    omega = angular velocity (rad/s) = rope_speed / spool_radius
    r     = flyweight centroid radius from axis (m)
    F_sp  = spring preload force (N)

At engagement: m * omega_engage^2 * r = F_spring
Therefore:     omega_engage = sqrt(F_spring / (m * r))
And:           v_rope_engage = omega_engage * R_spool

If we change m → m + delta_m (by adding washers), the engagement speed changes.
We solve for delta_m that gives target engagement speed.

Target engagement speed: 1.27 m/s (from CEM)
Normal climbing speed:   ~0.30 m/s
Required safety margin:  1.27 / 0.30 = 4.23× (>3× required by Lead Solo design principle)

Author: Jonathan Kofman — ARIA Project
"""

import argparse
import math


# ── CEM design parameters (from aria_cem.py outputs) ─────────────────────────
DESIGN_PARAMS = {
    # Mechanical
    'flyweight_mass_17_4PH_g':  213.0,   # target mass per flyweight (17-4PH H900)
    'flyweight_radius_mm':       60.0,   # centroid radius from spool axis
    'spring_preload_N':           2.5,   # music wire spring preload force
    'n_flyweights':                 3,   # number of flyweight segments
    'spool_radius_mm':            60.0,  # rope spool radius (hub Dia 120mm → r=60mm)
    'target_engagement_v_ms':    1.27,   # target rope speed at engagement
    'normal_climb_v_ms':         0.30,   # typical climbing speed
    'required_margin':            4.23,  # v_engage / v_climb (>3.0 required)

    # PETG material (typical FDM PETG at 50% infill)
    'petg_density_g_cm3':         0.72,  # ~50% infill effective density

    # 17-4PH H900 material
    'steel_density_g_cm3':        7.78,

    # Geometry (cylinder approximation for flyweight body)
    'flyweight_diameter_mm':      20.0,
    'flyweight_length_mm':        58.0,
}


def cylinder_mass_g(diameter_mm: float, length_mm: float, density_g_cm3: float) -> float:
    """Mass of a solid cylinder in grams."""
    r_cm = (diameter_mm / 2) / 10
    l_cm = length_mm / 10
    vol_cm3 = math.pi * r_cm**2 * l_cm
    return vol_cm3 * density_g_cm3


def engagement_speed_ms(
        flyweight_mass_g: float,
        flyweight_radius_mm: float,
        spring_preload_N: float,
        spool_radius_mm: float,
) -> float:
    """Calculate rope speed at clutch engagement."""
    m   = flyweight_mass_g / 1000          # kg
    r   = flyweight_radius_mm / 1000       # m
    R   = spool_radius_mm / 1000           # m
    omega_engage = math.sqrt(spring_preload_N / (m * r))
    return omega_engage * R


def mass_for_target_speed_g(
        target_v_ms: float,
        flyweight_radius_mm: float,
        spring_preload_N: float,
        spool_radius_mm: float,
) -> float:
    """Solve for flyweight mass required to achieve target engagement speed."""
    r   = flyweight_radius_mm / 1000
    R   = spool_radius_mm / 1000
    # v = omega * R,  omega = v/R
    # m = F_spring / (omega^2 * r)
    omega = target_v_ms / R
    m_kg  = spring_preload_N / (omega**2 * r)
    return m_kg * 1000  # grams


def run_analysis(added_mass_g: float = 0.0):
    p = DESIGN_PARAMS

    # ── PETG prototype mass ───────────────────────────────────────────────────
    petg_mass_g = cylinder_mass_g(
        p['flyweight_diameter_mm'],
        p['flyweight_length_mm'],
        p['petg_density_g_cm3'],
    )
    total_petg_mass_g = petg_mass_g + added_mass_g

    # ── Engagement speeds ─────────────────────────────────────────────────────
    v_steel = engagement_speed_ms(
        p['flyweight_mass_17_4PH_g'],
        p['flyweight_radius_mm'],
        p['spring_preload_N'],
        p['spool_radius_mm'],
    )
    v_petg = engagement_speed_ms(
        total_petg_mass_g,
        p['flyweight_radius_mm'],
        p['spring_preload_N'],
        p['spool_radius_mm'],
    )
    v_target = p['target_engagement_v_ms']

    # ── Mass needed to match target ────────────────────────────────────────────
    m_needed_g  = mass_for_target_speed_g(
        v_target,
        p['flyweight_radius_mm'],
        p['spring_preload_N'],
        p['spool_radius_mm'],
    )
    m_to_add_g  = max(0.0, m_needed_g - total_petg_mass_g)

    # ── Drill/lathe RPM for verification test ─────────────────────────────────
    # Engagement condition in terms of spool RPM:
    omega_engage_steel = v_steel / (p['spool_radius_mm'] / 1000)
    rpm_steel          = omega_engage_steel * 60 / (2 * math.pi)

    omega_engage_petg  = v_petg / (p['spool_radius_mm'] / 1000)
    rpm_petg           = omega_engage_petg * 60 / (2 * math.pi)

    omega_engage_added = engagement_speed_ms(
        total_petg_mass_g, p['flyweight_radius_mm'],
        p['spring_preload_N'], p['spool_radius_mm']
    ) / (p['spool_radius_mm'] / 1000) * 60 / (2 * math.pi)

    # Margin calculations
    margin_steel = v_steel / p['normal_climb_v_ms']
    margin_petg  = v_petg  / p['normal_climb_v_ms']

    # ── Print report ─────────────────────────────────────────────────────────
    print()
    print("="*65)
    print("ARIA FLYWEIGHT ENGAGEMENT SPEED VERIFICATION")
    print("="*65)

    print(f"\n── Design targets ──")
    print(f"  Target engagement speed:  {v_target:.2f} m/s")
    print(f"  Normal climbing speed:    {p['normal_climb_v_ms']:.2f} m/s")
    print(f"  Required margin:          >{p['required_margin']:.1f}×")

    print(f"\n── 17-4PH H900 (final hardware) ──")
    print(f"  Flyweight mass per unit:  {p['flyweight_mass_17_4PH_g']:.1f} g")
    print(f"  Engagement speed:         {v_steel:.3f} m/s")
    print(f"  Safety margin:            {margin_steel:.2f}×  ({'OK' if margin_steel > p['required_margin'] else 'FAIL'})")
    print(f"  Spool RPM at engagement:  {rpm_steel:.0f} RPM")

    print(f"\n── PETG prototype (as-printed, no added mass) ──")
    print(f"  PETG flyweight mass:      {petg_mass_g:.1f} g")
    print(f"  Engagement speed:         {v_petg:.3f} m/s")
    print(f"  Safety margin:            {margin_petg:.2f}×")
    print(f"  Spool RPM at engagement:  {rpm_petg:.0f} RPM")
    density_ratio = p['steel_density_g_cm3'] / p['petg_density_g_cm3']
    print(f"  ⚠  PETG is {density_ratio:.1f}× lighter than steel →")
    print(f"     clutch engages at {v_petg:.2f} m/s instead of {v_target:.2f} m/s")
    print(f"     This is {'too fast' if v_petg > v_target else 'too slow'} — "
          f"{"won't catch normal falls" if v_petg > v_target else "will false-trigger during fast climbing"}")

    if added_mass_g > 0:
        print(f"\n── PETG + {added_mass_g:.1f} g added mass ──")
        print(f"  Total mass per flyweight: {total_petg_mass_g:.1f} g")
        v_with_added = engagement_speed_ms(
            total_petg_mass_g, p['flyweight_radius_mm'],
            p['spring_preload_N'], p['spool_radius_mm'])
        m_with_added = v_with_added / p['normal_climb_v_ms']
        print(f"  Engagement speed:         {v_with_added:.3f} m/s  "
              f"(target {v_target:.2f} m/s)")
        print(f"  Safety margin:            {m_with_added:.2f}×")
        print(f"  Spool RPM at engagement:  {omega_engage_added:.0f} RPM")
        pct_error = abs(v_with_added - v_target) / v_target * 100
        print(f"  Error from target:        {pct_error:.1f}%  "
              f"({'ACCEPTABLE (<10%)' if pct_error < 10 else 'TOO HIGH - adjust mass'})")

    print(f"\n── What to add to PETG flyweights ──")
    print(f"  Mass needed per flyweight: {m_needed_g:.1f} g")
    print(f"  PETG flyweight mass:       {petg_mass_g:.1f} g")
    print(f"  Mass to add per flyweight: {m_to_add_g:.1f} g")

    # Suggest standard hardware to add
    m6_nut_g = 2.5; m8_nut_g = 5.0; m6_washer_g = 1.8; m8_washer_g = 3.5
    suggestions = []
    for n_m8 in range(0, 30):
        for n_m6 in range(0, 20):
            total = n_m8 * m8_nut_g + n_m6 * m6_nut_g
            if abs(total - m_to_add_g) < 3.0:
                suggestions.append((n_m8, n_m6, total))
    if suggestions:
        best = min(suggestions, key=lambda x: abs(x[2] - m_to_add_g))
        print(f"\n  Suggested hardware to bolt to flyweight arms:")
        print(f"    {best[0]} × M8 steel nut (~{m8_nut_g}g each)")
        print(f"    {best[1]} × M6 steel nut (~{m6_nut_g}g each)")
        print(f"    Total added: {best[2]:.1f} g  (need {m_to_add_g:.1f} g)")
    else:
        print(f"\n  Suggested: bolt steel nuts/washers to arms until total")
        print(f"  added mass = {m_to_add_g:.1f} g per flyweight.")
        print(f"  M8 nuts ({m8_nut_g}g each) and M6 nuts ({m6_nut_g}g each)")
        print(f"  are easiest to find and weigh accurately.")

    print(f"\n── Drill/lathe test protocol ──")
    print(f"  Goal: verify the clutch engages AT the right speed,")
    print(f"        NOT above it (misses falls) or below it (false triggers).")
    print()
    print(f"  1. Mount flyweight assembly on a variable-speed drill or lathe")
    print(f"     (use the main shaft if machined, or a 25mm mandrel)")
    print(f"  2. Hold a strip of paper against the ratchet teeth as a catch indicator")
    print(f"  3. Slowly ramp up speed from 0")
    print(f"  4. Note the RPM at which the pawl seats into the ratchet teeth")
    print()
    print(f"  Expected RPM values:")
    print(f"    Bare PETG (no added mass):  {rpm_petg:.0f} RPM")
    print(f"    Target (with added mass):   {rpm_steel:.0f} RPM")
    print(f"    Maximum acceptable:          {rpm_steel * 1.10:.0f} RPM (+10%)")
    print(f"    Minimum acceptable:          {rpm_steel * 0.90:.0f} RPM (-10%)")
    print()
    print(f"  5. If engagement RPM is outside ±10% of target:")
    print(f"     - Too high (engages too fast): add more mass")
    print(f"     - Too low (engages too slowly): remove mass")
    print(f"  6. Test at least 5 times and average — engagement speed")
    print(f"     should be consistent within ±5 RPM across trials")
    print()
    print(f"  7. False-trigger verification (CRITICAL):")
    print(f"     Run at {p['normal_climb_v_ms'] / (p['spool_radius_mm']/1000) * 60 / (2*math.pi):.0f} RPM (normal climbing speed)")
    print(f"     for 60 seconds continuously.")
    print(f"     The pawl must NOT seat during this test.")
    print(f"     If it does: spring preload is too low, increase preload.")

    print(f"\n── Before committing to metal ──")
    print(f"  1. Drill test passes (engagement at {rpm_steel:.0f} ±{rpm_steel*0.10:.0f} RPM)")
    print(f"  2. False-trigger test passes (no engagement at climbing speed)")
    print(f"  3. Drop test from 300 mm: sandbag catches every time (≥5/5)")
    print(f"  4. Drop test from 100 mm: sandbag does NOT catch (0/5)")
    print(f"     (100 mm is below trigger threshold — must NOT engage)")
    print(f"  Only then: proceed to machine 17-4PH flyweights at CEM dimensions")
    print("="*65)
    print()

    return {
        'petg_mass_g':         petg_mass_g,
        'total_with_added_g':  total_petg_mass_g,
        'v_petg_ms':           v_petg,
        'v_steel_ms':          v_steel,
        'v_target_ms':         v_target,
        'mass_to_add_g':       m_to_add_g,
        'rpm_target':          rpm_steel,
        'rpm_petg':            rpm_petg,
        'margin_steel':        margin_steel,
        'margin_petg':         margin_petg,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARIA Flyweight Engagement Speed Verification")
    parser.add_argument("--added-mass-g", type=float, default=0.0,
                        help="Mass added per flyweight (g) for tuning (e.g. steel nuts/washers)")
    args = parser.parse_args()
    run_analysis(added_mass_g=args.added_mass_g)
