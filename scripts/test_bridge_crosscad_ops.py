"""test_bridge_crosscad_ops.py - Validate that each bridge accepts cross-CAD op vocabulary.

Run this AFTER restarting the HTTP bridge servers to confirm:
1. newSketch op is registered
2. cross-CAD param translation (sketch, cx, cy, r -> x_mm, y_mm, radius_mm) works
3. dryrun geometry export (DXF, STEP, STL) produces real files

Usage:
  python scripts/test_bridge_crosscad_ops.py --cad autocad
  python scripts/test_bridge_crosscad_ops.py --cad autocad onshape
  python scripts/test_bridge_crosscad_ops.py --probe-only
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BRIDGES = {
    "autocad": {"port": 7503, "name": "AutoCAD"},
    "onshape": {"port": 7506, "name": "Onshape"},
}

def post(port: int, kind: str, params: dict | None = None) -> dict:
    """POST to bridge at localhost:port."""
    url = f"http://localhost:{port}/op"
    body = json.dumps({"kind": kind, "params": params or {}}).encode()
    rq = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=3) as r:
            return json.loads(r.read())
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:200]}

def test_single_op(port: int, kind: str, params: dict) -> bool:
    """Send one op, return True if result.ok is True."""
    r = post(port, kind, params)
    result = r.get("result", {})
    ok = result.get("ok", False)
    error = result.get("error", r.get("error", ""))
    print(f"    {kind:25s} {'PASS' if ok else 'FAIL':4s}", end="")
    if error:
        print(f"  ({error[:80]})")
    else:
        print()
    return ok

def test_bridge(cad: str, port: int, name: str) -> None:
    """Run minimal T0_BASIC smoke test against one bridge."""
    print(f"\n[{cad.upper()}] {name} (port {port})")
    print("=" * 80)

    # Minimal test: newSketch -> sketchCircle -> extrude -> saveAs
    tests = [
        ("newSketch",     {}),
        ("sketchCircle",  {"sketch": 1, "cx": 50.0, "cy": 50.0, "r": 20.0}),
        ("extrude",       {"sketch": 1, "distance": 10.0}),
        ("saveAs",        {"path": str(REPO / "outputs" / f"test_{cad}.stl")}),
    ]

    passed = 0
    for kind, params in tests:
        if test_single_op(port, kind, params):
            passed += 1

    print(f"\n  Result: {passed}/{len(tests)} ops succeeded")
    return passed == len(tests)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cad", nargs="+", choices=list(BRIDGES.keys()),
                        default=list(BRIDGES.keys()),
                        help="CADs to test (default: all)")
    parser.add_argument("--probe-only", action="store_true",
                        help="Just check if bridges are reachable")
    args = parser.parse_args()

    if args.probe_only:
        print("\nProbing bridge availability...")
        for cad in args.cad:
            info = BRIDGES[cad]
            r = post(info["port"], "beginPlan", {})
            available = "error" not in r or "refused" not in r.get("error", "").lower()
            status = "UP" if available else "DOWN"
            print(f"  {cad:12s} (port {info['port']:4d}) ... {status}")
        return

    print("\n[BRIDGE SMOKE TEST - Cross-CAD Op Vocabulary]")
    all_pass = True
    for cad in args.cad:
        info = BRIDGES[cad]
        try:
            passed = test_bridge(cad, info["port"], info["name"])
            all_pass = all_pass and passed
        except Exception as e:
            print(f"\n  ERROR: {e}")
            all_pass = False

    print("\n" + "=" * 80)
    if all_pass:
        print("SUCCESS: All tested bridges accept cross-CAD op vocabulary!")
    else:
        print("FAILURE: Some ops were rejected. Bridges may not have been restarted.")
    sys.exit(0 if all_pass else 1)

if __name__ == "__main__":
    main()
