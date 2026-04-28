"""End-to-end test of the scan-to-CAD + image-to-CAD + ECAD pipelines.

Thingiverse blocks unauthenticated downloads, so we generate three
representative "real-world-style" STLs locally that exercise the
same code paths Thingiverse-sourced meshes would:

  1. iot_enclosure   - rectangular box with cutout (USB-C slot) +
                       mounting bosses. Mimics ESP32 dev-board housings.
  2. printer_bracket - L-bracket with multiple bolt patterns. Mimics
                       3D-printer corner-bracket category.
  3. control_knob    - knurled cylinder with shaft hole. Mimics
                       potentiometer / encoder knob category.

For each part:
  A. Scan-to-CAD:  STL → run_scan_pipeline → cleaned STL + features
  B. Image-to-CAD: render STL to PNG → analyze_image_for_cad → goal
                   text → orchestrator → STEP
  C. Electrical:   compose a goal "PCB to fit inside <part>" →
                   ECAD pipeline (KiCad) → .kicad_pcb + Gerbers

Outputs to outputs/thingiverse_test/<part>/. All renders saved as
PNGs so visual verification is just `ls *.png`.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
OUT_DIR = REPO_ROOT / "outputs" / "thingiverse_test"


# --------------------------------------------------------------------- #
# Synthetic Thingiverse-style STLs.
# --------------------------------------------------------------------- #

PARTS: dict[str, dict] = {
    "iot_enclosure": {
        "description": "IoT device enclosure with USB-C slot + mounting bosses",
        "electrical_goal":
            "ESP32-S3 dev board with USB-C, 60x40mm 2-layer PCB, "
            "2 status LEDs, indoor use, prototype quantity",
    },
    "printer_bracket": {
        "description": "3D printer corner bracket, L-shape, "
                          "M3 + M5 mounting holes, aluminium",
        "electrical_goal":
            "limit switch breakout PCB, 25x15mm 2-layer, JST-XH connector, "
            "single mechanical switch input, indoor use",
    },
    "control_knob": {
        "description": "knurled control knob with set-screw, 30mm OD, "
                          "6mm shaft hole",
        "electrical_goal":
            "rotary encoder breakout PCB, 30x30mm 2-layer, "
            "RGB indicator LED, push-button center, indoor use",
    },
}


def _build_iot_enclosure(out_path: Path) -> bool:
    """Box 80x60x30mm with USB-C slot + 4 mounting bosses inside.
    Approximates an ESP32-S3 enclosure (Adafruit / Seeed style)."""
    try:
        import cadquery as cq
    except ImportError:
        return False
    OD_X, OD_Y, OD_Z = 80.0, 60.0, 30.0
    WALL = 2.0
    USB_W, USB_H = 9.5, 4.0
    body = (cq.Workplane("XY")
              .box(OD_X, OD_Y, OD_Z)
              .faces(">Z").shell(-WALL))
    body = (body.faces("<Y").workplane(centerOption="CenterOfMass")
              .center(0, -2)
              .rect(USB_W, USB_H).cutThruAll())
    bosses = (cq.Workplane("XY")
                .rect(OD_X - 12, OD_Y - 12, forConstruction=True)
                .vertices()
                .circle(4.0).extrude(8.0))
    bosses = bosses.translate((0, 0, -OD_Z / 2 + WALL))
    holes = (cq.Workplane("XY")
              .rect(OD_X - 12, OD_Y - 12, forConstruction=True)
              .vertices()
              .circle(1.5).extrude(8.0))
    holes = holes.translate((0, 0, -OD_Z / 2 + WALL))
    result = body.union(bosses).cut(holes)
    cq.exporters.export(result, str(out_path), "STL")
    return out_path.is_file() and out_path.stat().st_size > 0


def _build_printer_bracket(out_path: Path) -> bool:
    """L-bracket 60x40x40mm with 2x M5 + 4x M3 holes. Mimics
    a typical 3D-printer-frame corner brace."""
    try:
        import cadquery as cq
    except ImportError:
        return False
    W, H, D = 60.0, 40.0, 40.0
    T = 5.0
    base = (cq.Workplane("XY").box(W, D, T)
              .faces(">Z").workplane()
              .pushPoints([(-W/2 + 10, 0), (W/2 - 10, 0)])
              .circle(2.5).cutThruAll())
    leg = (cq.Workplane("YZ").box(D, H, T)
              .translate((-W/2 + T/2, 0, H/2 - T/2))
              .faces(">X").workplane()
              .pushPoints([(0, H/2 - 10), (0, -H/2 + 10),
                            (D/2 - 8, 0), (-D/2 + 8, 0)])
              .circle(1.5).cutThruAll())
    result = base.union(leg)
    cq.exporters.export(result, str(out_path), "STL")
    return out_path.is_file() and out_path.stat().st_size > 0


def _build_control_knob(out_path: Path) -> bool:
    """Cylinder 30mm OD x 20mm tall with 6mm shaft hole + 12 cosmetic
    grip pockets. Mimics a potentiometer / encoder knob."""
    try:
        import cadquery as cq
        import math
    except ImportError:
        return False
    OD, HT, BORE = 30.0, 20.0, 6.0
    body = cq.Workplane("XY").circle(OD / 2).extrude(HT)
    body = body.faces(">Z").workplane().circle(BORE / 2).cutThruAll()
    # 12 grip pockets around the perimeter — uses individual cuts so
    # we don't trip cadquery's coplanar-face requirement.
    pockets = cq.Workplane("XY")
    for i in range(12):
        ang = 2 * math.pi * i / 12
        cx = (OD / 2 - 1.0) * math.cos(ang)
        cy = (OD / 2 - 1.0) * math.sin(ang)
        pockets = pockets.union(
            cq.Workplane("XY").center(cx, cy)
              .circle(1.0).extrude(HT))
    body = body.cut(pockets)
    cq.exporters.export(body, str(out_path), "STL")
    return out_path.is_file() and out_path.stat().st_size > 0


def _generate_test_meshes(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    builders = {
        "iot_enclosure":   _build_iot_enclosure,
        "printer_bracket": _build_printer_bracket,
        "control_knob":    _build_control_knob,
    }
    paths = {}
    for name, fn in builders.items():
        out = out_dir / f"{name}.stl"
        try:
            ok = fn(out)
            if ok:
                paths[name] = out
                print(f"[mesh] {name}: {out.stat().st_size} bytes")
            else:
                print(f"[mesh] {name}: build returned False")
        except Exception as exc:
            print(f"[mesh] {name}: {type(exc).__name__}: {exc}")
    return paths


# --------------------------------------------------------------------- #
# Renderers.
# --------------------------------------------------------------------- #

def render_to_png(geom_path: Path, out_png: Path,
                    title: str = "") -> bool:
    """Render an STL or STEP to a 3D PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import trimesh
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    except ImportError as exc:
        print(f"  [render] missing dep: {exc}")
        return False
    try:
        if geom_path.suffix.lower() in (".step", ".stp"):
            import cadquery as cq, tempfile
            shp = cq.importers.importStep(str(geom_path))
            with tempfile.NamedTemporaryFile(suffix=".stl",
                                                delete=False) as t:
                stl_path = t.name
            cq.exporters.export(shp, stl_path, "STL")
            m = trimesh.load(stl_path, force="mesh")
        else:
            m = trimesh.load(str(geom_path), force="mesh")
    except Exception as exc:
        print(f"  [render] load failed: {exc}")
        return False
    try:
        fig = plt.figure(figsize=(7, 7))
        ax = fig.add_subplot(111, projection="3d")
        v = m.vertices
        if len(m.faces) == 0:
            print("  [render] empty mesh"); return False
        ax.plot_trisurf(v[:, 0], v[:, 1], v[:, 2],
                          triangles=m.faces, alpha=0.7,
                          edgecolor="steelblue", linewidth=0.05)
        ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)")
        ax.set_zlabel("Z (mm)")
        if title: ax.set_title(title, fontsize=10)
        ax.view_init(elev=22, azim=42)
        plt.tight_layout()
        plt.savefig(str(out_png), dpi=120, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception as exc:
        print(f"  [render] savefig: {exc}")
        return False


# --------------------------------------------------------------------- #
# A. Scan-to-CAD.
# --------------------------------------------------------------------- #

def test_scan_to_cad(name: str, stl_path: Path, out_dir: Path
                       ) -> dict:
    """Run STL through run_scan_pipeline; record cleaned STL + features."""
    rec = {"name": name, "ok": False, "stage": "init", "duration_s": 0.0}
    t0 = time.time()
    try:
        from aria_os.scan_pipeline import run_scan_pipeline
        rec["stage"] = "running_pipeline"
        entry = run_scan_pipeline(stl_path,
                                    output_dir=out_dir,
                                    material="ABS")
        rec["ok"] = True
        rec["stage"] = "complete"
        rec["topology"] = getattr(entry, "topology", None)
        rec["confidence"] = getattr(entry, "confidence", 0.0)
        rec["bbox"] = getattr(entry, "bounding_box", None)
        rec["volume_mm3"] = getattr(entry, "volume_mm3", None)
        rec["primitives"] = getattr(entry, "primitives_summary", None)
        rec["stl_path"] = getattr(entry, "stl_path", None)
        rec["features_path"] = getattr(entry, "features_path", None)
        # Render the cleaned STL.
        cleaned = Path(rec.get("stl_path") or stl_path)
        png = out_dir / f"{name}_scan_clean.png"
        if cleaned.is_file():
            if render_to_png(cleaned, png,
                              title=f"{name} (cleaned mesh from scan_pipeline)"):
                rec["render_png"] = str(png)
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc(limit=3)
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


# --------------------------------------------------------------------- #
# B. Image-to-CAD.
# --------------------------------------------------------------------- #

def test_image_to_cad(name: str, stl_path: Path, out_dir: Path
                        ) -> dict:
    """Render STL → PNG → analyze_image_for_cad → goal text → see what
    the vision pipeline extracts. Skips the full orchestrator run
    here (that's a 30-90s pipeline) — we just verify the vision +
    spec-extraction stage produces a sensible goal."""
    rec = {"name": name, "ok": False, "stage": "init", "duration_s": 0.0}
    t0 = time.time()
    try:
        # 1. Render STL to a "real photo" stand-in.
        png_in = out_dir / f"{name}_input_render.png"
        rec["stage"] = "rendering"
        if not render_to_png(stl_path, png_in,
                                title=f"{name} (input photo)"):
            rec["error"] = "input render failed"
            return rec
        rec["input_image"] = str(png_in)
        # 2. Vision analysis. analyze_image_for_cad returns a string
        # goal (or None) — there is no separate features dict on this
        # entry point. Surface what we got verbatim.
        rec["stage"] = "vision"
        from aria_os.llm_client import analyze_image_for_cad
        goal = analyze_image_for_cad(
            str(png_in), hint="", repo_root=REPO_ROOT)
        rec["goal_extracted"] = goal
        rec["ok"] = bool(goal)
        rec["stage"] = "complete"
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


# --------------------------------------------------------------------- #
# C. ECAD: KiCad PCB to match.
# --------------------------------------------------------------------- #

def test_ecad_for_part(name: str, electrical_goal: str, out_dir: Path
                         ) -> dict:
    """Run the ECAD pipeline (KiCad) for the part's electrical aspect."""
    rec = {"name": name, "ok": False, "stage": "init",
            "duration_s": 0.0}  # init early so error paths don't crash caller
    t0 = time.time()
    try:
        # Try the high-level text-to-board pipeline if present.
        rec["stage"] = "running_ecad"
        try:
            from aria_os.ecad.pipeline import run_text_to_board  # type: ignore
        except ImportError:
            try:
                from aria_os.ecad.kicad_pipeline import run_text_to_board  # type: ignore
            except ImportError:
                run_text_to_board = None
        if run_text_to_board is None:
            # Fallback: hit the running aria_server's /api/ecad/text-to-board.
            import urllib.request as _ureq
            try:
                req = _ureq.Request(
                    "http://localhost:8000/api/ecad/text-to-board",
                    data=json.dumps({"goal": electrical_goal}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with _ureq.urlopen(req, timeout=300) as r:
                    rec["result"] = json.loads(r.read().decode("utf-8"))
                rec["ok"] = bool(rec["result"].get("ok"))
                rec["stage"] = "complete"
                return rec
            except Exception as exc:
                rec["error"] = (f"ecad fallback HTTP failed and no in-proc "
                                  f"path available: {exc}")
                return rec
        out = run_text_to_board(electrical_goal, output_dir=out_dir)
        rec["result"] = out
        rec["ok"] = True
        rec["stage"] = "complete"
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc(limit=3)
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


# --------------------------------------------------------------------- #
# Driver.
# --------------------------------------------------------------------- #

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Generate test STLs.
    print("[step 1] generating Thingiverse-style STLs")
    meshes = _generate_test_meshes(OUT_DIR)
    if not meshes:
        print("[error] no meshes generated — cadquery likely missing")
        return 2

    summary: list[dict] = []
    for name, stl in meshes.items():
        print(f"\n=== {name} ============================================")
        part_dir = OUT_DIR / name
        part_dir.mkdir(parents=True, exist_ok=True)

        # Always render the input STL too.
        in_png = part_dir / f"{name}_input.png"
        render_to_png(stl, in_png, title=f"{name} (synthetic source)")

        # A. Scan-to-CAD
        print(f"  [A] scan-to-CAD")
        scan_rec = test_scan_to_cad(name, stl, part_dir)
        print(f"      ok={scan_rec['ok']} stage={scan_rec['stage']} "
              f"({scan_rec['duration_s']}s)"
              + (f"  err: {scan_rec.get('error', '')[:80]}"
                  if not scan_rec['ok'] else ""))

        # B. Image-to-CAD
        print(f"  [B] image-to-CAD (vision only)")
        img_rec = test_image_to_cad(name, stl, part_dir)
        print(f"      ok={img_rec['ok']} stage={img_rec['stage']} "
              f"({img_rec['duration_s']}s)"
              + (f"  err: {img_rec.get('error', '')[:80]}"
                  if not img_rec['ok'] else ""))
        if img_rec.get('goal_extracted'):
            print(f"      goal: {img_rec['goal_extracted'][:100]}")

        # C. ECAD
        electrical = PARTS[name]["electrical_goal"]
        print(f"  [C] ecad: {electrical[:80]}")
        ecad_rec = test_ecad_for_part(name, electrical, part_dir)
        print(f"      ok={ecad_rec['ok']} stage={ecad_rec['stage']} "
              f"({ecad_rec['duration_s']}s)"
              + (f"  err: {ecad_rec.get('error', '')[:80]}"
                  if not ecad_rec['ok'] else ""))

        summary.append({
            "name":       name,
            "input_stl":  str(stl),
            "scan":       scan_rec,
            "image":      img_rec,
            "ecad":       ecad_rec,
        })

    out_json = OUT_DIR / "summary.json"
    out_json.write_text(json.dumps(summary, indent=2, default=str), "utf-8")
    print(f"\n[done] summary at {out_json}")
    print(f"[done] PNGs in {OUT_DIR}/<part>/*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
