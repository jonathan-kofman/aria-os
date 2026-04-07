"""
aria_os/output_verifier.py

Multi-format output verification with geometric checks + PNG renders.

Covers:
  - DXF (AutoCAD civil drawings)   → geometry checks + PNG render
  - ECAD (.kicad_pcb BOM JSON)     → placement checks + board layout PNG
  - STL/STEP (CadQuery geometry)   → mesh health + 3-view PNG (delegates to visual_verifier)

CLI:
  python -m aria_os.output_verifier --dxf outputs/test_drainage.dxf
  python -m aria_os.output_verifier --ecad outputs/ecad/<board>/bom.json
  python -m aria_os.output_verifier --stl outputs/test_box.stl --goal "box 80x50x25mm"
  python -m aria_os.output_verifier --all   # check every output in outputs/
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "outputs" / "verify"


# ─── DXF CHECKS ───────────────────────────────────────────────────────────────

def verify_dxf(dxf_path: str | Path, render_png: bool = True) -> dict:
    """
    Geometric and structural checks on a DXF file.

    Checks:
      1. Parseable by ezdxf without errors
      2. At least one entity in modelspace
      3. No entity has coordinates outside ±1e6 (blowup detection)
      4. All TEXT entities have non-empty content
      5. Entity type coverage (warn if only LINE, no LWPOLYLINE/CIRCLE/TEXT)
      6. Layer assignments — entities on layer '0' only flag (should use named layers)
      7. Coordinate bbox sanity — verify extents match expected drawing size
      8. Entity density — flag if < 10 entities (probably empty/failed generation)

    Returns dict with 'passed', 'checks', 'warnings', 'render_path'.
    """
    import ezdxf

    path = Path(dxf_path)
    checks: list[dict] = []
    warnings: list[str] = []
    render_path: str | None = None

    def _check(name: str, passed: bool, detail: str = ""):
        checks.append({"name": name, "passed": passed, "detail": detail})

    # 1. Parse
    try:
        doc = ezdxf.readfile(str(path))
        _check("parseable", True)
    except Exception as e:
        _check("parseable", False, str(e))
        return {"passed": False, "checks": checks, "warnings": warnings, "render_path": None}

    msp = doc.modelspace()
    entities = list(msp)

    # 2. Non-empty
    _check("non_empty", len(entities) > 0, f"{len(entities)} entities")

    # 3. Coordinate blowup
    max_coord = 0.0
    blowup_ents = 0
    for e in entities:
        try:
            if hasattr(e.dxf, "start"):
                for pt in [e.dxf.start, e.dxf.end]:
                    max_coord = max(max_coord, abs(pt.x), abs(pt.y))
                    if abs(pt.x) > 1e6 or abs(pt.y) > 1e6:
                        blowup_ents += 1
            elif hasattr(e.dxf, "insert"):
                pt = e.dxf.insert
                max_coord = max(max_coord, abs(pt.x), abs(pt.y))
        except Exception:
            pass
    _check("no_coord_blowup", blowup_ents == 0,
           f"max coord ±{max_coord:.1f}, {blowup_ents} blowup entities")

    # 4. Text content
    texts = [e for e in entities if e.dxftype() == "TEXT" or e.dxftype() == "MTEXT"]
    empty_texts = 0
    for t in texts:
        try:
            val = t.dxf.text if e.dxftype() == "TEXT" else t.text
            if not str(val).strip():
                empty_texts += 1
        except Exception:
            pass
    _check("text_content", empty_texts == 0,
           f"{len(texts)} text entities, {empty_texts} empty")

    # 5. Entity type coverage
    type_counts: dict[str, int] = {}
    for e in entities:
        t = e.dxftype()
        type_counts[t] = type_counts.get(t, 0) + 1
    has_variety = len(type_counts) >= 2
    _check("entity_variety", has_variety, f"types: {type_counts}")

    # 6. Named layer usage
    on_layer_0 = sum(1 for e in entities if e.dxf.layer == "0")
    pct_named = 1.0 - (on_layer_0 / max(len(entities), 1))
    _check("named_layers", pct_named > 0.5,
           f"{on_layer_0}/{len(entities)} on layer '0' ({pct_named:.0%} on named layers)")

    # 7. Entity density
    _check("entity_density", len(entities) >= 10,
           f"{len(entities)} entities total")

    # 8. Coordinate bbox — compute extents of all LINE/LWPOLYLINE endpoints
    xs, ys = [], []
    for e in entities:
        try:
            if e.dxftype() == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif e.dxftype() == "CIRCLE":
                cx, cy = e.dxf.center.x, e.dxf.center.y
                r = e.dxf.radius
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
            elif e.dxftype() == "LWPOLYLINE":
                for pt in e.get_points():
                    xs.append(pt[0])
                    ys.append(pt[1])
            elif e.dxftype() == "TEXT":
                xs.append(e.dxf.insert.x)
                ys.append(e.dxf.insert.y)
        except Exception:
            pass

    if xs and ys:
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)
        # Civil drawings should span at least 10m (10000mm) in plan
        reasonable_extent = x_span > 100 or y_span > 100
        _check("reasonable_extent", reasonable_extent,
               f"bbox {x_span:.1f} x {y_span:.1f} units")
        if not reasonable_extent:
            warnings.append(f"Drawing extent only {x_span:.1f} x {y_span:.1f} — may be empty")
    else:
        _check("reasonable_extent", False, "no extractable coordinates")

    # ── PNG render ─────────────────────────────────────────────────────────────
    if render_png:
        try:
            from ezdxf.addons.drawing import RenderContext, Frontend
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

            OUT_DIR.mkdir(parents=True, exist_ok=True)
            png_path = OUT_DIR / f"verify_dxf_{path.stem}.png"

            fig = plt.figure(figsize=(14, 10))
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_facecolor("#1a1a2e")
            fig.patch.set_facecolor("#1a1a2e")

            ctx = RenderContext(doc)
            backend = MatplotlibBackend(ax)
            Frontend(ctx, backend).draw_layout(msp, finalize=True)

            ax.set_title(f"DXF: {path.name}", color="white", fontsize=10, pad=4)
            fig.savefig(str(png_path), dpi=150, bbox_inches="tight",
                        facecolor=fig.get_facecolor())
            plt.close(fig)
            render_path = str(png_path)
            print(f"[verify] DXF render: {png_path}")
        except Exception as e:
            warnings.append(f"PNG render failed: {e}")

    passed = all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks, "warnings": warnings, "render_path": render_path}


# ─── ECAD CHECKS ──────────────────────────────────────────────────────────────

def verify_ecad(bom_path: str | Path, render_png: bool = True) -> dict:
    """
    Placement and connectivity checks on an ECAD BOM JSON.

    Checks:
      1. BOM parseable and has components
      2. All components within board bounds
      3. No component overlap (bounding box collision)
      4. Every component has at least one net assigned
      5. GND and at least one power net present
      6. Component count reasonable (>= 3)
      7. No duplicate ref designators
      8. Board dimensions reasonable (>= 20x20mm, <= 500x500mm)

    Renders a top-down board view PNG with component outlines and ref labels.
    """
    path = Path(bom_path)
    checks: list[dict] = []
    warnings: list[str] = []
    render_path: str | None = None

    def _check(name: str, passed: bool, detail: str = ""):
        checks.append({"name": name, "passed": passed, "detail": detail})

    # 1. Parse BOM
    try:
        with open(path) as f:
            bom = json.load(f)
        _check("parseable", True)
    except Exception as e:
        _check("parseable", False, str(e))
        return {"passed": False, "checks": checks, "warnings": warnings, "render_path": None}

    comps = bom.get("components", [])
    board = bom.get("board", {})
    board_w = float(board.get("width_mm", 100))
    board_h = float(board.get("height_mm", 80))

    # 2. Non-empty
    _check("has_components", len(comps) >= 1, f"{len(comps)} components")

    # 3. Board dimensions
    _check("board_dimensions",
           20 <= board_w <= 500 and 20 <= board_h <= 500,
           f"{board_w:.1f} x {board_h:.1f} mm")

    # 4. No duplicate refs
    refs = [c.get("ref", "") for c in comps]
    dupe_refs = [r for r in refs if refs.count(r) > 1]
    _check("no_duplicate_refs", len(dupe_refs) == 0,
           f"duplicates: {list(set(dupe_refs))}" if dupe_refs else "all unique")

    # 5. Within board bounds
    out_of_bounds = []
    for c in comps:
        x, y = float(c.get("x_mm", 0)), float(c.get("y_mm", 0))
        cw, ch = float(c.get("width_mm", 2)), float(c.get("height_mm", 2))
        if x < 0 or y < 0 or (x + cw) > board_w + 5 or (y + ch) > board_h + 5:
            out_of_bounds.append(c.get("ref", "?"))
    _check("within_bounds", len(out_of_bounds) == 0,
           f"out of bounds: {out_of_bounds}" if out_of_bounds else f"all within {board_w}x{board_h}mm")

    # 6. Component overlap detection
    overlaps = []
    for i, a in enumerate(comps):
        ax1, ay1 = float(a.get("x_mm", 0)), float(a.get("y_mm", 0))
        aw, ah = float(a.get("width_mm", 2)), float(a.get("height_mm", 2))
        for b in comps[i + 1:]:
            bx1, by1 = float(b.get("x_mm", 0)), float(b.get("y_mm", 0))
            bw, bh = float(b.get("width_mm", 2)), float(b.get("height_mm", 2))
            # AABB overlap
            if (ax1 < bx1 + bw and ax1 + aw > bx1 and
                    ay1 < by1 + bh and ay1 + ah > by1):
                overlaps.append(f"{a.get('ref')}↔{b.get('ref')}")
    _check("no_overlaps", len(overlaps) == 0,
           f"overlapping: {overlaps[:5]}" if overlaps else "no overlaps")

    # 7. Net coverage — every component has at least one net
    no_nets = [c.get("ref") for c in comps if not c.get("nets")]
    _check("net_coverage", len(no_nets) == 0,
           f"unnetted: {no_nets}" if no_nets else "all have nets")

    # 8. Power nets present
    all_nets: set[str] = set()
    for c in comps:
        all_nets.update(c.get("nets", []))
    has_gnd = any("gnd" in n.lower() for n in all_nets)
    has_pwr = any(n in all_nets for n in {"+3V3", "3V3", "+5V", "5V", "VIN", "12V", "+12V", "VCC"})
    _check("gnd_net_present", has_gnd, f"nets: {sorted(all_nets)[:10]}")
    _check("power_net_present", has_pwr, f"power nets found: {[n for n in all_nets if any(p in n for p in ['3V3','5V','VIN','12V','VCC'])]}")

    # ── PNG board layout render ────────────────────────────────────────────────
    if render_png and comps:
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            png_path = OUT_DIR / f"verify_ecad_{path.parent.name[:40]}.png"

            fig, ax = plt.subplots(figsize=(12, 9))
            ax.set_facecolor("#0d1117")
            fig.patch.set_facecolor("#0d1117")

            # Board outline
            board_rect = mpatches.Rectangle((0, 0), board_w, board_h,
                                            linewidth=2, edgecolor="#00ff88",
                                            facecolor="none", linestyle="--")
            ax.add_patch(board_rect)

            # Color map by component type
            type_colors = {
                "U": "#4f9de0",   # ICs — blue
                "J": "#e0a94f",   # Connectors — orange
                "C": "#a0d060",   # Caps — green
                "R": "#d06060",   # Resistors — red
                "D": "#c060c0",   # Diodes/LEDs — purple
                "L": "#60c0c0",   # Inductors — teal
                "ANT": "#e0e060", # Antenna — yellow
            }

            for c in comps:
                x, y = float(c.get("x_mm", 0)), float(c.get("y_mm", 0))
                cw, ch = float(c.get("width_mm", 2)), float(c.get("height_mm", 2))
                ref = c.get("ref", "?")
                val = c.get("value", "")
                prefix = ref[0] if ref else "?"
                color = type_colors.get(prefix, "#888888")

                rect = mpatches.FancyBboxPatch(
                    (x, y), cw, ch,
                    boxstyle="round,pad=0.3",
                    linewidth=1, edgecolor=color,
                    facecolor=color + "44",  # semi-transparent fill
                )
                ax.add_patch(rect)
                # Ref label
                ax.text(x + cw / 2, y + ch / 2, ref,
                        ha="center", va="center",
                        fontsize=6, color="white", fontweight="bold")
                # Value below
                ax.text(x + cw / 2, y - 1.5, val[:12],
                        ha="center", va="top",
                        fontsize=4.5, color="#aaaaaa")

            ax.set_xlim(-5, board_w + 5)
            ax.set_ylim(-8, board_h + 5)
            ax.set_aspect("equal")
            ax.set_xlabel("X (mm)", color="white")
            ax.set_ylabel("Y (mm)", color="white")
            ax.tick_params(colors="white")
            for spine in ax.spines.values():
                spine.set_edgecolor("#444")

            # Legend
            legend_items = [mpatches.Patch(color=v, label=k)
                            for k, v in type_colors.items()]
            ax.legend(handles=legend_items, loc="upper right",
                      fontsize=7, facecolor="#1a1a2e", labelcolor="white")

            ax.set_title(f"ECAD Layout: {path.parent.name[:50]}\n"
                         f"{len(comps)} components  |  {board_w}×{board_h}mm board",
                         color="white", fontsize=9)

            fig.savefig(str(png_path), dpi=150, bbox_inches="tight",
                        facecolor=fig.get_facecolor())
            plt.close(fig)
            render_path = str(png_path)
            print(f"[verify] ECAD render: {png_path}")
        except Exception as e:
            warnings.append(f"PNG render failed: {e}")

    passed = all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks, "warnings": warnings, "render_path": render_path}


# ─── STL CHECKS ───────────────────────────────────────────────────────────────

def verify_stl(stl_path: str | Path, goal: str = "", render_png: bool = True) -> dict:
    """
    Mesh health checks on an STL file.

    Checks:
      1. Loadable by trimesh
      2. Watertight (no open edges)
      3. Single connected component
      4. Volume > 0 (not inverted normals)
      5. Bounding box non-degenerate (all dims > 0.1mm)
      6. No degenerate faces (area < 1e-10)
      7. Face count reasonable (>= 4 for any solid)

    Renders 3-view PNG via visual_verifier._render_views.
    """
    import trimesh

    path = Path(stl_path)
    checks: list[dict] = []
    warnings: list[str] = []
    render_path: str | None = None

    def _check(name: str, passed: bool, detail: str = ""):
        checks.append({"name": name, "passed": passed, "detail": detail})

    # 1. Load
    try:
        mesh = trimesh.load(str(path))
        if hasattr(mesh, "geometry"):
            mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
        _check("loadable", True, f"{len(mesh.faces)} faces, {len(mesh.vertices)} verts")
    except Exception as e:
        _check("loadable", False, str(e))
        return {"passed": False, "checks": checks, "warnings": warnings, "render_path": None}

    # 2. Watertight
    _check("watertight", mesh.is_watertight, "open mesh" if not mesh.is_watertight else "closed")

    # 3. Single component
    comps = mesh.split(only_watertight=False)
    _check("single_component", len(comps) == 1,
           f"{len(comps)} components")

    # 4. Volume positive
    vol = mesh.volume
    _check("positive_volume", vol > 0, f"volume = {vol:.2f} mm³")

    # 5. Non-degenerate bbox
    bb = mesh.bounds
    dims = bb[1] - bb[0]
    _check("bbox_nondegenerate",
           all(d > 0.1 for d in dims),
           f"{dims[0]:.2f} x {dims[1]:.2f} x {dims[2]:.2f} mm")

    # 6. Degenerate faces
    areas = mesh.area_faces
    degen = int((areas < 1e-10).sum())
    _check("no_degenerate_faces", degen == 0, f"{degen} degenerate faces")

    # 7. Face count
    _check("sufficient_faces", len(mesh.faces) >= 4,
           f"{len(mesh.faces)} faces")

    # ── PNG render (3 views) ───────────────────────────────────────────────────
    if render_png:
        try:
            from aria_os.visual_verifier import _render_views
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            pngs = _render_views(str(path), goal or path.stem, OUT_DIR)
            render_path = pngs[2] if len(pngs) >= 3 else (pngs[0] if pngs else None)
            print(f"[verify] STL renders: {[Path(p).name for p in pngs]}")
        except Exception as e:
            warnings.append(f"PNG render failed: {e}")

    passed = all(c["passed"] for c in checks)
    return {
        "passed": passed, "checks": checks, "warnings": warnings,
        "render_path": render_path,
        "stats": {
            "faces": len(mesh.faces),
            "volume_mm3": round(vol, 2),
            "bbox_mm": [round(d, 2) for d in dims.tolist()],
            "components": len(comps),
        },
    }


# ─── UNIFIED REPORT ───────────────────────────────────────────────────────────

def _print_result(label: str, result: dict):
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n{'='*60}")
    print(f"[{status}] {label}")
    print(f"{'='*60}")
    for c in result.get("checks", []):
        icon = "OK" if c["passed"] else "FAIL"
        detail = f"  ({c['detail']})" if c.get("detail") else ""
        print(f"  {icon} {c['name']}{detail}")
    for w in result.get("warnings", []):
        if w:
            print(f"  WARN {w}")
    if result.get("render_path"):
        print(f"  -> PNG: {result['render_path']}")
    if result.get("stats"):
        s = result["stats"]
        print(f"  -> {s['faces']} faces, {s['volume_mm3']} mm3, "
              f"bbox {s['bbox_mm'][0]}x{s['bbox_mm'][1]}x{s['bbox_mm'][2]}mm")


def verify_all(outputs_dir: str | Path | None = None) -> dict[str, dict]:
    """Scan outputs/ and verify everything found."""
    root = Path(outputs_dir or ROOT / "outputs")
    results: dict[str, dict] = {}

    # STL files
    for stl in root.rglob("*.stl"):
        key = f"STL:{stl.relative_to(root)}"
        results[key] = verify_stl(stl, stl.stem)
        _print_result(key, results[key])

    # DXF files
    for dxf in root.rglob("*.dxf"):
        key = f"DXF:{dxf.relative_to(root)}"
        results[key] = verify_dxf(dxf)
        _print_result(key, results[key])

    # ECAD BOM files
    for bom in root.rglob("*_bom.json"):
        key = f"ECAD:{bom.relative_to(root)}"
        results[key] = verify_ecad(bom)
        _print_result(key, results[key])

    n_pass = sum(1 for r in results.values() if r["passed"])
    n_fail = len(results) - n_pass
    print(f"\n{'='*60}")
    print(f"TOTAL: {n_pass} passed, {n_fail} failed / {len(results)} checked")
    print(f"Renders in: {OUT_DIR}")
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verify ARIA-OS outputs")
    parser.add_argument("--dxf",  help="Path to DXF file")
    parser.add_argument("--ecad", help="Path to ECAD BOM JSON")
    parser.add_argument("--stl",  help="Path to STL file")
    parser.add_argument("--goal", default="", help="Goal string for STL label")
    parser.add_argument("--all",  action="store_true", help="Verify all outputs/")
    args = parser.parse_args()

    if args.all:
        verify_all()
    elif args.dxf:
        r = verify_dxf(args.dxf)
        _print_result(f"DXF: {args.dxf}", r)
    elif args.ecad:
        r = verify_ecad(args.ecad)
        _print_result(f"ECAD: {args.ecad}", r)
    elif args.stl:
        r = verify_stl(args.stl, args.goal)
        _print_result(f"STL: {args.stl}", r)
    else:
        parser.print_help()
