r"""run_sw_feature_matrix.py - execute every micro-test, verify, learn.

Pipeline per test:
  1. Run plan against running SW addin (port 7501).
  2. saveAs sldprt + step + stl to outputs/feature_matrix/<slug>.*
  3. geometry precheck: load STL with trimesh, compare bbox to spec, check
     watertight, body_count, holes (mesh.genus where requested),
     volume ratio for shells.
  4. Update sw_learning_ledger.json for every feature_key the test maps to.

After all tests:
  - Write outputs/feature_matrix/report.md with per-test result + per-feature
    rollup pulled from the ledger.
  - Exit 0 even on partial fails; the point is to LEARN, not to abort.

Usage:
  python scripts/run_sw_feature_matrix.py              # all tests
  python scripts/run_sw_feature_matrix.py --only sketch_circle pattern_circular_6
  python scripts/run_sw_feature_matrix.py --category sketch
  python scripts/run_sw_feature_matrix.py --skip-verify  # ops only, no STL load
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import sw_feature_matrix as catalog
from scripts import sw_feature_matrix_advanced as catalog_adv
from scripts import sw_feature_variation_gen as variation_gen
from scripts import sw_learning_ledger as ledger_mod

SW = "http://localhost:7501"
OUT = REPO / "outputs" / "feature_matrix"
OUT.mkdir(parents=True, exist_ok=True)


def post(kind: str, params: dict | None = None,
         timeout: float = 60) -> dict:
    body = json.dumps({"kind": kind, "params": params or {}}).encode()
    rq = urllib.request.Request(f"{SW}/op", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as he:
        return {"ok": False, "error": f"HTTP {he.code}",
                "body": he.read().decode("utf-8", "replace")}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def normalize_plan(plan: list[dict]) -> list[dict]:
    """Apply validator + ledger-aware workarounds before dispatch.

    This is the same path the orchestrator uses, so test results reflect
    what the LIVE system actually sends to the bridge. Without this, the
    runner would post raw circularPattern/linearPattern ops that the
    bridge can't execute - the test would fail not because the workaround
    is missing, but because the runner bypassed it.
    """
    try:
        from aria_os.native_planner import validator, feature_workarounds
        plan = validator._normalize_plan(plan)
        # _normalize_plan already applies workarounds, but be explicit so
        # we still get the rewrite even if the ledger file is missing.
        ledger = ledger_mod.load()
        plan = feature_workarounds.apply_workarounds(plan, ledger=ledger,
                                                      force=True)
    except Exception as ex:
        print(f"  ! normalize_plan threw: {ex}")
    return plan


def execute_plan(plan: list[dict]) -> tuple[int, int, list[str]]:
    """Run a list of {kind, params} ops. Returns (ok, total, error_list)."""
    plan = normalize_plan(plan)
    ok = 0
    errors = []
    for i, op in enumerate(plan):
        r = post(op["kind"], op.get("params", {}))
        succeeded = r.get("result", {}).get("ok", False)
        if succeeded:
            ok += 1
        else:
            err = r.get("result", {}).get("error",
                  r.get("error", "unknown"))
            errors.append(f"[{i}] {op['kind']}: {err[:120]}")
    return ok, len(plan), errors


def export(slug: str) -> dict[str, Path | None]:
    """Save STEP, STL, sldprt for the current part."""
    paths = {
        "sldprt": OUT / f"{slug}.sldprt",
        "step":   OUT / f"{slug}.step",
        "stl":    OUT / f"{slug}.stl",
    }
    for kind_path in [paths["sldprt"], paths["step"], paths["stl"]]:
        # Pre-clear so we don't observe stale files from a prior run
        try:
            if kind_path.exists():
                kind_path.unlink()
        except Exception:
            pass
        post("saveAs", {"path": str(kind_path)})
    return {k: (v if v.exists() else None) for k, v in paths.items()}


def geometry_check(stl_path: Path, expected: dict, spec: dict) -> dict:
    """Strict deterministic geometry check.

    Returns a dict of pass/fail per check. The overall_pass field is True iff
    every requested check passed.
    """
    out: dict = {"checks": [], "overall_pass": True, "stats": {}}
    if not stl_path or not stl_path.exists():
        out["overall_pass"] = False
        out["checks"].append(("file_exists", False, "STL missing"))
        return out
    try:
        import trimesh
        mesh = trimesh.load(str(stl_path), force="mesh")
    except Exception as ex:
        out["overall_pass"] = False
        out["checks"].append(("trimesh_load", False, str(ex)[:120]))
        return out
    # Empty mesh check
    if len(mesh.vertices) == 0 or mesh.volume == 0:
        out["overall_pass"] = False
        out["checks"].append(("non_empty", False,
                              f"verts={len(mesh.vertices)} vol={mesh.volume}"))
        return out
    extents = tuple(float(x) for x in mesh.bounding_box.extents)
    out["stats"]["bbox_mm"] = [round(e, 2) for e in extents]
    out["stats"]["volume_mm3"] = round(float(mesh.volume), 2)
    out["stats"]["watertight"] = bool(mesh.is_watertight)
    out["stats"]["body_count"] = int(mesh.body_count) \
        if hasattr(mesh, "body_count") else 1
    try:
        out["stats"]["genus"] = int(mesh.euler_number != 2 and
            (2 - mesh.euler_number) // 2) if mesh.is_watertight else None
    except Exception:
        out["stats"]["genus"] = None

    # bbox_mm tolerance check
    if "bbox_mm" in expected:
        ex_bbox = expected["bbox_mm"]
        # Allow any axis permutation, since SW's plane choice can swap axes
        actual_sorted = sorted(extents, reverse=True)
        ex_sorted = sorted(ex_bbox, reverse=True)
        for i, (a, e) in enumerate(zip(actual_sorted, ex_sorted)):
            tol = 0.20 * e + 1.0  # 20% or 1mm absolute, whichever is bigger
            ok = abs(a - e) <= tol
            out["checks"].append(
                (f"bbox[{i}]", ok, f"expect~{e:.1f}, got {a:.1f} (tol {tol:.1f})"))
            if not ok:
                out["overall_pass"] = False

    # watertight
    if expected.get("watertight"):
        ok = bool(mesh.is_watertight)
        out["checks"].append(("watertight", ok,
                              "watertight" if ok else "leaks"))
        if not ok:
            out["overall_pass"] = False

    # body_count
    if "body_count" in expected:
        bc = out["stats"].get("body_count", 1)
        ok = bc == expected["body_count"]
        out["checks"].append(("body_count", ok,
                              f"expect {expected['body_count']}, got {bc}"))
        if not ok:
            out["overall_pass"] = False

    # genus (number of through-holes / handles)
    if "genus" in expected and out["stats"].get("genus") is not None:
        g = out["stats"]["genus"]
        ok = g == expected["genus"]
        out["checks"].append(("genus", ok,
                              f"expect {expected['genus']}, got {g}"))
        if not ok:
            out["overall_pass"] = False

    # volume ratio (for shells - must NOT be solid)
    if "min_volume_ratio" in expected or "max_volume_ratio" in expected:
        bbox_vol = float(extents[0] * extents[1] * extents[2])
        if bbox_vol > 0:
            ratio = float(mesh.volume) / bbox_vol
            out["stats"]["volume_ratio"] = round(ratio, 3)
            mn = expected.get("min_volume_ratio", 0)
            mx = expected.get("max_volume_ratio", 1)
            ok = mn <= ratio <= mx
            out["checks"].append(("volume_ratio", ok,
                                  f"expect [{mn},{mx}], got {ratio:.3f}"))
            if not ok:
                out["overall_pass"] = False

    return out


def visual_verify(stl_path: Path, goal: str, spec: dict, slug: str) -> dict:
    """Render 3 views of the STL + (optionally) call vision LLM.

    Always saves PNG renders to outputs/feature_matrix/<slug>_views/ so the
    user can SEE every test result. The LLM call is best-effort - if no
    vision provider is configured, the renders alone provide visual evidence.

    Returns:
      {"renders": [paths], "overall_match": bool|None, "confidence": float|None,
       "issues": [str], "vision_provider": str|None}
    """
    out: dict = {"renders": [], "overall_match": None,
                  "confidence": None, "issues": [],
                  "vision_provider": None}
    if not stl_path or not stl_path.exists():
        out["issues"].append("no STL")
        return out
    try:
        from aria_os.visual_verifier import verify_visual
    except Exception as ex:
        out["issues"].append(f"import verify_visual: {ex}")
        return out
    try:
        # The verifier writes view PNGs into a subfolder of CWD by default;
        # patch its out_dir behavior by calling its private renderer first.
        from aria_os.visual_verifier import _render_views
        view_dir = OUT / f"{slug}_views"
        view_dir.mkdir(parents=True, exist_ok=True)
        try:
            paths, labels = _render_views(str(stl_path), goal, view_dir)
            out["renders"] = [str(p) for p in paths]
        except Exception as rex:
            out["issues"].append(f"render: {rex}")
        # Now do the full verify (renders again internally + vision LLM)
        v = verify_visual(None, str(stl_path), goal, spec)
        out["overall_match"] = v.get("overall_match")
        out["confidence"] = v.get("confidence")
        out["vision_provider"] = v.get("provider") or v.get("model")
        if v.get("issues"):
            out["issues"].extend(v["issues"][:3])
    except Exception as ex:
        out["issues"].append(f"verify_visual threw: {str(ex)[:120]}")
    return out


def run_one(test: dict, args) -> dict:
    slug = test["slug"]
    print(f"\n=== {slug:30s} [{test['category']:8s}] ===")
    t0 = time.time()
    plan = test["build"]()
    op_ok, op_total, op_errors = execute_plan(plan)
    print(f"  ops {op_ok}/{op_total}")
    if op_errors:
        for e in op_errors[:3]:
            print(f"     ! {e}")

    paths: dict = {}
    geom: dict = {"overall_pass": False, "checks": [], "stats": {}}
    visual: dict = {"renders": [], "overall_match": None}

    if op_ok > 0 and not args.skip_export:
        time.sleep(0.5)
        paths = export(slug)
        print(f"  exports: sldprt={'ok' if paths.get('sldprt') else 'X'}"
              f" step={'ok' if paths.get('step') else 'X'}"
              f" stl={'ok' if paths.get('stl') else 'X'}")

        if not args.skip_verify and paths.get("stl"):
            try:
                geom = geometry_check(paths["stl"], test["expected"],
                                      test["spec"])
            except Exception as ex:
                geom["checks"].append(("geom_check_crash", False,
                                       str(ex)[:120]))
            # Visual verification: render PNGs (always) + LLM check (best-effort).
            # Skipped only by --skip-visual, since user explicitly wants the
            # human-readable proof for every test.
            if not args.skip_visual:
                visual = visual_verify(paths["stl"], test["goal"],
                                        test["spec"], slug)
                vp = visual.get("vision_provider", "none")
                vm = visual.get("overall_match")
                vc = visual.get("confidence")
                print(f"  visual: provider={vp} match={vm} conf={vc} "
                      f"renders={len(visual.get('renders', []))}")

    geom_ok = geom.get("overall_pass", False)
    op_full = op_ok == op_total
    # Visual verdict (when present and definitive) overrides geometry pass
    visual_match = visual.get("overall_match")
    visual_veto = (visual_match is False)
    overall = (op_full
               and (geom_ok or args.skip_verify or not test["expected"])
               and not visual_veto)

    print(f"  geom_pass={geom_ok}  visual={visual_match}  "
          f"overall={overall}  stats={geom.get('stats', {})}")
    for label, ok, msg in geom.get("checks", []):
        if not ok:
            print(f"     X {label}: {msg}")

    return {
        "slug": slug,
        "category": test["category"],
        "feature_keys": test["feature_keys"],
        "ops": {"ok": op_ok, "total": op_total, "errors": op_errors[:5]},
        "exports": {k: bool(v) for k, v in paths.items()},
        "geom": geom,
        "visual": visual,
        "overall_pass": overall,
        "elapsed_s": round(time.time() - t0, 1),
    }


def update_ledger(rows: list[dict]) -> dict:
    led = ledger_mod.load()
    for row in rows:
        passed = row["overall_pass"]
        # Pull last error string for context
        err = ""
        if not passed:
            if row["ops"]["errors"]:
                err = row["ops"]["errors"][-1]
            elif row["geom"]["checks"]:
                bad = [c for c in row["geom"]["checks"] if not c[1]]
                if bad:
                    err = f"{bad[-1][0]}: {bad[-1][2]}"
        for key in row["feature_keys"]:
            ledger_mod.record_result(led, key, passed=passed, error=err,
                                     call_path=f"test:{row['slug']}")
    ledger_mod.save(led)
    return led


def write_report(rows: list[dict], led: dict) -> Path:
    md = ["# SW Feature Matrix Run\n",
          f"_Generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}_\n"]

    # Per-test table
    md.append("\n## Per-test results\n")
    md.append("| slug | category | ops | exports | geom | visual | overall | elapsed | renders |")
    md.append("|------|----------|-----|---------|------|--------|---------|---------|---------|")
    for r in rows:
        ops = f"{r['ops']['ok']}/{r['ops']['total']}"
        exps = "".join("S" if r["exports"].get(k) else "-"
                       for k in ("sldprt", "step", "stl"))
        geom = ("PASS" if r["geom"].get("overall_pass") else
                ("FAIL" if r["geom"].get("checks") else "SKIP"))
        v = r.get("visual", {})
        vm = v.get("overall_match")
        visual = ("PASS" if vm is True else
                   ("FAIL" if vm is False else "?"))
        renders = len(v.get("renders", []))
        ovr = "PASS" if r["overall_pass"] else "FAIL"
        md.append(f"| {r['slug']} | {r['category']} | {ops} | {exps} "
                  f"| {geom} | {visual} | {ovr} | {r['elapsed_s']}s | {renders} |")

    # Per-category rollup
    md.append("\n## By category\n")
    cats: dict[str, list] = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)
    md.append("| category | tests | passed | failed |")
    md.append("|----------|-------|--------|--------|")
    for cat, items in sorted(cats.items()):
        passed = sum(1 for r in items if r["overall_pass"])
        md.append(f"| {cat} | {len(items)} | {passed} | {len(items) - passed} |")

    # Failure detail (geometry checks)
    fails = [r for r in rows if not r["overall_pass"]]
    if fails:
        md.append("\n## Failures (detail)\n")
        for r in fails:
            md.append(f"\n### {r['slug']}")
            if r["ops"]["errors"]:
                md.append("  Op errors:")
                for e in r["ops"]["errors"]:
                    md.append(f"  - `{e}`")
            for label, ok, msg in r["geom"].get("checks", []):
                if not ok:
                    md.append(f"  - GEOM **{label}**: {msg}")

    # Ledger summary
    md.append("\n## Learning ledger (cumulative across all runs)\n")
    md.append(ledger_mod.summary(led))

    path = OUT / "report.md"
    path.write_text("\n".join(md), encoding="utf-8")

    json_rows = OUT / "report.json"
    json_rows.write_text(json.dumps(rows, indent=2, default=str),
                         encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to these slugs")
    ap.add_argument("--category", default=None,
                    help="restrict to a category (sketch/feat/pattern/...)")
    ap.add_argument("--skip-verify", action="store_true",
                    help="skip STL geometry checks (ops + export only)")
    ap.add_argument("--skip-export", action="store_true",
                    help="skip saveAs entirely (ops only)")
    ap.add_argument("--skip-visual", action="store_true",
                    help="skip rendering + vision LLM check")
    ap.add_argument("--extended", action="store_true",
                    help="include CSWPA + CSWE advanced tests")
    ap.add_argument("--only-extended", action="store_true",
                    help="run ONLY the advanced tests")
    ap.add_argument("--variations", type=int, default=0,
                    help="after base, run N scaled variants of each PASS test")
    args = ap.parse_args()

    tests: list[dict] = []
    if not args.only_extended:
        tests.extend(catalog.TESTS)
    if args.extended or args.only_extended:
        tests.extend(catalog_adv.ADVANCED_TESTS)
    if args.only:
        tests = [t for t in tests if t["slug"] in args.only]
    if args.category:
        tests = [t for t in tests if t["category"] == args.category]
    if not tests:
        print("No tests selected"); sys.exit(2)

    print(f"Running {len(tests)} tests...")
    rows = []
    for t in tests:
        try:
            rows.append(run_one(t, args))
        except Exception as ex:
            print(f"  CRASH in {t['slug']}: {ex}")
            traceback.print_exc()
            rows.append({
                "slug": t["slug"], "category": t["category"],
                "feature_keys": t["feature_keys"],
                "ops": {"ok": 0, "total": 0,
                        "errors": [f"runner crash: {ex}"]},
                "exports": {}, "geom": {"overall_pass": False, "checks": []},
                "overall_pass": False, "elapsed_s": 0,
            })

    led = update_ledger(rows)

    if args.variations > 0:
        var_tests = variation_gen.generate_variations(
            per_feature=args.variations, only_passed=True)
        print(f"\n=== variation pass: {len(var_tests)} variants ===")
        for t in var_tests:
            try:
                rows.append(run_one(t, args))
            except Exception as ex:
                print(f"  CRASH in {t['slug']}: {ex}")
        led = update_ledger(rows)

    report = write_report(rows, led)

    n_pass = sum(1 for r in rows if r["overall_pass"])
    print(f"\n=== DONE: {n_pass}/{len(rows)} passed ===")
    print(f"Report: {report}")
    print(f"Ledger: {ledger_mod.LEDGER_PATH}")


if __name__ == "__main__":
    main()
