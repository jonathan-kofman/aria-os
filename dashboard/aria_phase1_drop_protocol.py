"""
aria_phase1_drop_protocol.py — Phase 1 Drop Test Protocol
==========================================================
Defines exactly how to instrument and run the Phase 1 sandbag drop tests
so the results are measurable, reproducible, and comparable to CEM predictions.

Phase 1 testing is mechanical-only — no electronics, no motor.
Goal: verify the ratchet/pawl catch mechanism works under dynamic load.

Outputs a complete test record you can attach to the certification package.

Usage:
    python3 tools/aria_phase1_drop_protocol.py               # print protocol
    python3 tools/aria_phase1_drop_protocol.py --log-results # enter results interactively

Author: Jonathan Kofman — ARIA Project
"""

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

# ── CEM predictions to compare against ───────────────────────────────────────
CEM_PREDICTIONS = {
    'arrest_distance_mm': 54.0,   # from aria_cem.py at default inputs
    'peak_force_N':       5373.0,
    'avg_force_N':        2390.0,
    'catch_time_ms':      31.2,
}

ANSI_LIMITS = {
    'arrest_distance_mm': 813.0,
    'peak_force_N':       8000.0,
    'avg_force_N':        6000.0,
}

# ── Test matrix ───────────────────────────────────────────────────────────────
# Each test case: (label, drop_height_mm, mass_kg, expect_catch)
# expect_catch=False → sandbag should NOT engage clutch (below trigger threshold)
# expect_catch=True  → sandbag SHOULD engage clutch and arrest

TEST_MATRIX = [
    # Negative tests first — verify no false triggers
    ("NEG-01", 50,  20,  False, "Below trigger threshold — must NOT catch"),
    ("NEG-02", 100, 20,  False, "Below trigger threshold — must NOT catch"),
    ("NEG-03", 150, 20,  False, "Near threshold — must NOT catch"),

    # Positive tests — verify catch
    ("POS-01", 300, 20,  True,  "Above trigger threshold — must catch every time"),
    ("POS-02", 300, 40,  True,  "Heavier load, same height"),
    ("POS-03", 300, 60,  True,  "60 kg — real light climber equivalent"),
    ("POS-04", 500, 40,  True,  "Taller fall, verify arrest distance"),
    ("POS-05", 500, 80,  True,  "ANSI test mass equivalent (80 kg)"),
    ("POS-06", 500, 100, True,  "Full ANSI 100 kg test mass"),

    # Repeatability — same test 5 times
    ("REP-01", 300, 80,  True,  "Repeatability run 1/5"),
    ("REP-02", 300, 80,  True,  "Repeatability run 2/5"),
    ("REP-03", 300, 80,  True,  "Repeatability run 3/5"),
    ("REP-04", 300, 80,  True,  "Repeatability run 4/5"),
    ("REP-05", 300, 80,  True,  "Repeatability run 5/5"),
]


