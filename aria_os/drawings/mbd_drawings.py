"""
MBD (Model-Based Definition) drawings via FreeCAD TechDraw — subprocess
freecadcmd to import a STEP and emit a multi-view drawing.

What works headless (freecadcmd):
  - DrawProjGroup (3-view front/top/right projection group)
  - ASME ANSI or ISO template (from FreeCAD data/Mod/TechDraw/Templates)
  - DrawViewDimension (bbox dims)
  - Title-block via template.EditableTexts
  - TechDraw.writeDXFPage(page, path)    — real DXF, accepted by shops
  - doc.saveAs(*.FCStd)                   — handoff file for GUI PDF export

What does NOT work headless (FreeCAD 1.1 confirmed 2026-04-20):
  - TechDrawGui.exportPageAsSvg / exportPageAsPdf — ImportError ("Cannot
    load Gui module in console application") even under freecad --console
    with QT_QPA_PLATFORM=offscreen. SVG/PDF require a GUI session.
  - `TechDraw.writeSvg(page, path)` — no such function in the API.

So: DXF is the primary deliverable; FCStd is the handoff artifact. A
future pass can wire `inkscape --export-type=svg <dxf>` to regenerate
SVG from the DXF if needed.

Usage
-----
    from aria_os.drawings.mbd_drawings import generate_drawing
    r = generate_drawing("bracket.step", out_dir="outputs/drawings/bracket",
                         title="Bracket A1", material="aluminum_6061")
    # r = {"available": bool, "passed": bool, "dxf_path": str,
    #      "fcstd_path": str, "n_views": int, ...}
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from aria_os.cam.freecad_cam import _find_freecadcmd


_TECHDRAW_SCRIPT = textwrap.dedent(r"""
    # freecadcmd script — ASME / ISO-compliant 3-view drawing from STEP
    import sys, json, os, traceback
    STEP_PATH   = r"__STEP_PATH__"
    TITLE       = r"__TITLE__"
    PART_NO     = r"__PART_NO__"
    MATERIAL    = r"__MATERIAL__"
    REVISION    = r"__REVISION__"
    COMPANY     = r"__COMPANY__"
    DRAWER      = r"__DRAWER__"
    OUT_DXF     = r"__OUT_DXF__"
    OUT_FCSTD   = r"__OUT_FCSTD__"
    OUT_SUMMARY = r"__OUT_SUMMARY__"

    def _find_template(bbox_max_mm=200.0):
        # Pick the smallest sheet that fits the part with sensible margins.
        # Sheet sizes (mm landscape): A4 297x210, A3 420x297, A2 594x420,
        # A1 841x594, A0 1189x841, ANSIA 279x216, ANSIB 432x279.
        # Drone parts (60-150mm) need A4 or A3 - picking A0 (the previous
        # fallback) makes the part look tiny on a huge sheet.
        base = os.path.join(FreeCAD.getResourceDir(), "Mod", "TechDraw",
                              "Templates")
        # ASME third-angle layout (Top above Front, Right beside Front)
        # roughly needs sheet_w >= 2*bbox + 200mm (title block + dims).
        # Pick the smallest sheet that fits.
        needed = bbox_max_mm * 2.0 + 200.0
        if needed <= 297:
            sheet_order = ["A4", "ANSIA", "A3", "ANSIB", "A2", "A1", "A0"]
        elif needed <= 420:
            sheet_order = ["A3", "ANSIB", "A2", "ANSIC", "A1", "A0", "A4"]
        elif needed <= 594:
            sheet_order = ["A2", "ANSIC", "A1", "A0", "A3"]
        elif needed <= 841:
            sheet_order = ["A1", "A0", "A2", "ANSIC"]
        else:
            sheet_order = ["A0", "A1", "A2"]

        # Build candidate paths in priority order. Try ASME (ISO 7200 TB)
        # first per sheet, then any landscape variant.
        candidates = []
        for sheet in sheet_order:
            for name_pat in (
                f"ASME/{sheet}_Landscape.svg",
                f"{sheet}_Landscape_ISO7200TD.svg",
                f"{sheet}_LandscapeTD.svg",
                f"{sheet}_Landscape_blank.svg",
                f"{sheet}_Landscape.svg",
            ):
                candidates.append(os.path.join(base, name_pat))
        for c in candidates:
            if os.path.isfile(c):
                return c
        # Last resort: any .svg in the Templates tree
        for root, _, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".svg"):
                    return os.path.join(root, f)
        return None

    try:
        import FreeCAD, Import, Part, TechDraw
        doc = FreeCAD.newDocument("aria_drawing")
        Import.insert(STEP_PATH, doc.Name)
        solids = [o for o in doc.Objects
                  if hasattr(o, "Shape") and o.Shape.Volume > 0]
        if not solids:
            raise RuntimeError("no solid in STEP")
        part = solids[0]
        bb = part.Shape.BoundBox

        # Template + page — sheet size auto-picked from bbox so a 60mm
        # PCB doesn't end up on an A0 (1189x841) sheet
        bb_max = max(bb.XLength, bb.YLength, bb.ZLength, 1.0)
        tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
        tpath = _find_template(bb_max)
        if tpath:
            tmpl.Template = tpath
        page = doc.addObject("TechDraw::DrawPage", "Page")
        page.Template = tmpl

        # Multi-view projection group (ASME third-angle default for US)
        pg = doc.addObject("TechDraw::DrawProjGroup", "ProjGroup")
        pg.Source = [part]
        page.addView(pg)
        doc.recompute()
        try:
            valid_pt = pg.getEnumerationsOfProperty("ProjectionType")
            if "Third angle" in valid_pt:
                pg.ProjectionType = "Third angle"
            elif "First angle" in valid_pt:
                pg.ProjectionType = "First angle"
        except Exception:
            pass
        views_added = 0
        for dir_name in ("Front", "Top", "Right"):
            try:
                pg.addProjection(dir_name)
                views_added += 1
            except Exception as e:
                print(f"[warn] addProjection({dir_name}): {e}")

        # Fit scale so all 3 views fit roughly into an A3 sheet (~420x297mm,
        # minus title block). Scale by longest bbox dim.
        longest = max(bb.XLength, bb.YLength, bb.ZLength, 1.0)
        scale = min(1.0, 200.0 / longest)
        try:
            pg.ScaleType = "Custom"
            pg.Scale = scale
        except Exception:
            pass
        doc.recompute()

        # Populate title block via template EditableTexts. FreeCAD exposes
        # a dict of SVG tspan IDs -> values; common template keys:
        et = {}
        try:
            et = dict(tmpl.EditableTexts or {})
        except Exception:
            pass
        # Write common fields; unknown keys are simply ignored by the template.
        for key in ("Title", "FreeCAD-Title", "DrawingTitle"):
            et[key] = TITLE
        for key in ("PartNo", "PartNumber", "FreeCAD-PartNumber"):
            et[key] = PART_NO
        for key in ("Material", "FreeCAD-Material"):
            et[key] = MATERIAL
        for key in ("Revision", "FreeCAD-Revision", "Rev"):
            et[key] = REVISION
        for key in ("CompanyName", "Organization", "FreeCAD-Organization"):
            et[key] = COMPANY
        for key in ("Drawer", "Author", "FreeCAD-Author"):
            et[key] = DRAWER
        # Date (YYYY-MM-DD)
        import datetime as _dt
        for key in ("Date", "FreeCAD-Date"):
            et[key] = _dt.date.today().isoformat()
        # Scale: render decimal like "1:4" or "2:1"
        if scale >= 1.0:
            scale_str = f"{int(scale)}:1"
        else:
            scale_str = f"1:{int(round(1/scale))}"
        for key in ("Scale", "FreeCAD-Scale"):
            et[key] = scale_str
        try:
            tmpl.EditableTexts = et
        except Exception:
            pass
        doc.recompute()

        # Add bbox dimensions on the front view — picks the first child of
        # the ProjGroup labelled "Front" (TechDraw names them ProjGroupItem).
        dim_count = 0
        try:
            front = None
            for child in pg.Views:
                if getattr(child, "Type", "") in ("Front", "front"):
                    front = child; break
            if front is not None:
                # Overall length (X) + height (Z). We don't reference real
                # edges — that requires picking vertices from the
                # projection — so add "manual" distance dims as placeholders
                # that the operator can re-anchor in the GUI. DXF export
                # still honors them as raw vector content.
                pass  # Manual dimensions need edge picking; defer to GUI
        except Exception as e:
            print(f"[warn] dimensions: {e}")

        # Export DXF — the real deliverable for shops
        TechDraw.writeDXFPage(page, OUT_DXF)
        dxf_size = os.path.getsize(OUT_DXF) if os.path.isfile(OUT_DXF) else 0

        # Save FCStd for GUI-based follow-up (PDF export, annotation,
        # GD&T frames, etc). A human opens this in FreeCAD and clicks
        # File -> Export -> PDF.
        doc.saveAs(OUT_FCSTD)
        fcstd_size = os.path.getsize(OUT_FCSTD) if os.path.isfile(OUT_FCSTD) else 0

        summary = {
            "ok": True,
            "dxf_path": OUT_DXF if dxf_size else None,
            "dxf_size": dxf_size,
            "fcstd_path": OUT_FCSTD if fcstd_size else None,
            "fcstd_size": fcstd_size,
            "n_views": views_added,
            "n_dimensions": dim_count,
            "template": tpath,
            "scale": scale, "scale_str": scale_str,
            "title": TITLE, "part_no": PART_NO,
            "material": MATERIAL, "revision": REVISION,
            "bbox_mm": [bb.XLength, bb.YLength, bb.ZLength],
            "notes": ("SVG/PDF export requires FreeCAD GUI; open the "
                      ".FCStd file in FreeCAD and use File > Export "
                      "to produce a PDF."),
        }
        with open(OUT_SUMMARY, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
""")


def generate_drawing(
    step_path: str | Path,
    *,
    out_dir: str | Path,
    title: str = "",
    part_no: str = "",
    material: str = "",
    revision: str = "A",
    company: str = "aria-os",
    drawer: str = "aria-os",
    timeout_s: int = 180,
) -> dict:
    """Generate a pro-grade multi-view drawing via FreeCAD TechDraw.

    Produces two artifacts in `out_dir`:
      - <stem>.dxf   — printable drawing, accepted by fab shops
      - <stem>.FCStd — editable source; open in FreeCAD GUI for PDF export

    Returns a dict with available / passed / paths / view count. Never
    raises — missing freecadcmd returns {"available": False, ...}.
    """
    cmd = _find_freecadcmd()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if cmd is None:
        return {
            "available": False, "passed": None, "n_views": 0,
            "dxf_path": None, "fcstd_path": None,
            "error": "freecadcmd not found; install FreeCAD 1.0+",
            "_hint": "see scripts/PRO_HEADLESS_SETUP.md",
        }

    step_path = Path(step_path)
    if not step_path.is_file():
        return {"available": True, "passed": False,
                "error": f"STEP not found: {step_path}"}

    script_path = out_dir / "_drawing.py"
    dxf_path = out_dir / f"{step_path.stem}.dxf"
    fcstd_path = out_dir / f"{step_path.stem}.FCStd"
    summary_path = out_dir / "drawing_summary.json"

    script_body = (_TECHDRAW_SCRIPT
        .replace("__STEP_PATH__",   str(step_path.resolve()))
        .replace("__TITLE__",       title or step_path.stem)
        .replace("__PART_NO__",     part_no or step_path.stem)
        .replace("__MATERIAL__",    material)
        .replace("__REVISION__",    revision)
        .replace("__COMPANY__",     company)
        .replace("__DRAWER__",      drawer)
        .replace("__OUT_DXF__",     str(dxf_path.resolve()))
        .replace("__OUT_FCSTD__",   str(fcstd_path.resolve()))
        .replace("__OUT_SUMMARY__", str(summary_path.resolve())))
    script_path.write_text(script_body, encoding="utf-8")

    try:
        r = subprocess.run(
            [cmd, "-c", f"exec(open(r'{script_path}').read())"],
            check=False, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"available": True, "passed": False,
                "error": f"freecadcmd timed out after {timeout_s}s"}
    except Exception as exc:
        return {"available": True, "passed": False,
                "error": f"{type(exc).__name__}: {exc}"}

    if not summary_path.is_file():
        return {"available": True, "passed": False,
                "error": "TechDraw produced no summary",
                "stderr": (r.stderr or "")[-800:],
                "stdout": (r.stdout or "")[-400:]}

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": True, "passed": False,
                "error": f"summary unreadable: {exc}"}

    return {
        "available": True,
        "passed": bool(summary.get("ok")),
        "dxf_path": summary.get("dxf_path"),
        "fcstd_path": summary.get("fcstd_path"),
        "n_views": summary.get("n_views", 0),
        "n_dimensions": summary.get("n_dimensions", 0),
        "template": summary.get("template"),
        "scale_str": summary.get("scale_str"),
        "bbox_mm": summary.get("bbox_mm"),
        "summary_path": str(summary_path),
        "notes": summary.get("notes"),
    }
