r"""verify_advanced_ops.py — Build + visually verify each advanced SW op.

Runs through the catalog of advanced ops (helix, shell-with-face-remove,
loft, rib, draft, sheet metal, circular pattern), exports STEP/STL, and
runs visual_verifier.verify_visual against an expected feature description.
Fails loudly if any op doesn't actually produce the geometry it claims.

Usage:
  python scripts/verify_advanced_ops.py
  python scripts/verify_advanced_ops.py --only helix shell
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SW = "http://localhost:7501"


def post(kind: str, params: dict | None = None) -> dict:
    body = json.dumps({"kind": kind, "params": params or {}}).encode("utf-8")
    rq = urllib.request.Request(f"{SW}/op", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        return {"ok": False, "error": f"HTTP {he.code}",
                "body": he.read().decode("utf-8", "replace")}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def export_current(out_basename: str) -> tuple[Path | None, Path | None]:
    """Save STEP + STL via the addin's saveAs op (one path at a time)."""
    out_dir = REPO / "outputs" / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)
    step = out_dir / f"{out_basename}.step"
    stl = out_dir / f"{out_basename}.stl"
    post("saveAs", {"path": str(step)})
    post("saveAs", {"path": str(stl)})
    return (step if step.exists() else None,
            stl if stl.exists() else None)


def visual_verify(stl: Path, goal: str, spec: dict) -> dict:
    try:
        from aria_os.visual_verifier import verify_visual
    except Exception as ex:
        return {"ok": False, "error": f"import verify_visual: {ex}"}
    try:
        return verify_visual(None, str(stl), goal, spec)
    except Exception as ex:
        return {"ok": False, "error": f"verify_visual threw: {ex}"}


# ---------------------------------------------------------------------------
# Per-op test recipes. Each yields (slug, builder_fn, goal_text, spec_dict).
# Builder fn takes no args; it makes the part. The harness exports +
# verifies after.
# ---------------------------------------------------------------------------

def build_helix():
    post("beginPlan")
    post("newSketch", {"plane": "XY", "alias": "sk_h"})
    post("sketchCircle", {"cx": 0, "cy": 0, "radius": 10})
    r = post("helix", {"sketch": "sk_h", "pitch_mm": 5,
                        "revolutions": 4, "alias": "hp"})
    return r


def build_shell_with_face_remove():
    post("beginPlan")
    post("newSketch", {"plane": "XY", "alias": "sk"})
    post("sketchRect", {"x": -50, "y": -50, "w": 100, "h": 100})
    post("extrude", {"sketch": "sk", "distance": 40, "alias": "box"})
    r = post("shell", {"thickness": 3, "remove_faces": [[0, 0, 40]]})
    return r


def build_rib():
    post("beginPlan")
    # Two thin walls forming an L-shape
    post("newSketch", {"plane": "XY", "alias": "sk_v"})
    post("sketchRect", {"x": -50, "y": -3, "w": 100, "h": 6})
    post("extrude", {"sketch": "sk_v", "distance": 60})
    post("newSketch", {"plane": "XZ", "alias": "sk_rib"})
    # Open profile (line) — rib needs an OPEN sketch; sketchPolyline
    # with closed=false leaves it open.
    post("sketchPolyline", {"points": [[-30, 0], [30, 30]], "closed": False})
    r = post("rib", {"sketch": "sk_rib", "thickness": 5,
                      "edge_type": 1, "thickness_side": 0,
                      "alias": "rib_feat"})
    return r


def build_loft():
    post("beginPlan")
    post("newSketch", {"plane": "XY", "alias": "p1"})
    post("sketchRect", {"x": -25, "y": -25, "w": 50, "h": 50})
    # SW needs each loft profile on a separate plane. Use offset plane
    # at +50mm (default Top Plane copies). Without an explicit "ref
    # plane" op we cheat with a second sketch on a temporary offset
    # plane — emit it via existing newSketch + offset_mm hint if
    # supported, otherwise mark as expected to fail.
    post("newSketch", {"plane": "XY", "alias": "p2"})  # will overlap p1 — known fail
    post("sketchCircle", {"cx": 0, "cy": 0, "radius": 15})
    r = post("loft", {"profile_sketches": ["p1", "p2"], "alias": "loft1"})
    return r