def print_protocol():
    print()
    print("="*70)
    print("ARIA PHASE 1 DROP TEST PROTOCOL")
    print("Mechanical-only (no electronics, no motor)")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d')}")
    print("="*70)

    print("""
OBJECTIVES
----------
1. Verify ratchet/pawl catch engages under dynamic load
2. Verify no false triggers below threshold drop height
3. Measure arrest distance and compare to CEM predictions
4. Verify repeatability (same result 5/5 consecutive tests)
5. Generate documented evidence for certification package

SAFETY RULES (MANDATORY)
-------------------------
• Never test with a human. ALWAYS use a sandbag or dead weight.
• Stand clear of the fall line during every drop.
• Inspect the assembly after every 5 drops for wear, deformation, or
  loose fasteners before continuing.
• If any catch fails or any structural deformation is visible: STOP.
  Do not continue until the root cause is identified and corrected.
• Wear safety glasses during all testing.

INSTRUMENTATION REQUIRED
------------------------
Minimum (cheap, accurate enough for Phase 1):
  □ Luggage scale (digital, 0-100 kg range, ±100g accuracy)
      → Used to weigh sandbags before each test
  □ Steel measuring tape
      → Used to set drop height and measure arrest distance
  □ Phone camera (slow-motion 240 fps or higher)
      → Records fall and arrest for post-analysis
  □ Masking tape + marker
      → Mark rope at spool exit before each test to measure rope payout

Recommended (for Phase 1 data that feeds Phase 3 correlation):
  □ Simple load cell + HX711 + Arduino + serial logging
      → Measures arrest force in real time (same hardware you'll use in ARIA)
      → ~$15 total. Gives you the arrest force curve not just peak.
  □ Gyroscope module on sandbag (MPU-6050, ~$3)
      → Measures deceleration during arrest
      → Lets you calculate arrest distance from kinematics as cross-check

SETUP
-----
1. Mount ARIA Phase 1 assembly (housing + spool + ratchet/pawl + flyweight)
   at the base of a wall or test stand. Secure with 4× M8 bolts to structural
   frame. Minimum pull-out rating: 16 kN per bolt.

2. Route rope through housing, over pulley/guide, up and over the wall/pulley
   at the top of the test height, back down to the sandbag.
   (Simple falling mass on a rope over a pulley — standard test configuration.)

3. Mark the rope with masking tape at the spool exit point. This is your
   reference for measuring rope payout = arrest distance.

4. Set up phone on tripod at 90° to fall line, 2-3 m back. Start recording.

5. Set sandbag drop height by measuring from sandbag bottom to floor.

MEASUREMENT PROCEDURE (per test)
---------------------------------
1. Weigh sandbag. Record exact mass.
2. Set drop height with measuring tape. Record exact height.
3. Mark rope at spool exit with new tape mark (or measure to existing mark).
4. Clear all personnel from fall line and below.
5. Release sandbag. Record time (for test log).
6. After arrest:
   a. Measure distance from new rope mark to spool exit = arrest distance
   b. Note whether catch engaged (pawl seated in tooth) or not
   c. Check for any deformation or wear visible on ratchet/pawl
7. Reset: retract rope by hand, re-mark rope, re-weigh sandbag if needed.
8. Wait 30 seconds between drops (allow springs/mechanism to settle).
""")

    print("TEST MATRIX")
    print("-"*70)
    print(f"  {'ID':<8} {'Drop (mm)':<12} {'Mass (kg)':<12} {'Expect':<10} {'Notes'}")
    print(f"  {'-'*8} {'-'*11} {'-'*11} {'-'*9} {'-'*30}")
    for tid, h, m, expect, note in TEST_MATRIX:
        e_str = "CATCH" if expect else "NO CATCH"
        print(f"  {tid:<8} {h:<12} {m:<12} {e_str:<10} {note}")

    print(f"""
EXPECTED RESULTS (from CEM)
---------------------------
  Arrest distance:  {CEM_PREDICTIONS['arrest_distance_mm']:.0f} mm   (ANSI limit: {ANSI_LIMITS['arrest_distance_mm']:.0f} mm)
  Peak force:       {CEM_PREDICTIONS['peak_force_N']:.0f} N   (ANSI limit: {ANSI_LIMITS['peak_force_N']:.0f} N)
  Avg force:        {CEM_PREDICTIONS['avg_force_N']:.0f} N   (ANSI limit: {ANSI_LIMITS['avg_force_N']:.0f} N)
  Catch time:       {CEM_PREDICTIONS['catch_time_ms']:.1f} ms

  Note: CEM predictions assume fully machined 17-4PH flyweights and
  correct spring preload. PETG prototype with added mass will differ.
  Record actual values and use aria_flyweight_verify.py to calibrate.

PHASE 1 PASS CRITERIA
----------------------
  □ All NEG tests: sandbag does NOT engage clutch (0 catches on 3 tests)
  □ All POS tests: sandbag DOES engage clutch (all catches)
  □ Repeatability: 5/5 catches on REP tests (same drop height and mass)
  □ Arrest distance: measured distance < {ANSI_LIMITS['arrest_distance_mm']:.0f} mm on all positive tests
  □ No structural failure or permanent deformation on any component
  □ Ratchet teeth not cracked or visibly worn after full test matrix

AFTER PHASE 1
-------------
  □ Input all results into aria_drop_parser.py to compute ANSI metrics
  □ Compare measured arrest distance to CEM prediction — record delta
  □ If measured > CEM: update CEM correction factor in aria_testdata_tab.py
  □ Save all slow-motion video with timestamp in /tests/phase1/video/
  □ Archive this completed protocol in /tests/phase1/drop_protocol_results.json
  □ Attach to certification package via aria_cert_package.py
""")
    print("="*70)


