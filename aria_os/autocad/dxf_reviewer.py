"""
dxf_reviewer.py — Engineering review and LLM-assisted editing of existing DXF files.

Analyzes an existing DXF file, queries an LLM for actionable engineering
suggestions, prompts the user to select which to apply, then produces a revised
DXF (original unchanged) and a JSON sidecar summarising the session.

Public API
----------
review_dxf(dxf_path, state, discipline, hint, repo_root, interactive) -> Path
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Guard: ezdxf is optional at import time — raise clearly if missing
# ---------------------------------------------------------------------------

try:
    import ezdxf
    from ezdxf.layouts import Modelspace as _MSP
except ImportError as _ezdxf_err:
    raise ImportError(
        "ezdxf is required for DXF review. "
        "Install it with:  pip install ezdxf"
    ) from _ezdxf_err

from aria_os.autocad.standards_library import get_standard
from aria_os.llm_client import call_llm
from aria_os.autocad.civil_elements import add_north_arrow, add_title_block


# ---------------------------------------------------------------------------
# Discipline auto-detection keywords
# ---------------------------------------------------------------------------

_DISCIPLINE_LAYERS: dict[str, list[str]] = {
    "drainage":      ["DRAIN-", "STORM", "SANITARY", "CULVERT", "INLET", "MH"],
    "transportation": ["ROAD-", "CL-", "EOP", "SHLDR", "STATION", "LANE"],
    "grading":       ["GRADE-", "CONTOUR", "RETWALL", "SLOPE"],
    "utilities":     ["UTIL-", "WATER", "SEWER", "GAS", "ELEC", "FIBER"],
    "site":          ["SITE-", "BLDG", "PARKING", "ADA"],
}

_PIPE_LAYERS = {
    "DRAIN-PIPE-STORM", "DRAIN-PIPE-SANITARY",
    "UTIL-WATER-MAIN", "UTIL-SEWER-MAIN", "UTIL-GAS-MAIN",
    "UTIL-STORM-MAIN",
}


# ---------------------------------------------------------------------------
# Step 1 — Analyze DXF
# ---------------------------------------------------------------------------

def _detect_discipline(layers_used: list[str]) -> str:
    """Guess discipline from layer names present in the file."""
    scores: dict[str, int] = {d: 0 for d in _DISCIPLINE_LAYERS}
    for layer in layers_used:
        layer_upper = layer.upper()
        for disc, keywords in _DISCIPLINE_LAYERS.items():
            for kw in keywords:
                if kw in layer_upper:
                    scores[disc] += 1
                    break
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else "general"


def _collect_text_labels(msp: "_MSP") -> list[str]:
    """Return all text/mtext content strings from modelspace."""
    labels: list[str] = []
    for e in msp:
        if e.dxftype() == "TEXT":
            val = e.dxf.get("text", "").strip()
            if val:
                labels.append(val)
        elif e.dxftype() == "MTEXT":
            val = e.text.strip() if hasattr(e, "text") else e.dxf.get("text", "").strip()
            if val:
                labels.append(val)
    return labels


def _has_layer_content(msp: "_MSP", layer_name: str) -> bool:
    """Return True if any entity lives on the given layer."""
    layer_upper = layer_name.upper()
    for e in msp:
        if e.dxf.get("layer", "").upper() == layer_upper:
            return True
    return False


def _has_north_arrow(msp: "_MSP", text_labels: list[str]) -> bool:
    """Heuristic: ANNO-NORTH layer has entities OR text 'N' near a circle."""
    if _has_layer_content(msp, "ANNO-NORTH"):
        return True
    # Look for a standalone "N" text near a circle entity
    circles = [
        (e.dxf.center.x, e.dxf.center.y, e.dxf.radius)
        for e in msp
        if e.dxftype() == "CIRCLE"
    ]
    for e in msp:
        if e.dxftype() == "TEXT" and e.dxf.get("text", "").strip().upper() == "N":
            tx = e.dxf.insert.x
            ty = e.dxf.insert.y
            for cx, cy, cr in circles:
                if math.hypot(tx - cx, ty - cy) < cr * 4 + 5:
                    return True
    return False


def _count_unlabeled_pipe_runs(msp: "_MSP", pipe_layers: set[str], radius: float = 5.0) -> int:
    """
    Count LINE entities on pipe layers that have no Text entity within
    `radius` drawing units of their midpoint.
    """
    texts: list[tuple[float, float]] = []
    for e in msp:
        if e.dxftype() in ("TEXT", "MTEXT"):
            try:
                insert = e.dxf.insert
                texts.append((insert.x, insert.y))
            except Exception:
                pass

    unlabeled = 0
    for e in msp:
        if e.dxftype() != "LINE":
            continue
        if e.dxf.get("layer", "").upper() not in {p.upper() for p in pipe_layers}:
            continue
        try:
            sx, sy = e.dxf.start.x, e.dxf.start.y
            ex, ey = e.dxf.end.x, e.dxf.end.y
        except Exception:
            continue
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        near = any(math.hypot(tx - mx, ty - my) <= radius for tx, ty in texts)
        if not near:
            unlabeled += 1
    return unlabeled


def _get_bbox(msp: "_MSP") -> dict[str, float]:
    """Return bounding box of all entities in modelspace."""
    xs: list[float] = []
    ys: list[float] = []

    def _add(x: float, y: float) -> None:
        xs.append(x)
        ys.append(y)

    for e in msp:
        try:
            et = e.dxftype()
            if et in ("LINE",):
                _add(e.dxf.start.x, e.dxf.start.y)
                _add(e.dxf.end.x, e.dxf.end.y)
            elif et == "CIRCLE":
                cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
                _add(cx - r, cy - r)
                _add(cx + r, cy + r)
            elif et in ("TEXT", "MTEXT"):
                ins = e.dxf.insert
                _add(ins.x, ins.y)
            elif et == "LWPOLYLINE":
                for pt in e.get_points():
                    _add(pt[0], pt[1])
        except Exception:
            pass

    if not xs:
        return {"min_x": 0.0, "max_x": 100.0, "min_y": 0.0, "max_y": 100.0}
    return {
        "min_x": min(xs), "max_x": max(xs),
        "min_y": min(ys), "max_y": max(ys),
    }


def _analyze_dxf(dxf_path: Path, state: str, discipline: str | None) -> dict[str, Any]:
    """Read a DXF and return an analysis dict."""
    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    # Layer inventory
    all_defined = {layer.dxf.name for layer in doc.layers}
    layers_used: list[str] = []
    layers_empty: list[str] = []

    for layer_name in sorted(all_defined):
        if _has_layer_content(msp, layer_name):
            layers_used.append(layer_name)
        else:
            layers_empty.append(layer_name)

    # Entity count
    entity_count = sum(1 for _ in msp)

    # Text labels
    text_labels = _collect_text_labels(msp)

    # Annotation checks
    has_north_arrow = _has_north_arrow(msp, text_labels)
    has_title_block_ = _has_layer_content(msp, "ANNO-TITLEBLOCK")
    has_general_notes = any(
        "note" in lbl.lower() or len(lbl) > 40
        for lbl in text_labels
    )

    # Unlabeled pipe runs
    pipe_runs_unlabeled = _count_unlabeled_pipe_runs(msp, _PIPE_LAYERS)

    # Bounding box
    bbox = _get_bbox(msp)

    # Discipline guess
    discipline_guess = discipline or _detect_discipline(layers_used)

    return {
        "entity_count": entity_count,
        "layers_used": layers_used,
        "layers_empty": layers_empty,
        "text_labels": text_labels[:50],  # cap for prompt size
        "has_north_arrow": has_north_arrow,
        "has_title_block": has_title_block_,
        "has_general_notes": has_general_notes,
        "pipe_runs_unlabeled": pipe_runs_unlabeled,
        "bbox": bbox,
        "discipline_guess": discipline_guess,
        "state": state.upper() if state and state.lower() != "national" else "national",
    }


# ---------------------------------------------------------------------------
# Step 2 — Build LLM prompt and get suggestions
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a licensed PE reviewing a civil engineering DXF plan. "
    "Provide specific, actionable suggestions as a JSON array. "
    "Each suggestion must have: id (int), description (str), priority "
    "('required'|'recommended'|'optional'), action (str), params (dict). "
    "Valid action types: add_text, add_mtext, add_line, add_circle, "
    "add_north_arrow, add_title_block, relayer, add_general_notes. "
    "Return ONLY the JSON array — no markdown, no prose."
)


def _build_llm_prompt(
    analysis: dict[str, Any],
    state: str,
    discipline: str,
    hint: str,
    standards: dict,
) -> str:
    bbox = analysis["bbox"]
    mid_x = (bbox["min_x"] + bbox["max_x"]) / 2
    mid_y = (bbox["min_y"] + bbox["max_y"]) / 2

    # Summarise standards into a short block to keep prompt concise
    std_summary_parts: list[str] = []
    disc_std = standards.get(discipline, {})
    for k, v in list(disc_std.items())[:12]:
        std_summary_parts.append(f"  {k}: {v}")
    std_block = "\n".join(std_summary_parts) if std_summary_parts else "  (national defaults)"

    prompt = f"""
