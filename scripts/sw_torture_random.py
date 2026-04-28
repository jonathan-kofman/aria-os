r"""sw_torture_random.py - random part generator for breadth coverage.

Composes N random parts using ONLY ledger-verified-PASS features:
  - sketchCircle, sketchRect, sketchPolyline (closed)
  - extrude (op=new and op=join)
  - revolve (full + partial)
  - fillet (constant radius)
  - chamfer (distance)
  - helix
  - shell (with face removal)

Each generated part is dimensionally varied, geometrically validated, and
the result feeds back into the learning ledger. Catches regressions where
a feature works in isolation but fails when composed with others.

Usage:
  python scripts/sw_torture_random.py --count 50         # 50 random parts
  python scripts/sw_torture_random.py --count 10 --seed 42
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import sw_learning_ledger as ledger_mod
from scripts import run_sw_feature_matrix as runner

OUT = REPO / "outputs" / "torture_random"


# Single-feature builders that the catalog has confirmed work.
def _gen_box(rng: random.Random) -> tuple[list[dict], dict]:
    w = rng.randint(20, 200)
    d = rng.randint(20, 200)
    h = rng.randint(10, 100)
    plan = [
        {"kind": "beginPlan"},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},
        {"kind": "sketchRect",
         "params": {"sketch": "s", "cx": 0, "cy": 0, "w": w, "h": d}},
        {"kind": "extrude",
         "params": {"sketch": "s", "distance": h, "alias": "blk"}},
    ]
    expected = {"bbox_mm": (w, d, h), "watertight": True}
    return plan, expected


def _gen_cylinder(rng: random.Random) -> tuple[list[dict], dict]:
    r = rng.randint(10, 80)
    h = rng.randint(5, 100)
    plan = [
        {"kind": "beginPlan"},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "s", "cx": 0, "cy": 0, "r": r}},
        {"kind": "extrude",
         "params": {"sketch": "s", "distance": h, "alias": "cyl"}},
    ]
    expected = {"bbox_mm": (2 * r, 2 * r, h), "watertight": True}
    return plan, expected


def _gen_polygon_prism(rng: random.Random) -> tuple[list[dict], dict]:
    n = rng.choice([3, 4, 5, 6, 7, 8])
    r = rng.randint(15, 60)
    h = rng.randint(5, 50)
    pts = []
    for i in range(n):
        a = math.radians(i * 360 / n)
        pts.append([round(r * math.cos(a), 3), round(r * math.sin(a), 3)])
    plan = [
        {"kind": "beginPlan"},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},
        {"kind": "sketchPolyline",
         "params": {"sketch": "s", "points": pts, "closed": True}},
        {"kind": "extrude",
         "params": {"sketch": "s", "distance": h, "alias": "p"}},
    ]
    expected = {"watertight": True}
    return plan, expected


def _gen_revolve(rng: random.Random) -> tuple[list[dict], dict]:
    """Revolve a simple rectangle profile to make a torus or disc."""
    inner = rng.randint(10, 50)
    outer = inner + rng.randint(10, 40)
    height = rng.randint(5, 40)
    pts = [[inner, 0], [outer, 0], [outer, height], [inner, height]]
    plan = [
        {"kind": "beginPlan"},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},
        {"kind": "sketchPolyline",
         "params": {"sketch": "s", "points": pts, "closed": True}},
        {"kind": "revolve",
         "params": {"sketch": "s", "axis": "Y",
                    "angle_deg": 360, "alias": "r"}},
    ]
    expected = {"bbox_mm": (2 * outer, height, 2 * outer),
                "watertight": True}
    return plan, expected


def _gen_filleted_box(rng: random.Random) -> tuple[list[dict], dict]:
    plan, exp = _gen_box(rng)
    fil_r = rng.randint(2, 8)
    plan.append({"kind": "fillet",
                 "params": {"edges": [], "radius": fil_r, "alias": "f"}})
    return plan, exp


def _gen_chamfered_cyl(rng: random.Random) -> tuple[list[dict], dict]:
    plan, exp = _gen_cylinder(rng)
    plan.append({"kind": "fillet",
                 "params": {"edges": [], "radius": rng.randint(1, 4),
                            "type": "chamfer", "alias": "c"}})
    return plan, exp


def _gen_shelled_box(rng: random.Random) -> tuple[list[dict], dict]:
    plan, exp = _gen_box(rng)
    wall = rng.choice([2, 3, 4, 5])
    h = exp["bbox_mm"][2]
    plan.append({"kind": "shell",
                 "params": {"thickness": wall, "remove_faces": [[0, 0, h]]}})
    exp["min_volume_ratio"] = 0.05
    exp["max_volume_ratio"] = 0.60
    return plan, exp


def _gen_helix(rng: random.Random) -> tuple[list[dict], dict]:
    r = rng.randint(5, 30)
    pitch = rng.randint(2, 10)
    revs = rng.randint(2, 8)
    plan = [
        {"kind": "beginPlan"},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "s", "cx": 0, "cy": 0, "r": r}},
        {"kind": "helix",
         "params": {"sketch": "s", "pitch_mm": pitch,
                    "revolutions": revs, "alias": "h"}},
    ]
    return plan, {}  # helix is a curve, no body to verify


def _gen_compound_part(rng: random.Random) -> tuple[list[dict], dict]:
    """Box base + cylinder boss on top."""
    bw = rng.randint(40, 120)
    bd = rng.randint(40, 120)
    bh = rng.randint(10, 30)
    cr = rng.randint(8, 20)
    ch = rng.randint(10, 40)
    plan = [
        {"kind": "beginPlan"},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "b"}},
        {"kind": "sketchRect",
         "params": {"sketch": "b", "cx": 0, "cy": 0, "w": bw, "h": bd}},
        {"kind": "extrude",
         "params": {"sketch": "b", "distance": bh, "alias": "base"}},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "c"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "c", "cx": 0, "cy": 0, "r": cr}},
        {"kind": "extrude",
         "params": {"sketch": "c", "distance": ch,
                    "operation": "join", "alias": "boss",
                    "start_offset": bh}},
    ]
    expected = {"bbox_mm": (bw, bd, bh + ch), "watertight": True}
    return plan, expected


GENERATORS = [
    _gen_box, _gen_cylinder, _gen_polygon_prism, _gen_revolve,
    _gen_filleted_box, _gen_chamfered_cyl, _gen_shelled_box,
    _gen_helix, _gen_compound_part,
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--gen-only", action="store_true",
                    help="Only emit plans to stdout, don't run")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    OUT.mkdir(parents=True, exist_ok=True)

    rows = []
    led = ledger_mod.load()
    for i in range(args.count):
        gen = rng.choice(GENERATORS)
        name = gen.__name__.replace("_gen_", "")
        plan, expected = gen(rng)
        slug = f"r{i:03d}_{name}"
        print(f"\n=== {slug} ===")
        if args.gen_only:
            for op in plan:
                print(f"  {op['kind']}: {op.get('params', {})}")
            continue

        t0 = time.time()
        plan_norm = runner.normalize_plan(plan)
        op_ok, op_total, op_errors = runner.execute_plan(plan_norm)
        time.sleep(0.3)

        # Save STL only (drop sldprt + step to keep run fast)
        stl = OUT / f"{slug}.stl"
        if stl.exists():
            stl.unlink()
        runner.post("saveAs", {"path": str(stl)})

        geom = {"overall_pass": False, "checks": []}
        if stl.exists():
            geom = runner.geometry_check(stl, expected, {})

        passed = (op_ok == op_total) and (
            geom.get("overall_pass") or not expected)
        ledger_mod.record_result(led, name, passed=passed,
            error=(op_errors[-1] if op_errors else None),
            call_path=f"torture:{slug}")
        rows.append({"slug": slug, "feature": name,
                     "ops": f"{op_ok}/{op_total}",
                     "passed": passed,
                     "stats": geom.get("stats", {}),
                     "elapsed_s": round(time.time() - t0, 1)})
        print(f"  ops={op_ok}/{op_total} passed={passed} "
              f"stats={geom.get('stats', {})}")

    ledger_mod.save(led)

    # Per-feature rollup
    print("\n# Torture run summary")
    from collections import Counter
    cnt: dict[str, list] = {}
    for r in rows:
        cnt.setdefault(r["feature"], []).append(r["passed"])
    print(f"| feature | tries | passed |")
    print(f"|---------|-------|--------|")
    for f, ls in sorted(cnt.items()):
        print(f"| {f} | {len(ls)} | {sum(ls)} |")

    n_pass = sum(1 for r in rows if r["passed"])
    print(f"\n=== TORTURE DONE: {n_pass}/{len(rows)} passed ===")


if __name__ == "__main__":
    main()