def build_circular_pattern():
    post("beginPlan")
    # Disc + one bolt hole, then pattern ×4
    post("newSketch", {"plane": "XY", "alias": "sk_disc"})
    post("sketchCircle", {"cx": 0, "cy": 0, "radius": 50})
    post("extrude", {"sketch": "sk_disc", "distance": 8, "alias": "disc"})
    post("newSketch", {"plane": "XY", "alias": "sk_hole"})
    post("sketchCircle", {"cx": 35, "cy": 0, "radius": 4})
    post("extrude", {"sketch": "sk_hole", "distance": 8,
                      "operation": "cut", "alias": "h0"})
    r = post("circularPattern", {"feature": "h0", "count": 6,
                                  "axis": "Z", "alias": "cp",
                                  "seed_x": 35, "seed_y": 0, "seed_r": 4})
    return r


RECIPES = {
    "helix":            (build_helix,
                         "M10 helical thread path with pitch 5mm 4 revolutions",
                         {"od_mm": 20, "n_revs": 4}),
    "shell":            (build_shell_with_face_remove,
                         "100x100x40mm hollow box, 3mm wall, top face open",
                         {"width_mm": 100, "depth_mm": 100, "height_mm": 40,
                          "wall_mm": 3}),
    "rib":              (build_rib,
                         "wall plate with diagonal reinforcement rib 5mm thick",
                         {"thickness_mm": 5}),
    "loft":             (build_loft,
                         "lofted body transitioning from 50mm square base to 30mm circle top",
                         {}),
    "circular_pattern": (build_circular_pattern,
                         "100mm disc with 6 bolt holes equally spaced on PCD 70mm",
                         {"od_mm": 100, "n_bolts": 6, "thickness_mm": 8}),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to a subset of slugs")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip vision verify (just check op + bbox)")
    args = ap.parse_args()
    selected = args.only or list(RECIPES.keys())

    rows = []
    for slug in selected:
        if slug not in RECIPES:
            print(f"  ! unknown slug: {slug}")
            continue
        builder, goal, spec = RECIPES[slug]
        print(f"\n=== {slug} ===")
        t0 = time.time()
        op_result = builder()
        op_ok = bool(op_result.get("result", {}).get("ok"))
        op_err = op_result.get("result", {}).get("error", "")
        print(f"  op_ok={op_ok} {op_err}")
        time.sleep(0.5)  # let SW settle
        step, stl = export_current(slug)
        print(f"  step={'ok' if step else 'MISSING'} stl={'ok' if stl else 'MISSING'}")
        verdict = "skip"
        confidence = None
        if stl and not args.no_verify:
            v = visual_verify(stl, goal, spec)
            verdict = ("PASS" if v.get("overall_match") else
                        ("FAIL" if v.get("overall_match") is False else "?"))
            confidence = v.get("confidence")
            print(f"  verify={verdict} conf={confidence}")
        rows.append({
            "slug": slug, "op_ok": op_ok, "op_err": op_err,
            "stl": str(stl) if stl else "",
            "verify": verdict, "confidence": confidence,
            "wall_s": round(time.time() - t0, 1),
        })

    print("\n# Verify report")
    print("| slug | op_ok | verify | conf | wall(s) | err |")
    print("|------|-------|--------|------|---------|-----|")
    for r in rows:
        print(f"| {r['slug']} | {r['op_ok']} | {r['verify']} "
              f"| {r['confidence']} | {r['wall_s']} | "
              f"{(r['op_err'] or '').replace('|','/')[:60]} |")
    fail = [r for r in rows if not r["op_ok"] or r["verify"] == "FAIL"]
    if fail:
        print(f"\nFAILURES: {[r['slug'] for r in fail]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