DXF ANALYSIS REPORT
===================
File discipline : {analysis['discipline_guess']}
State           : {analysis['state']}
Entity count    : {analysis['entity_count']}
Layers in use   : {', '.join(analysis['layers_used'][:20]) or 'none'}
Empty layers    : {', '.join(analysis['layers_empty'][:15]) or 'none'}
Text labels     : {', '.join(repr(t) for t in analysis['text_labels'][:20]) or 'none'}
Has north arrow : {analysis['has_north_arrow']}
Has title block : {analysis['has_title_block']}
Has general notes: {analysis['has_general_notes']}
Unlabeled pipe runs: {analysis['pipe_runs_unlabeled']}
Bounding box    : min=({bbox['min_x']:.1f},{bbox['min_y']:.1f})  max=({bbox['max_x']:.1f},{bbox['max_y']:.1f})
Computed mid    : ({mid_x:.1f}, {mid_y:.1f})

APPLICABLE STANDARDS ({state} / {discipline})
{std_block}

USER HINT: {hint or '(none)'}

INSTRUCTIONS
------------
Return a JSON array of engineering review suggestions.  Each item:
{{
  "id": <int>,
  "description": "<concise description>",
  "priority": "required" | "recommended" | "optional",
  "action": "<action_type>",
  "params": {{... action-specific params ...}}
}}