def log_results_interactive():
    """Interactive CLI to record actual test results."""
    print("\nARIA Phase 1 Drop Test — Result Logger")
    print("Enter results for each test. Press Enter to skip a test.")
    print()

    results = {
        'session_date':     datetime.now().isoformat(),
        'assembly_version': input("Assembly version (e.g. 'PETG-v1'): ").strip(),
        'flyweight_mass_g': float(input("Flyweight mass per unit (g): ") or "0"),
        'spring_preload_N': float(input("Spring preload force (N): ") or "2.5"),
        'notes':            input("Session notes: ").strip(),
        'tests':            [],
    }

    for tid, h_mm, m_kg, expect_catch, note in TEST_MATRIX:
        print(f"\n[{tid}]  Drop: {h_mm} mm  Mass: {m_kg} kg  Expected: {'CATCH' if expect_catch else 'NO CATCH'}")
        print(f"  {note}")

        skip = input("  Skip this test? (y/N): ").strip().lower()
        if skip == 'y':
            continue

        caught_str    = input("  Catch engaged? (y/n): ").strip().lower()
        arrest_mm_str = input("  Arrest distance (mm, or blank): ").strip()
        force_N_str   = input("  Peak force (N, or blank if not measured): ").strip()
        notes_str     = input("  Notes: ").strip()

        caught       = caught_str == 'y'
        arrest_mm    = float(arrest_mm_str) if arrest_mm_str else None
        force_N      = float(force_N_str)   if force_N_str   else None

        passed = (caught == expect_catch)
        if not passed:
            print(f"  ⚠  UNEXPECTED RESULT — expected {'CATCH' if expect_catch else 'NO CATCH'}")

        results['tests'].append({
            'id':            tid,
            'drop_height_mm': h_mm,
            'mass_kg':       m_kg,
            'expect_catch':  expect_catch,
            'caught':        caught,
            'passed':        passed,
            'arrest_mm':     arrest_mm,
            'peak_force_N':  force_N,
            'notes':         notes_str,
        })

    # Summary
    n_tested = len(results['tests'])
    n_passed = sum(1 for t in results['tests'] if t['passed'])
    print(f"\n── Test session complete ──")
    print(f"   Tests run:   {n_tested}")
    print(f"   Passed:      {n_passed}/{n_tested}")

    all_arrests = [t['arrest_mm'] for t in results['tests']
                   if t['arrest_mm'] is not None and t['expect_catch']]
    if all_arrests:
        print(f"   Max arrest dist: {max(all_arrests):.0f} mm  (ANSI limit: {ANSI_LIMITS['arrest_distance_mm']:.0f} mm)")

    # Save
    out_dir = Path("tests/phase1")
    out_dir.mkdir(parents=True, exist_ok=True)
    fname   = out_dir / f"drop_results_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    fname.write_text(json.dumps(results, indent=2))
    print(f"\n   Results saved to {fname}")
    print("   Run: python tools/aria_drop_parser.py tests/phase1/ to compute ANSI metrics")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARIA Phase 1 Drop Test Protocol")
    parser.add_argument("--log-results", action="store_true",
                        help="Interactively log test results")
    args = parser.parse_args()

    if args.log_results:
        log_results_interactive()
    else:
        print_protocol()
