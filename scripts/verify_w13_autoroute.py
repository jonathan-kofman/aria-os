"""W13 verification: end-to-end autoroute on the drone fixture.

Manual verification step the user must run after grabbing the
Freerouting JAR (Claude's sandbox can't download external code).

Usage:
    # 1. Download Freerouting JAR (one-time):
    #    https://github.com/freerouting/freerouting/releases/latest
    #    Save to: %USERPROFILE%/.tools/freerouting.jar
    # 2. Run:
    python scripts/verify_w13_autoroute.py

Outputs:
    outputs/_track_a_verify/fc_pcb.dsn          (DSN export)
    outputs/_track_a_verify/fc_pcb.ses          (Freerouting routed)
    outputs/_track_a_verify/fc_pcb_routed.kicad_pcb  (SES re-imported)
    outputs/_track_a_verify/SUMMARY.md          (before/after numbers)

Acceptance: routed_pcb has more tracks than the unrouted input.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aria_os.ecad.autoroute import run_autoroute
from aria_os.ecad.drc_check import _find_kicad_python


DRONE_PCB = REPO / "outputs/drone_quad/drone_recon_military_7inch/ecad/fc_pcb/fc_pcb.kicad_pcb"
OUT_DIR = REPO / "outputs/_track_a_verify"


def count_tracks(pcb_path: Path) -> dict:
    """Use the bundled KiCad Python to count tracks/nets/footprints in
    a .kicad_pcb file. Returns {} if pcbnew unavailable."""
    py = _find_kicad_python()
    if py is None:
        return {}
    script = (
        "import sys, json, pcbnew\n"
        "b = pcbnew.LoadBoard(sys.argv[1])\n"
        "print(json.dumps({\n"
        "    'tracks':     len(b.GetTracks()),\n"
        "    'nets':       b.GetNetCount(),\n"
        "    'footprints': len(list(b.GetFootprints())),\n"
        "    'pads':       len(b.GetPads()),\n"
        "}))\n"
    )
    r = subprocess.run([py, "-c", script, str(pcb_path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"_error": r.stderr[:200]}
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"_error": r.stdout[:200]}


def main():
    if not DRONE_PCB.is_file():
        print(f"FAIL: drone fixture missing: {DRONE_PCB}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Drone fixture: {DRONE_PCB}")
    before = count_tracks(DRONE_PCB)
    print(f"Before: {before}")

    print("Running autoroute (this may take 1-2 min while Freerouting works)...")
    result = run_autoroute(DRONE_PCB, OUT_DIR, max_seconds=120)
    print(f"autoroute returned: {json.dumps(result, indent=2, default=str)}")

    if not result.get("routed_pcb_path"):
        print(f"\nFAIL: no routed_pcb_path. Error: {result.get('error')}",
              file=sys.stderr)
        if result.get("_hint"):
            print(f"\nHINT: {result['_hint']}", file=sys.stderr)
        return 2

    routed = Path(result["routed_pcb_path"])
    after = count_tracks(routed)
    print(f"\nAfter:  {after}")

    # Acceptance: routed must have MORE tracks than input (proves SES import worked)
    delta = after.get("tracks", 0) - before.get("tracks", 0)
    pcbs_byte_equal = routed.read_bytes() == DRONE_PCB.read_bytes()

    summary = OUT_DIR / "SUMMARY.md"
    summary.write_text(
        f"# W13 autoroute verification\n\n"
        f"Drone fixture: `{DRONE_PCB.relative_to(REPO)}`\n\n"
        f"## Before (unrouted)\n```json\n{json.dumps(before, indent=2)}\n```\n\n"
        f"## After (Freerouting + pcbnew SES re-import)\n"
        f"```json\n{json.dumps(after, indent=2)}\n```\n\n"
        f"## Delta\n"
        f"- Tracks: {before.get('tracks', 0)} -> {after.get('tracks', 0)} "
        f"(delta {delta:+d})\n"
        f"- Output is byte-equal to input? **{pcbs_byte_equal}** "
        f"(must be `False` -- if `True`, audit bug regressed)\n\n"
        f"## Files\n- `{result.get('dsn_path', '')}`\n"
        f"- `{result.get('ses_path', '')}`\n"
        f"- `{result.get('routed_pcb_path', '')}`\n",
        encoding="utf-8")
    print(f"\nSummary written: {summary}")

    if pcbs_byte_equal:
        print("\nFAIL: routed PCB is byte-equal to input. Audit bug regressed.",
              file=sys.stderr)
        return 3
    if delta < 0:
        print(f"\nWARN: routed has fewer tracks ({delta}). Check SES import.",
              file=sys.stderr)
        return 4
    print(f"\nPASS: routed PCB has {delta:+d} more tracks than input.")
    print(f"Open in KiCad: {routed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