Coordinate hints for params:
  north arrow center  : [{bbox['max_x'] + 20:.1f}, {bbox['max_y'] - 20:.1f}]
  title block origin  : [{bbox['min_x']:.1f}, {bbox['min_y'] - 30:.1f}]
  general notes origin: [{bbox['min_x']:.1f}, {bbox['min_y'] - 20:.1f}]
  pipe label example  : [{mid_x:.1f}, {mid_y + 3:.1f}]

Produce 3–8 suggestions relevant to this specific drawing.
"""
    return prompt.strip()


_FALLBACK_SUGGESTIONS: list[dict[str, Any]] = [
    {
        "id": 1,
        "description": "Add north arrow — required on all plan sheets",
        "priority": "required",
        "action": "add_north_arrow",
        "params": {"center": [120.0, 80.0], "size": 10},
    },
    {
        "id": 2,
        "description": "Add title block — required by all DOT standards",
        "priority": "required",
        "action": "add_title_block",
        "params": {"origin": [0.0, -30.0], "title": "CIVIL PLAN", "scale": "1\"=20'"},
    },
    {
        "id": 3,
        "description": "Add general notes block referencing applicable standards",
        "priority": "recommended",
        "action": "add_general_notes",
        "params": {
            "insert": [0.0, -20.0],
            "lines": [
                "GENERAL NOTES:",
                "1. ALL WORK SHALL CONFORM TO LOCAL DOT STANDARDS.",
                "2. CONTRACTOR TO VERIFY ALL DIMENSIONS IN FIELD.",
                "3. NOTIFY ENGINEER OF RECORD OF ANY CONFLICTS.",
            ],
            "layer": "ANNO-TEXT",
        },
    },
]


def _parse_suggestions(llm_response: str) -> list[dict[str, Any]]:
    """Extract JSON array from LLM response, tolerating markdown fences."""
    text = llm_response.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            ln for ln in lines if not ln.startswith("```")
        ).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # Try to find the first [...] block
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return []


def _get_suggestions(
    analysis: dict[str, Any],
    state: str,
    discipline: str,
    hint: str,
    repo_root: Path | None,
) -> list[dict[str, Any]]:
    standards = get_standard(state if state.lower() != "national" else None, discipline)
    prompt = _build_llm_prompt(analysis, state, discipline, hint, standards)
    response = call_llm(prompt, _SYSTEM_PROMPT, repo_root=repo_root)

    if response:
        suggestions = _parse_suggestions(response)
        if suggestions:
            return suggestions
        print("[DXF REVIEW] Warning: could not parse LLM response as JSON — using fallback suggestions.")

    # LLM unavailable or parse failed — produce contextual fallback list
    fallback = list(_FALLBACK_SUGGESTIONS)
    # Adjust coordinates to match actual bbox
    bbox = analysis["bbox"]
    if fallback:
        fallback[0]["params"]["center"] = [
            round(bbox["max_x"] + 20, 1),
            round(bbox["max_y"] - 20, 1),
        ]
    if len(fallback) > 1:
        fallback[1]["params"]["origin"] = [
            round(bbox["min_x"], 1),
            round(bbox["min_y"] - 30, 1),
        ]
    if len(fallback) > 2:
        fallback[2]["params"]["insert"] = [
            round(bbox["min_x"], 1),
            round(bbox["min_y"] - 20, 1),
        ]
    return fallback


# ---------------------------------------------------------------------------
# Step 3 — Display suggestions and prompt user
# ---------------------------------------------------------------------------

_PRIORITY_ORDER = {"required": 0, "recommended": 1, "optional": 2}


def _print_suggestions(suggestions: list[dict], filename: str, state: str, discipline: str) -> None:
    print(
        f"\n[DXF REVIEW] {len(suggestions)} suggestion(s) for {filename} "
        f"({state} / {discipline}):"
    )
    for s in suggestions:
        pri = s.get("priority", "optional").upper()
        desc = s.get("description", "(no description)")
        sid = s.get("id", "?")
        print(f"  [{sid}] {pri:<12} {desc}")
    print()


def _prompt_user(suggestions: list[dict]) -> list[int]:
    """
    Ask which suggestions to apply.  Returns list of selected suggestion IDs.
    """
    required_ids = [
        s["id"] for s in suggestions if s.get("priority") == "required"
    ]
    default_label = ",".join(str(i) for i in required_ids) if required_ids else "all"
    prompt_str = f"Apply which? [all / {default_label} / none] (default: all required): "

    try:
        raw = input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""

    if not raw:
        # Default: apply all required
        return required_ids if required_ids else [s["id"] for s in suggestions]

    raw_lower = raw.lower()
    if raw_lower == "all":
        return [s["id"] for s in suggestions]
    if raw_lower == "none":
        return []
    if raw_lower == "required":
        return required_ids

    # Parse comma-separated IDs
    selected: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            selected.append(int(part))
    return selected


# ---------------------------------------------------------------------------
# Step 4 — Apply edits
# ---------------------------------------------------------------------------

def _ensure_layer(doc: ezdxf.document.Drawing, layer_name: str) -> None:
    """Create layer if it does not exist."""
    if layer_name not in doc.layers:
        doc.layers.add(layer_name)


def _apply_add_text(doc, msp: "_MSP", params: dict) -> None:
    insert = params.get("insert", [0, 0])
    content = params.get("content", "")
    layer = params.get("layer", "ANNO-TEXT")
    height = params.get("height", 0.12)
    _ensure_layer(doc, layer)
    msp.add_text(
        content,
        dxfattribs={"layer": layer, "height": height, "insert": tuple(insert[:2])},
    )


def _apply_add_mtext(doc, msp: "_MSP", params: dict) -> None:
    insert = params.get("insert", [0, 0])
    content = params.get("content", "")
    layer = params.get("layer", "ANNO-TEXT")
    height = params.get("height", 0.12)
    width = params.get("width", 60)
    _ensure_layer(doc, layer)
    mt = msp.add_mtext(content, dxfattribs={"layer": layer, "char_height": height, "width": width})
    mt.set_location(tuple(insert[:2]))


def _apply_add_line(doc, msp: "_MSP", params: dict) -> None:
    start = params.get("start", [0, 0])
    end = params.get("end", [10, 0])
    layer = params.get("layer", "ANNO-TEXT")
    _ensure_layer(doc, layer)
    msp.add_line(tuple(start[:2]), tuple(end[:2]), dxfattribs={"layer": layer})


def _apply_add_circle(doc, msp: "_MSP", params: dict) -> None:
    center = params.get("center", [0, 0])
    radius = params.get("radius", 5.0)
    layer = params.get("layer", "ANNO-TEXT")
    _ensure_layer(doc, layer)
    msp.add_circle(tuple(center[:2]), radius, dxfattribs={"layer": layer})


def _apply_add_north_arrow(doc, msp: "_MSP", params: dict) -> None:
    center = params.get("center", [100.0, 80.0])
    size = params.get("size", 10.0)
    layer = params.get("layer", "ANNO-NORTH")
    _ensure_layer(doc, layer)
    add_north_arrow(msp, tuple(center[:2]), size=float(size), layer=layer)


def _apply_add_title_block(doc, msp: "_MSP", params: dict) -> None:
    origin = params.get("origin", [0.0, -30.0])
    title = params.get("title", "CIVIL PLAN")
    scale = params.get("scale", "1\"=20'")
    layer = params.get("layer", "ANNO-TITLEBLOCK")
    _ensure_layer(doc, layer)
    add_title_block(
        msp,
        origin=tuple(origin[:2]),
        title=title,
        scale=scale,
        layer=layer,
    )


def _apply_relayer(doc, msp: "_MSP", params: dict) -> None:
    from_layer = params.get("from_layer", "")
    to_layer = params.get("to_layer", "")
    if not from_layer or not to_layer:
        print(f"    [warning] relayer: missing from_layer/to_layer in params: {params}")
        return
    _ensure_layer(doc, to_layer)
    moved = 0
    for e in msp:
        if e.dxf.get("layer", "").upper() == from_layer.upper():
            e.dxf.layer = to_layer
            moved += 1
    print(f"    [relayer] moved {moved} entit{'y' if moved==1 else 'ies'} from '{from_layer}' → '{to_layer}'")


def _apply_add_general_notes(doc, msp: "_MSP", params: dict) -> None:
    insert = params.get("insert", [0, 0])
    lines = params.get("lines", ["GENERAL NOTES:"])
    layer = params.get("layer", "ANNO-TEXT")
    height = params.get("height", 0.12)
    _ensure_layer(doc, layer)
    # Combine lines into MTEXT paragraph breaks
    content = "\\P".join(str(ln) for ln in lines)
    mt = msp.add_mtext(content, dxfattribs={"layer": layer, "char_height": height, "width": 80})
    mt.set_location(tuple(insert[:2]))


_APPLY_DISPATCH: dict[str, Any] = {
    "add_text":          _apply_add_text,
    "add_mtext":         _apply_add_mtext,
    "add_line":          _apply_add_line,
    "add_circle":        _apply_add_circle,
    "add_north_arrow":   _apply_add_north_arrow,
    "add_title_block":   _apply_add_title_block,
    "relayer":           _apply_relayer,
    "add_general_notes": _apply_add_general_notes,
}


def _apply_suggestions(
    doc: ezdxf.document.Drawing,
    suggestions: list[dict],
    selected_ids: set[int],
) -> list[int]:
    """Apply selected suggestions to doc.  Returns list of successfully applied IDs."""
    msp = doc.modelspace()
    applied: list[int] = []

    for s in suggestions:
        sid = s.get("id")
        if sid not in selected_ids:
            continue
        action = s.get("action", "")
        params = s.get("params", {})
        fn = _APPLY_DISPATCH.get(action)
        if fn is None:
            print(f"  [DXF REVIEW] Warning: unknown action '{action}' for suggestion {sid} — skipped.")
            continue
        try:
            fn(doc, msp, params)
            applied.append(sid)
            print(f"  [DXF REVIEW] Applied [{sid}] {action}")
        except Exception as exc:
            print(f"  [DXF REVIEW] Warning: could not apply suggestion {sid} ({action}): {exc}")

    return applied


# ---------------------------------------------------------------------------
# Step 5 — Save
# ---------------------------------------------------------------------------

def _write_sidecar(
    out_dir: Path,
    stem: str,
    source_name: str,
    suggestions: list[dict],
    applied_ids: list[int],
    state: str,
    discipline: str,
) -> Path:
    all_ids = {s["id"] for s in suggestions}
    applied_set = set(applied_ids)
    skipped = sorted(all_ids - applied_set)

    sidecar: dict[str, Any] = {
        "source_dxf": source_name,
        "suggestions_count": len(suggestions),
        "applied": sorted(applied_ids),
        "skipped": skipped,
        "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "state": state,
        "discipline": discipline,
    }
    sidecar_path = out_dir / f"{stem}_revised_review.json"
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2)
    return sidecar_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def review_dxf(
    dxf_path: str | Path,
    state: str = "national",
    discipline: str | None = None,
    hint: str = "",
    repo_root: Path | None = None,
    interactive: bool = True,
) -> Path:
    """
    Analyze dxf_path, suggest engineering edits via LLM, apply selected edits.
    Returns path to the revised DXF (original is never modified).
    Output: <original_stem>_revised.dxf in same directory.

    Parameters
    ----------
    dxf_path    : Path to the existing DXF file.
    state       : 2-letter US state code (e.g. "NJ") or "national".
    discipline  : Civil discipline override; auto-detected if None.
                  One of: transportation, drainage, grading, utilities, site.
    hint        : Free-text hint for the LLM reviewer, e.g. "check pipe labels".
    repo_root   : Repo root for .env key lookup (passed to call_llm).
    interactive : If False, apply all suggestions without prompting.

    Returns
    -------
    Path to the revised DXF file.
    """
    dxf_path = Path(dxf_path).resolve()
    if not dxf_path.exists():
        raise FileNotFoundError(f"DXF file not found: {dxf_path}")

    # ── Step 1: analyse ──────────────────────────────────────────────────────
    print(f"[DXF REVIEW] Analysing {dxf_path.name} …")
    analysis = _analyze_dxf(dxf_path, state, discipline)
    effective_discipline: str = discipline or analysis["discipline_guess"]
    effective_state: str = analysis["state"]

    # ── Step 2: get suggestions ──────────────────────────────────────────────
    print(f"[DXF REVIEW] Requesting engineering review ({effective_state} / {effective_discipline}) …")
    suggestions = _get_suggestions(
        analysis, effective_state, effective_discipline, hint, repo_root
    )

    if not suggestions:
        print("[DXF REVIEW] No suggestions generated — nothing to apply.")
        return dxf_path

    # Sort: required first, then recommended, then optional
    suggestions.sort(key=lambda s: _PRIORITY_ORDER.get(s.get("priority", "optional"), 2))

    # ── Step 3: prompt ───────────────────────────────────────────────────────
    _print_suggestions(suggestions, dxf_path.name, effective_state, effective_discipline)

    if interactive:
        selected_ids = set(_prompt_user(suggestions))
    else:
        selected_ids = {s["id"] for s in suggestions}

    if not selected_ids:
        print("[DXF REVIEW] No suggestions selected — original file unchanged.")
        return dxf_path

    # ── Step 4: apply ────────────────────────────────────────────────────────
    doc = ezdxf.readfile(str(dxf_path))
    applied_ids = _apply_suggestions(doc, suggestions, selected_ids)

    # ── Step 5: save ─────────────────────────────────────────────────────────
    out_path = dxf_path.parent / f"{dxf_path.stem}_revised.dxf"
    doc.saveas(str(out_path))

    sidecar_path = _write_sidecar(
        dxf_path.parent,
        dxf_path.stem,
        dxf_path.name,
        suggestions,
        applied_ids,
        effective_state,
        effective_discipline,
    )

    print(
        f"[DXF REVIEW] Applied {len(applied_ids)} edit(s).  "
        f"Revised DXF: {out_path}"
    )
    print(f"[DXF REVIEW] Sidecar: {sidecar_path}")
    print(f"[DXF REVIEW] To view: python run_aria_os.py --review-view {out_path}")

    return out_path
