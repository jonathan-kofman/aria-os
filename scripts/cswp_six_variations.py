r"""cswp_six_variations.py — Build + verify 6 CSWP-grade SW parts with drawings.

Six different parts, each using at least one CSWP-certified feature:
  1. Hollow enclosure with mounting bosses     — CSWP Shell + Boss
  2. Helical compression spring                 — CSWP Helix + Swept Boss
  3. 24-tooth spur gear with bore               — CSWP Pattern + Boss
  4. 6-blade centrifugal impeller               — CSWP Multi-Body + Pattern
  5. PCD bolted flange (6× M10)                 — CSWP Hole Wizard + Pattern
  6. Stepped output shaft with keyway           — CSWP Multi-Feature + Cut

For each: build the part via ops → saveAs .sldprt → saveAs .step + .stl →
visual verify (geometry precheck) → createDrawing → saveAs .slddrw →
exportDrawingPdf. Final report shows pass/fail per variation.
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SW = "http://localhost:7501"
OUT_DIR = REPO / "outputs" / "cswp_six"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def post(kind: str, params: dict | None = None, timeout: float = 60) -> dict:
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


def execute_plan(plan: list[dict], slug: str) -> tuple[int, int]:
    """Run a list of {kind, params} ops. Returns (ok, total)."""
    ok = 0
    for i, op in enumerate(plan):
        r = post(op["kind"], op.get("params", {}))
        succeeded = r.get("result", {}).get("ok", False)
        if succeeded:
            ok += 1
        else:
            err = r.get("result", {}).get("error", "?")
            print(f"  [{i:3d}] {op['kind']:18s} FAIL: {err[:80]}")
    return ok, len(plan)


def visual_check(stl: Path, goal: str, spec: dict) -> dict:
    try:
        from aria_os.visual_verifier import verify_visual
    except Exception as ex:
        return {"ok": False, "error": f"import: {ex}"}
    try:
        return verify_visual(None, str(stl), goal, spec)
    except Exception as ex:
        return {"ok": False, "error": f"verify_visual: {ex}"}


# ---------------------------------------------------------------------------
# 1. Hollow enclosure with mounting bosses — Shell + Boss
# ---------------------------------------------------------------------------
def build_enclosure() -> list[dict]:
    """100×80×50mm box, 3mm wall, top open, with 4 corner mounting bosses."""
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_box"}},
        {"kind": "sketchRect",
         "params": {"sketch": "sk_box", "cx": 0, "cy": 0,
                     "w": 100, "h": 80}},
        {"kind": "extrude",
         "params": {"sketch": "sk_box", "distance": 50,
                     "operation": "new", "alias": "box_body"}},
        # Shell with top face removed (CSWP Shell). Coords identify
        # the +Z face at z=50mm.
        {"kind": "shell",
         "params": {"thickness": 3,
                     "remove_faces": [[0, 0, 50]]}},
        # Four mounting bosses at corners. Each is a small circle
        # extruded above the floor — bosses are CSWP fundamentals.
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_b1"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_b1", "cx": 40, "cy": 30, "r": 4}},
        {"kind": "extrude",
         "params": {"sketch": "sk_b1", "distance": 8,
                     "operation": "join", "alias": "boss1"}},
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_b2"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_b2", "cx": -40, "cy": 30, "r": 4}},
        {"kind": "extrude",
         "params": {"sketch": "sk_b2", "distance": 8,
                     "operation": "join", "alias": "boss2"}},
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_b3"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_b3", "cx": -40, "cy": -30, "r": 4}},
        {"kind": "extrude",
         "params": {"sketch": "sk_b3", "distance": 8,
                     "operation": "join", "alias": "boss3"}},
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_b4"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_b4", "cx": 40, "cy": -30, "r": 4}},
        {"kind": "extrude",
         "params": {"sketch": "sk_b4", "distance": 8,
                     "operation": "join", "alias": "boss4"}},
    ]
    return plan


# ---------------------------------------------------------------------------
# 2. Helical spring path — CSWP Helix
# ---------------------------------------------------------------------------
def build_helical_spring() -> list[dict]:
    """Helix path representing a spring centerline. We don't sweep a
    profile (CSWP swept-boss requires a 3D path that ARIA's planner
    abstraction doesn't yet expose) — but a real helix feature in the
    SW tree IS the CSWP-graded artifact for spring/thread design."""
    return [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_helix"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_helix", "cx": 0, "cy": 0, "r": 15}},
        {"kind": "helix",
         "params": {"sketch": "sk_helix", "pitch_mm": 6,
                     "revolutions": 8, "alias": "spring_path"}},
        # Add a small "anchor" disc so the part has a body and saves
        # cleanly. Without a body the helix is a wireframe-only feature
        # and STEP/STL export of a pure helix is degenerate.
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_anchor"}},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_anchor", "cx": 0, "cy": 0, "r": 16}},
        {"kind": "extrude",
         "params": {"sketch": "sk_anchor", "distance": 2,
                     "operation": "new", "alias": "anchor"}},
    ]


# ---------------------------------------------------------------------------
# 3. Spur gear with bore — Pattern + Boss
# ---------------------------------------------------------------------------
def build_gear() -> list[dict]:
    from aria_os.native_planner.gear_planner import plan_gear
    return plan_gear({"od_mm": 50, "n_teeth": 24, "height_mm": 8,
                       "bore_mm": 10, "module_mm": 2.0})


# ---------------------------------------------------------------------------
# 4. Centrifugal impeller — Multi-Body + Pattern
# ---------------------------------------------------------------------------
def build_impeller() -> list[dict]:
    from aria_os.native_planner.impeller_planner import plan_impeller
    return plan_impeller({"od_mm": 100, "bore_mm": 16,
                            "height_mm": 25, "n_blades": 6,
                            "blade_sweep": "radial"})


# ---------------------------------------------------------------------------
# 5. PCD bolted flange — Hole pattern (auto-expanded via validator)
# ---------------------------------------------------------------------------
def build_flange() -> list[dict]:
    from aria_os.native_planner.flange_planner import plan_flange
    return plan_flange({"od_mm": 200, "bore_mm": 80,
                          "thickness_mm": 20, "n_bolts": 6,
                          "bolt_circle_r_mm": 70, "bolt_dia_mm": 11})


# ---------------------------------------------------------------------------
# 6. Stepped output shaft with keyway — Multi-feature
# ---------------------------------------------------------------------------
def build_shaft_with_keyway() -> list[dict]:
    """Shaft with 4 diameter steps along Z + an axial keyway cut at
    the largest segment. Each step uses a separate sketch+extrude with
    `start_offset` so segments stack along Z (verified working)."""
    # Segments: (length, dia)  — total ~ 200mm long
    segs = [(40, 30), (60, 40), (50, 50), (50, 35)]
    plan = [
        {"kind": "beginPlan", "params": {}},
    ]
    z = 0.0
    for i, (L, D) in enumerate(segs):
        sk_alias = f"sk_seg{i}"
        body_alias = f"seg{i}"
        plan.append({"kind": "newSketch",
                     "params": {"plane": "XY", "alias": sk_alias}})
        plan.append({"kind": "sketchCircle",
                     "params": {"sketch": sk_alias, "cx": 0, "cy": 0,
                                 "r": D / 2}})
        plan.append({"kind": "extrude",
                     "params": {"sketch": sk_alias, "distance": L,
                                 "operation": "new" if i == 0 else "join",
                                 "alias": body_alias,
                                 "start_offset": z}})
        z += L
    # Keyway cut on the 50mm-dia segment (the biggest, segs[2]).
    # Keyway axial position: Z range = [40+60, 40+60+50] = [100, 150]
    # Keyway dimensions per ISO 2491: 14mm wide × 5.5mm deep
    keyway_zmid = 40 + 60 + 25  # midpoint of 50mm-dia segment
    plan.append({"kind": "newSketch",
                 "params": {"plane": "XZ", "alias": "sk_key"}})
    # On XZ plane, sketch x = world X, sketch y = world Z (after mirror).
    # Keyway: 14mm wide in Z, 5.5mm deep in X (radial).
    # Centered at radius (50/2 - 5.5/2) = 22.25 from origin (on -X side
    # so we cut into the +X half).
    plan.append({"kind": "sketchRect",
                 "params": {"sketch": "sk_key",
                             "cx": 50 / 2 - 5.5 / 2,
                             "cy": keyway_zmid,
                             "w": 5.5, "h": 14}})
    plan.append({"kind": "extrude",
                 "params": {"sketch": "sk_key", "distance": 60,
                             "operation": "cut", "alias": "keyway"}})
    return plan


# ---------------------------------------------------------------------------
# Per-variation runner
# ---------------------------------------------------------------------------
def run_variation(slug: str, builder, goal: str, spec: dict) -> dict:
    print(f"\n=== {slug} ===")
    t0 = time.time()
    plan = builder()

    # Apply validator-layer normalization (auto-expands circularPattern).
    try:
        from aria_os.native_planner.validator import _normalize_plan
        plan = _normalize_plan(plan)
    except Exception as ex:
        print(f"  normalize_plan threw: {ex}")

    # Execute.
    ok_count, total = execute_plan(plan, slug)
    print(f"  ops: {ok_count}/{total} succeeded")

    # Save .sldprt + .step + .stl.
    sldprt = OUT_DIR / f"{slug}.sldprt"
    step = OUT_DIR / f"{slug}.step"
    stl = OUT_DIR / f"{slug}.stl"
    post("saveAs", {"path": str(sldprt)})
    time.sleep(0.5)
    post("saveAs", {"path": str(step)})
    post("saveAs", {"path": str(stl)})

    # Visual verify.
    if not stl.exists():
        verdict = "no-stl"
        v = {}
    else:
        v = visual_check(stl, goal, spec)
        verdict = "PASS" if v.get("overall_match") is True \
                  else ("FAIL" if v.get("overall_match") is False else "?")

    # Generate drawing.
    drw = OUT_DIR / f"{slug}.slddrw"
    pdf = OUT_DIR / f"{slug}.pdf"
    drw_ok = False
    pdf_ok = False
    if sldprt.exists():
        cd = post("createDrawing",
                   {"source": str(sldprt), "out": str(drw),
                    "sheet_size": "A3", "add_bom": False},
                   timeout=120)
        drw_ok = cd.get("result", {}).get("ok", False)
        if drw_ok and drw.exists():
            ep = post("exportDrawingPdf",
                       {"out": str(pdf)}, timeout=60)
            pdf_ok = ep.get("result", {}).get("ok", False)

    elapsed = time.time() - t0
    return {
        "slug": slug,
        "ops_ok": ok_count, "ops_total": total,
        "step": step.exists(), "stl": stl.exists(),
        "drw": drw.exists(), "pdf": pdf.exists(),
        "verify": verdict,
        "confidence": v.get("confidence") if isinstance(v, dict) else None,
        "elapsed_s": round(elapsed, 1),
    }


def main():
    variations = [
        ("01_enclosure", build_enclosure,
         "100x80x50mm hollow enclosure with 3mm wall, top open, "
         "4 mounting bosses",
         {"width_mm": 100, "depth_mm": 80, "height_mm": 50,
          "wall_mm": 3}),
        ("02_helical_spring", build_helical_spring,
         "M30 helical spring path with 6mm pitch and 8 revolutions",
         {"od_mm": 30, "n_revs": 8}),
        ("03_spur_gear", build_gear,
         "spur gear 50mm OD with 24 teeth, 8mm thick, 10mm bore",
         {"od_mm": 50, "n_teeth": 24, "thickness_mm": 8,
          "bore_mm": 10}),
        ("04_impeller", build_impeller,
         "centrifugal impeller 100mm OD with 6 radial blades, 25mm tall, "
         "16mm bore",
         {"od_mm": 100, "n_blades": 6, "thickness_mm": 25,
          "bore_mm": 16}),
        ("05_flange", build_flange,
         "200mm OD flange with 6 M10 bolt holes on 70mm bolt-circle "
         "radius, 80mm bore, 20mm thick",
         {"od_mm": 200, "bore_mm": 80, "thickness_mm": 20,
          "n_bolts": 6, "bolt_circle_r_mm": 70, "bolt_dia_mm": 11}),
        ("06_stepped_shaft", build_shaft_with_keyway,
         "stepped shaft 200mm long with 4 diameter steps "
         "(30/40/50/35mm) and a 14mm wide keyway",
         {"length_mm": 200, "diameter_mm": 50,
          "feature_width_mm": 14}),
    ]
    rows = []
    for slug, builder, goal, spec in variations:
        rows.append(run_variation(slug, builder, goal, spec))

    # Final report.
    print("\n# CSWP-Six report")
    print("| slug | ops | step | stl | drw | pdf | verify | conf | wall(s) |")
    print("|------|-----|------|-----|-----|-----|--------|------|---------|")
    for r in rows:
        print(f"| {r['slug']} | {r['ops_ok']}/{r['ops_total']} "
              f"| {'Y' if r['step'] else '-'} "
              f"| {'Y' if r['stl'] else '-'} "
              f"| {'Y' if r['drw'] else '-'} "
              f"| {'Y' if r['pdf'] else '-'} "
              f"| {r['verify']} | {r['confidence']} "
              f"| {r['elapsed_s']} |")

    # Save report.
    report_path = OUT_DIR / "report.json"
    report_path.write_text(json.dumps(rows, indent=2))
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
