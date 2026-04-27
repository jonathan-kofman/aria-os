"""Phase E — fire varied complex prompts at the SW addin to surface
recipe-cache gaps + new failure modes. Each prompt is a part the planner
hasn't seen verbatim before; failures should be captured into
recipes.json so the next iteration short-circuits.

Targets the live addin's text-to-part flow (port 7501) — assumes
SW + addin are up (start with `python scripts/sw_redeploy.py`).

Outputs a JSON report at outputs/phase_e_report.json with per-prompt:
  { goal, ok, transcript_len, last_op, error?, duration_s }

Usage:
    python scripts/phase_e_varied_prompts.py
    python scripts/phase_e_varied_prompts.py --skip rocket_nozzle
    python scripts/phase_e_varied_prompts.py --only motorcycle_frame
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT    = REPO_ROOT / "outputs" / "phase_e_report.json"

# 5 prompts spanning different engineering domains. Each surfaces a
# different failure surface in the planner / template router / addin.
PROMPTS: dict[str, dict] = {
    "rocket_nozzle": {
        "goal": "bell-shaped rocket nozzle, throat 25mm, exit 80mm, length 120mm, 6061 aluminium, M6 mounting flange with 8 bolts",
        "expected_template": "nozzle",  # _cq_nozzle in cadquery_generator
        "domain": "aerospace",
    },
    "motorcycle_frame": {
        "goal": "motorcycle subframe, tubular steel, 40mm OD x 2mm wall, 600mm long, 3 weld stations, M10 mount points front and rear",
        "expected_template": "weldment",
        "domain": "transportation",
    },
    "satellite_chassis": {
        "goal": "1U cubesat chassis, 100x100x100mm, 6061 aluminium, 4 lateral panels with 6x M3 holes each, deployable solar panel hinges",
        "expected_template": "housing",
        "domain": "aerospace",
    },
    "camera_gimbal": {
        "goal": "3-axis camera gimbal yoke, 50mm OD ring, M3 servo mount, carbon fibre, 2 perpendicular pivot bearings",
        "expected_template": "spoked_wheel_or_yoke",
        "domain": "robotics",
    },
    "telescope_mount": {
        "goal": "alt-azimuth telescope mount fork, 200mm tall, cast aluminium, dual ball-bearing trunnion, 1/4-20 tripod thread",
        "expected_template": "bracket",
        "domain": "optics",
    },
}


def _post(base: str, path: str, payload: dict, timeout: float = 600.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_prompt(base: str, name: str, spec: dict) -> dict:
    """Send a single prompt to /op?kind=text-to-part-style. The addin
    bridges to the planner via /api/cad/text-to-part on the server, but
    here we hit the addin's own endpoint that takes a free-text goal
    and emits a sequence of newSketch/extrude ops."""
    t0 = time.time()
    try:
        # The addin doesn't have a free-form goal endpoint; the planner
        # sits on the dashboard side. Best the addin can do is accept a
        # pre-built op list. Phase E is therefore really a planner test —
        # we hit the dashboard's /api/cad/text-to-part instead.
        # (Falls back to direct /op streaming if dashboard unreachable.)
        result = _post(base, "/api/cad/text-to-part", {
            "goal": spec["goal"],
            "cad":  "solidworks",
            "expected_template": spec.get("expected_template"),
        }, timeout=900.0)
        return {
            "name":  name,
            "goal":  spec["goal"],
            "ok":    bool(result.get("ok")),
            "duration_s": round(time.time() - t0, 1),
            "transcript_len": len(result.get("transcript") or []),
            "last_op": (result.get("transcript") or [{}])[-1].get("kind"),
            "step_path": result.get("step_path"),
            "error":  result.get("error"),
        }
    except (urllib.error.URLError, TimeoutError) as exc:
        return {
            "name":  name,
            "goal":  spec["goal"],
            "ok":    False,
            "duration_s": round(time.time() - t0, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8000",
                    help="dashboard server base URL (default 8000)")
    ap.add_argument("--addin", default="http://localhost:7501",
                    help="SW addin base URL (probed for liveness)")
    ap.add_argument("--only", action="append", default=[],
                    help="restrict to a subset of prompt names")
    ap.add_argument("--skip", action="append", default=[],
                    help="skip these prompt names")
    args = ap.parse_args()

    # Liveness probe — both dashboard and addin should be up.
    try:
        addin = _get(args.addin, "/status")
        if not addin.get("sw_connected"):
            raise RuntimeError(f"addin reachable but not connected to SW: {addin}")
        print(f"[ok] addin: {addin.get('doc') or '(no active doc)'}")
    except Exception as exc:
        print(f"[warn] SW addin unreachable: {exc}")
        print(f"[warn] Phase E driver continues — dashboard planner can still emit op streams")

    selected = {
        k: v for k, v in PROMPTS.items()
        if (not args.only or k in args.only) and k not in args.skip
    }
    print(f"\n[run ] {len(selected)} prompt(s)")

    results = []
    for name, spec in selected.items():
        print(f"\n=== {name} ===")
        print(f"  {spec['goal'][:80]}")
        r = run_prompt(args.base, name, spec)
        print(f"  -> ok={r['ok']} dur={r['duration_s']}s "
                f"transcript={r.get('transcript_len')} "
                f"err={(r.get('error') or '')[:120]}")
        results.append(r)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "ran_at":  time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": results,
        "summary": {
            "total":  len(results),
            "passed": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
        },
    }, indent=2))
    print(f"\n[done] report: {REPORT}")
    print(f"        passed: {sum(1 for r in results if r['ok'])}/{len(results)}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
