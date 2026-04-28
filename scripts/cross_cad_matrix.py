r"""cross_cad_matrix.py - run the SAME catalog against any native CAD bridge.

The op vocabulary (beginPlan/newSketch/sketchCircle/extrude/...) is shared
across SW, Rhino, Fusion, Onshape, AutoCAD. So the catalog in
sw_feature_matrix.py is portable. This runner aims it at any bridge port and
records results into a per-CAD ledger so we learn the feature support matrix
of every CAD without rewriting tests.

Bridges + default ports:
  sw       7501
  rhino    7502
  fusion   7503
  onshape  7504
  autocad  7505

Usage:
  python scripts/cross_cad_matrix.py                   # all available bridges
  python scripts/cross_cad_matrix.py --cad sw rhino    # specific bridges
  python scripts/cross_cad_matrix.py --probe-only      # health-check ports
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

from scripts import sw_feature_matrix as catalog
from scripts import sw_feature_matrix_advanced as catalog_adv
from scripts import sw_learning_ledger as ledger_mod

BRIDGES = {
    "sw":      {"port": 7501, "name": "SolidWorks"},
    "rhino":   {"port": 7502, "name": "Rhino"},
    "autocad": {"port": 7503, "name": "AutoCAD"},
    "fusion":  {"port": 7504, "name": "Fusion 360"},
    "onshape": {"port": 7506, "name": "Onshape"},
}

OUT = REPO / "outputs" / "cross_cad_matrix"
OUT.mkdir(parents=True, exist_ok=True)


def post(url: str, kind: str, params: dict | None = None,
         timeout: float = 30) -> dict:
    body = json.dumps({"kind": kind, "params": params or {}}).encode()
    rq = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as he:
        return {"ok": False, "error": f"HTTP {he.code}",
                "body": he.read().decode("utf-8", "replace")[:200]}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:200]}


def probe(cad: str) -> dict:
    """Try beginPlan against the bridge - returns availability.

    Strictly checks: HTTP succeeded AND result.ok is True. A bridge that
    accepts the request but rejects the op (Unknown kind / dryrun / stub)
    is reported as 'partial'.
    """
    info = BRIDGES[cad]
    url = f"http://localhost:{info['port']}/op"
    r = post(url, "beginPlan", {}, timeout=3)
    if "error" in r and "ok" not in str(r.get("kind", "")):
        # Top-level error means the request failed (not just the op)
        if r.get("error", "").startswith("HTTP ") or \
           "timed out" in r.get("error", "") or \
           "refused" in r.get("error", "").lower() or \
           "actively refused" in r.get("error", "").lower():
            return {"cad": cad, "port": info["port"],
                    "name": info["name"], "available": False,
                    "status": "down", "raw": str(r)[:120]}
    inner = r.get("result", {}) if isinstance(r.get("result"), dict) else {}
    if inner.get("ok") is True:
        return {"cad": cad, "port": info["port"],
                "name": info["name"], "available": True,
                "status": "ok", "raw": str(r)[:120]}
    # Bridge responded but op didn't fully succeed (dryrun, stub, missing creds)
    return {"cad": cad, "port": info["port"],
            "name": info["name"], "available": True,
            "status": "partial", "raw": str(r)[:120]}


def execute_plan(url: str, plan: list[dict]) -> tuple[int, int, list[str]]:
    ok = 0
    errors = []
    for i, op in enumerate(plan):
        r = post(url, op["kind"], op.get("params", {}))
        succeeded = r.get("result", {}).get("ok", False)
        if succeeded:
            ok += 1
        else:
            err = r.get("result", {}).get("error",
                  r.get("error", "unknown"))
            errors.append(f"[{i}] {op['kind']}: {err[:120]}")
    return ok, len(plan), errors


def run_for_cad(cad: str, tests: list[dict], skip_verify: bool) -> list[dict]:
    info = BRIDGES[cad]
    url = f"http://localhost:{info['port']}/op"
    out_dir = OUT / cad
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in tests:
        slug = t["slug"]
        print(f"  [{cad}] {slug}", flush=True)
        t0 = time.time()
        plan = t["build"]()
        op_ok, op_total, op_errors = execute_plan(url, plan)

        # Try to export STL for geometry check (each bridge implements saveAs
        # at its own port - format is the same: {"path": "..."}).
        stl_path = out_dir / f"{slug}.stl"
        try:
            if stl_path.exists():
                stl_path.unlink()
        except Exception:
            pass
        post(url, "saveAs", {"path": str(stl_path)})
        stl_ok = stl_path.exists()

        geom = {"overall_pass": False, "checks": [], "stats": {}}
        if stl_ok and not skip_verify:
            try:
                import trimesh
                mesh = trimesh.load(str(stl_path), force="mesh")
                if len(mesh.vertices) > 0 and mesh.volume > 0:
                    extents = tuple(float(x) for x in
                                     mesh.bounding_box.extents)
                    geom["stats"] = {
                        "bbox_mm": [round(e, 2) for e in extents],
                        "volume_mm3": round(float(mesh.volume), 2),
                        "watertight": bool(mesh.is_watertight),
                    }
                    expected = t["expected"]
                    passed = True
                    if "bbox_mm" in expected:
                        ex_sorted = sorted(expected["bbox_mm"], reverse=True)
                        ac_sorted = sorted(extents, reverse=True)
                        for a, e in zip(ac_sorted, ex_sorted):
                            tol = 0.20 * e + 1.0
                            if abs(a - e) > tol:
                                passed = False
                    if expected.get("watertight") and \
                            not mesh.is_watertight:
                        passed = False
                    geom["overall_pass"] = passed
            except Exception as ex:
                geom["checks"].append(("trimesh", False, str(ex)[:80]))

        overall = (op_ok == op_total) and (geom["overall_pass"] or
                                            not t["expected"])
        rows.append({
            "cad": cad, "slug": slug, "category": t["category"],
            "feature_keys": t["feature_keys"],
            "ops": {"ok": op_ok, "total": op_total,
                    "errors": op_errors[:3]},
            "stl": stl_ok, "geom": geom,
            "overall_pass": overall,
            "elapsed_s": round(time.time() - t0, 1),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cad", nargs="*", default=None,
                    help="Restrict to specific CAD bridges")
    ap.add_argument("--probe-only", action="store_true",
                    help="Health-check ports and exit")
    ap.add_argument("--extended", action="store_true",
                    help="Include CSWPA+CSWE tests")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    cads = args.cad or list(BRIDGES.keys())

    print("# Bridge availability")
    available = []
    for cad in cads:
        if cad not in BRIDGES:
            print(f"  unknown: {cad}")
            continue
        p = probe(cad)
        marker = p.get("status", "down")
        print(f"  {p['name']:14s} :{p['port']}  {marker:8s} {p['raw'][:60]}")
        if p["available"] and p.get("status") == "ok":
            available.append(cad)

    if args.probe_only:
        return

    if not available:
        print("\nNo bridges available. Start at least one CAD addin.")
        sys.exit(2)

    tests = list(catalog.TESTS)
    if args.extended:
        tests.extend(catalog_adv.ADVANCED_TESTS)

    print(f"\n# Running {len(tests)} tests x {len(available)} CADs")
    all_rows = []
    for cad in available:
        print(f"\n--- {cad} ---")
        rows = run_for_cad(cad, tests, args.skip_verify)
        all_rows.extend(rows)

        # Per-CAD ledger update (separate file per CAD)
        ledger_path = REPO / "outputs" / f"{cad}_learning_ledger.json"
        led = (json.loads(ledger_path.read_text(encoding="utf-8"))
               if ledger_path.exists() else {})
        for row in rows:
            for key in row["feature_keys"]:
                ledger_mod.record_result(led, key,
                    passed=row["overall_pass"],
                    error=(row["ops"]["errors"][-1]
                           if row["ops"]["errors"] else None),
                    call_path=f"test:{row['slug']}")
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(led, indent=2), encoding="utf-8")

    # Cross-CAD support matrix
    md = ["# Cross-CAD Support Matrix\n"]
    md.append(f"_Tests: {len(tests)} | CADs: {len(available)} | "
              f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}_\n")
    by_slug: dict[str, dict[str, str]] = {}
    for r in all_rows:
        by_slug.setdefault(r["slug"], {})[r["cad"]] = (
            "PASS" if r["overall_pass"] else "FAIL")
    md.append("| test | " + " | ".join(available) + " |")
    md.append("|------|" + "------|" * len(available))
    for slug, cells in sorted(by_slug.items()):
        row = f"| {slug} |"
        for cad in available:
            row += f" {cells.get(cad, '-')} |"
        md.append(row)

    (OUT / "matrix.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "results.json").write_text(json.dumps(all_rows, indent=2,
                                                  default=str),
                                        encoding="utf-8")

    n_pass = sum(1 for r in all_rows if r["overall_pass"])
    print(f"\n=== DONE: {n_pass}/{len(all_rows)} cells passed ===")
    print(f"Matrix: {OUT / 'matrix.md'}")


if __name__ == "__main__":
    main()
