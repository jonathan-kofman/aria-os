"""Auto-loop visual-verify gate for SW drawings (rec #7).

Closes the loop after `/op enrichDrawing` lands on a .slddrw: dump it
to PDF via the SW addin, render to PNG, hand the PNG to Claude vision
with a checklist (datum letters present, FCFs present, section A-A
visible, exploded callout visible). If the checklist fails, the
caller can re-run enrichDrawing once with stronger hints.

Per the autonomy-first rule (memory: feedback_autonomy_first.md),
this module's `verify_and_recover()` runs the gate AND triggers the
recovery hop — orchestrator just calls it, gets a verified=True/False
back. No "tell the user how to debug" path.

Public surface:
    verify_drawing(slddrw_path, expected, port=7501) -> dict
    verify_and_recover(slddrw_path, expected, retry_params, port=7501)
                                                                  -> dict
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _post(base: str, path: str, payload: dict, timeout: float = 60.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _slddrw_to_pdf(slddrw: Path, base: str, timeout: float = 90.0) -> Path | None:
    """Drive SW addin's exportDrawingPdf op. Returns PDF path or None."""
    pdf = slddrw.with_suffix(".pdf")
    try:
        r = _post(base, "/op", {
            "kind": "exportDrawingPdf",
            "params": {"out": str(pdf).replace("\\", "/")},
        }, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    inner = (r or {}).get("result") or {}
    if not (r.get("ok") and inner.get("ok")): return None
    return pdf if pdf.is_file() else None


def _pdf_to_png(pdf: Path, out_dir: Path, dpi: int = 200) -> list[Path]:
    """Render each PDF page to PNG. Uses pymupdf if available, otherwise
    falls back to pdf2image. Returns list of page PNG paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pngs: list[Path] = []
    try:
        import fitz  # type: ignore  # pymupdf
        with fitz.open(str(pdf)) as doc:
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=mat, alpha=False)
                p = out_dir / f"page_{i+1:02d}.png"
                pix.save(str(p))
                pngs.append(p)
        return pngs
    except ImportError:
        pass
    try:
        from pdf2image import convert_from_path  # type: ignore
        imgs = convert_from_path(str(pdf), dpi=dpi)
        for i, im in enumerate(imgs):
            p = out_dir / f"page_{i+1:02d}.png"
            im.save(str(p))
            pngs.append(p)
        return pngs
    except ImportError:
        pass
    # Last resort: matplotlib + a tiny PDF reader. Keep this scope-creep
    # minimal — if neither pymupdf nor pdf2image are installed, the
    # verify gate degrades to a "best-effort" outcome (verified=None).
    return []


def _build_checklist(expected: dict) -> list[str]:
    """Generate a structured feature list from the params that were
    passed to enrichDrawing — what we need to see in the PNG to call
    the enrichment ok."""
    checks: list[str] = []
    # Datum letters — primary/secondary/tertiary, possibly with axis tag
    for k in ("primary_datum", "secondary_datum", "tertiary_datum"):
        v = expected.get(k)
        if not v: continue
        # "A(X)" → check for "A" letter; "A" → "A"; both should appear
        # somewhere on the sheet inside a datum-feature symbol box.
        letter = str(v).split("(", 1)[0].strip()
        checks.append(f"datum-feature symbol with letter '{letter}'")
    # Tolerance numerics — if non-default, look for the numbers
    pos = expected.get("position_tolerance_mm")
    if pos is not None:
        checks.append(f"position tolerance value '{pos:g}' visible in a "
                       f"feature control frame")
    flat = expected.get("flatness_mm")
    if flat is not None:
        checks.append(f"flatness FCF with value '{flat:g}'")
    perp = expected.get("perpendicularity_mm")
    if perp is not None:
        checks.append(f"perpendicularity FCF with value '{perp:g}'")
    # General-tol block + standard
    if expected.get("general_linear_mm"):
        checks.append("general tolerance note (text 'GENERAL TOL' or "
                       "'TOLERANCES UNLESS NOTED' visible)")
    if expected.get("standard"):
        checks.append(f"GD&T standard '{expected['standard']}' or "
                       f"'ASME Y14.5' text visible")
    # Feature ops — section + exploded
    if expected.get("section_view", True):
        checks.append("section line A-A visible OR section view labeled "
                       "'SECTION A-A'")
    if expected.get("exploded_view", True):
        checks.append("exploded view present OR text 'EXPLODED VIEW' "
                       "visible (placeholder note acceptable)")
    return checks


def _vision_check(png_path: Path, checklist: list[str],
                    repo_root: Path) -> dict:
    """Send the rendered PNG + checklist to Claude vision. Returns
    {verified: bool|None, missing: list[str], confidence: float,
     reason: str|None}."""
    try:
        from aria_os.visual_verifier import _call_vision  # type: ignore
    except Exception as exc:
        return {"verified": None, "missing": [], "confidence": 0.0,
                  "reason": f"_call_vision unavailable: {exc}"}
    if not png_path.is_file():
        return {"verified": None, "missing": [], "confidence": 0.0,
                  "reason": f"png not found: {png_path}"}
    view_labels = [f"Drawing sheet (page render of .slddrw at {png_path.name})"]
    goal = ("Engineering drawing — verify GD&T enrichment is present. "
             "The drawing should carry datum-feature symbols, feature "
             "control frames (FCFs), a section view, and (for asm) an "
             "exploded view callout.")
    spec = {"checklist": checklist}  # carried into the prompt by _call_vision
    try:
        # _call_vision signature varies; we pass [(path,)] images and
        # the checklist as both `goal` qualifiers and explicit feature
        # checks in the prompt.
        result = _call_vision([str(png_path)], view_labels, goal,
                                checklist, repo_root)
    except TypeError:
        # Older signature (image_paths, prompt, repo_root)
        from aria_os.visual_verifier import _call_vision_anthropic
        prompt = (f"Look at this engineering drawing image and confirm "
                  f"each item below is visible. Return JSON: "
                  f"{{'verified': bool, 'missing': [str], "
                  f"'confidence': float}}.\n\n"
                  + "\n".join(f"- {c}" for c in checklist))
        try:
            result = _call_vision_anthropic([str(png_path)], prompt, repo_root)
        except Exception as exc:
            return {"verified": None, "missing": [], "confidence": 0.0,
                      "reason": f"vision call failed: {exc}"}
    except Exception as exc:
        return {"verified": None, "missing": [], "confidence": 0.0,
                  "reason": f"vision call failed: {exc}"}

    # Normalize the response shape — _call_vision returns a dict
    # whose exact keys depend on the provider chain. We're after a
    # boolean overall_match + a missing-feature list.
    verified = (result.get("verified") if isinstance(result, dict)
                 else None)
    if verified is None:
        verified = result.get("overall_match") if isinstance(result, dict) \
            else None
    missing = []
    if isinstance(result, dict):
        for key in ("missing", "missing_features", "issues"):
            v = result.get(key)
            if isinstance(v, list):
                missing = [str(x) for x in v]
                break
        # Some providers return per-check pass/fail
        for key in ("checks", "feature_checks"):
            for c in (result.get(key) or []):
                if isinstance(c, dict) and not c.get("present", True):
                    missing.append(c.get("name") or c.get("description") or "?")
    confidence = float(result.get("confidence", 0.0)) \
        if isinstance(result, dict) else 0.0
    return {"verified": bool(verified) if verified is not None else None,
              "missing": missing,
              "confidence": confidence,
              "reason": None}


def verify_drawing(slddrw_path: str | Path,
                    expected: dict,
                    *,
                    port: int = 7501,
                    repo_root: Path | None = None) -> dict:
    """Render <slddrw>, vision-check the rendered PNG against a
    checklist derived from <expected>. Self-contained — caller doesn't
    need to know about pdf, pymupdf, or _call_vision."""
    slddrw = Path(slddrw_path).resolve()
    if not slddrw.is_file():
        return {"verified": None, "missing": [],
                  "reason": f"slddrw not found: {slddrw}",
                  "checklist": [], "confidence": 0.0}
    base = f"http://localhost:{port}"
    repo_root = repo_root or Path(__file__).resolve().parents[2]

    pdf = _slddrw_to_pdf(slddrw, base)
    if pdf is None:
        return {"verified": None, "missing": [],
                  "reason": "exportDrawingPdf failed (addin unreachable "
                            "or PDF write failed)",
                  "checklist": [], "confidence": 0.0}

    png_dir = slddrw.parent / "_verify"
    pngs = _pdf_to_png(pdf, png_dir, dpi=200)
    if not pngs:
        return {"verified": None, "missing": [],
                  "reason": "pdf→png renderer unavailable "
                            "(install pymupdf or pdf2image)",
                  "checklist": [], "confidence": 0.0,
                  "pdf": str(pdf)}

    checklist = _build_checklist(expected)
    # Use the first page (most drawings are 1-sheet). For multi-sheet,
    # we'd merge or run per-sheet — out of scope for the MVP.
    page1 = pngs[0]
    vision = _vision_check(page1, checklist, repo_root)
    return {
        "verified":   vision["verified"],
        "missing":    vision["missing"],
        "confidence": vision["confidence"],
        "reason":     vision.get("reason"),
        "checklist":  checklist,
        "pdf":        str(pdf),
        "screenshot": str(page1),
    }


def verify_and_recover(slddrw_path: str | Path,
                        expected: dict,
                        *,
                        retry_params: dict | None = None,
                        port: int = 7501,
                        max_retries: int = 1,
                        repo_root: Path | None = None) -> dict:
    """Run verify_drawing; if FAIL, call /op enrichDrawing once more with
    `retry_params` (or with `expected` + a 'force_recompute=True' hint),
    then re-verify. Returns the final verify result + a list of retries.

    The recovery layer: orchestrator just gets back a single result with
    `verified` and `retries_used` — the retry happens automatically."""
    base = f"http://localhost:{port}"
    retries: list[dict] = []
    result = verify_drawing(slddrw_path, expected, port=port,
                              repo_root=repo_root)
    attempt = 0
    while attempt < max_retries:
        if result.get("verified"):  # PASS — done
            break
        if result.get("verified") is None:
            # Tooling not available — can't retry meaningfully (no signal
            # to act on). Return the result as-is so caller knows the
            # gate skipped.
            break
        # FAIL — re-run enrichDrawing with stronger hints
        attempt += 1
        params = dict(expected)
        if retry_params: params.update(retry_params)
        # Append missing-features as a hint string the addin can log;
        # the addin doesn't yet read this but storing it surfaces in
        # the SW log file for postmortem.
        if result.get("missing"):
            params["_recover_missing"] = ", ".join(result["missing"])
        try:
            r = _post(base, "/op",
                      {"kind": "enrichDrawing", "params": params},
                      timeout=300.0)
            retries.append({"attempt": attempt, "ok": r.get("ok"),
                             "missing_before": result.get("missing")})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            retries.append({"attempt": attempt, "ok": False,
                             "error": f"{type(exc).__name__}: {exc}"})
            break
        time.sleep(1.0)  # let SW finish redrawing
        result = verify_drawing(slddrw_path, expected, port=port,
                                  repo_root=repo_root)
    result["retries_used"] = retries
    return result


def main() -> int:
    """CLI: python -m aria_os.drawing.verify_drawing <slddrw> [--recover]

    Reads expected params from a sibling `<slddrw>.expected.json` if
    present (matching the orchestrator's enrichDrawing payload), or
    falls back to defaults."""
    import sys
    if len(sys.argv) < 2:
        print("usage: verify_drawing.py <slddrw> [--recover]")
        return 2
    slddrw = Path(sys.argv[1]).resolve()
    do_recover = "--recover" in sys.argv
    expected_json = slddrw.with_suffix(".expected.json")
    expected: dict = {}
    if expected_json.is_file():
        try:
            expected = json.loads(expected_json.read_text("utf-8"))
        except Exception: pass
    expected.setdefault("section_view", True)
    expected.setdefault("exploded_view", True)
    if do_recover:
        result = verify_and_recover(slddrw, expected)
    else:
        result = verify_drawing(slddrw, expected)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("verified") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
