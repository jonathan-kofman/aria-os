r"""sw_feature_variation_gen.py - generate dim/position variants of any
test that has passed at least once.

The point: after the base matrix runs and we know which features actually
work, this generator deepens coverage by emitting N variations of each PASS
test (different sizes, aspect ratios, positions). The system learns the
operating envelope of each feature, not just "it worked once at default".

Output: a synthesized test list compatible with run_sw_feature_matrix.py.
Use:
  python scripts/sw_feature_variation_gen.py --per-feature 3 > /tmp/var.json
  python scripts/run_sw_feature_matrix.py --variants-file /tmp/var.json
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import sw_feature_matrix as catalog
from scripts import sw_feature_matrix_advanced as catalog_adv
from scripts import sw_learning_ledger as ledger_mod


SCALES = [0.5, 1.5, 2.0]   # dim multipliers


def _scale_op(op: dict, k: float) -> dict:
    """Scale all numeric dims in an op by k. Returns a new op dict."""
    op = copy.deepcopy(op)
    p = op.get("params", {})
    # Walk known numeric params
    for key in ("cx", "cy", "r", "radius", "w", "h", "x", "y",
                "distance", "thickness", "pitch_mm", "depth",
                "diameter", "cbore_diameter", "cbore_depth",
                "start_offset"):
        if key in p and isinstance(p[key], (int, float)):
            p[key] = round(p[key] * k, 3)
    if "points" in p and isinstance(p["points"], list):
        p["points"] = [[round(pt[0] * k, 3), round(pt[1] * k, 3)]
                        for pt in p["points"]]
    if "remove_faces" in p:
        p["remove_faces"] = [[round(c * k, 3) for c in face]
                              for face in p["remove_faces"]]
    return op


def _scale_test(test: dict, k: float) -> dict:
    """Build a scaled variation of a test."""
    new = copy.deepcopy(test)
    new["slug"] = f"{test['slug']}_x{int(k*10):03d}"
    # Wrap the original build_fn so scaled ops come out
    orig_build = test["build"]
    new["build"] = lambda b=orig_build, k=k: [_scale_op(o, k) for o in b()]
    # Scale expected dims
    if "bbox_mm" in new["expected"]:
        new["expected"]["bbox_mm"] = tuple(
            round(d * k, 2) for d in new["expected"]["bbox_mm"])
    # Scale spec dims
    spec = new["spec"]
    for key in ("od_mm", "bore_mm", "thickness_mm", "wall_mm",
                "width_mm", "height_mm", "depth_mm", "length_mm"):
        if key in spec and isinstance(spec[key], (int, float)):
            spec[key] = round(spec[key] * k, 2)
    return new


def generate_variations(per_feature: int = 3,
                         only_passed: bool = True) -> list[dict]:
    """For each catalog test, emit N scaled variations.

    If only_passed=True, restrict to tests whose feature_keys all show
    status=ok in the ledger (we want to deepen coverage of working features,
    not pile failures on broken ones).
    """
    ledger = ledger_mod.load() if only_passed else {}
    base_tests = list(catalog.TESTS) + list(catalog_adv.ADVANCED_TESTS)
    out = []
    for t in base_tests:
        if only_passed:
            statuses = [ledger.get(k, {}).get("status") for k in
                        t["feature_keys"]]
            if not all(s == "ok" for s in statuses):
                continue
        for k in SCALES[:per_feature]:
            out.append(_scale_test(t, k))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-feature", type=int, default=3)
    ap.add_argument("--all", action="store_true",
                    help="include unrecorded/failed features too")
    args = ap.parse_args()

    var = generate_variations(per_feature=args.per_feature,
                               only_passed=not args.all)
    print(f"# Generated {len(var)} variations", file=sys.stderr)
    for v in var:
        print(f"  {v['slug']}", file=sys.stderr)


if __name__ == "__main__":
    main()
