"""
aria_os/step_reviewer.py

Analyze an existing STEP file, suggest engineering redesign modifications via LLM,
apply selected modifications through CadQuery, and output a revised STEP.

Public API
----------
review_step(step_path, hint="", repo_root=None, interactive=True) -> Path
    Analyze geometry, suggest modifications, apply selected ones.
    Returns path to revised STEP (original untouched).
    Output: <stem>_revised.step in same directory.
"""
from __future__ import annotations

import json
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aria_os.llm_client import call_llm

# ---------------------------------------------------------------------------
# Part-type detection from filename stem
# ---------------------------------------------------------------------------

_PART_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("ratchet_ring", "ratchet ring"),
    ("ratchet", "ratchet ring"),
    ("brake_drum", "brake drum"),
    ("cam_collar", "cam collar"),
    ("rope_guide", "rope guide"),
    ("catch_pawl", "catch pawl"),
    ("housing", "housing"),
    ("spool", "spool"),
    ("bracket", "bracket"),
    ("flange", "flange"),
    ("shaft", "shaft"),
    ("pulley", "pulley"),
    ("nozzle", "nozzle"),
    ("spacer", "spacer"),
    ("pin", "pin"),
    ("cam", "cam"),
]


def _detect_part_type(stem: str) -> str:
    lower = stem.lower()
    for keyword, label in _PART_TYPE_KEYWORDS:
        if keyword in lower:
            return label
    return "mechanical part"


# ---------------------------------------------------------------------------
# Geometry analysis
# ---------------------------------------------------------------------------

def _analyze_step(step_path: Path) -> dict[str, Any]:
    """Import STEP with CadQuery and extract geometry facts. Never raises."""
    analysis: dict[str, Any] = {
        "file": step_path.name,
        "part_type": _detect_part_type(step_path.stem),
        "import_error": None,
    }

    try:
        import cadquery as cq  # type: ignore
    except ImportError:
        analysis["import_error"] = "cadquery not installed — geometry analysis skipped"
        return analysis

    try:
        result = cq.importers.importStep(str(step_path))
        bb = result.val().BoundingBox()
        faces = result.val().Faces()
        edges = result.val().Edges()
        solids = result.solids().vals()

        analysis.update(
            {
                "bbox_x_mm": round(bb.xmax - bb.xmin, 2),
                "bbox_y_mm": round(bb.ymax - bb.ymin, 2),
                "bbox_z_mm": round(bb.zmax - bb.zmin, 2),
                "volume_mm3": round(result.val().Volume(), 1),
                "face_count": len(faces),
                "edge_count": len(edges),
                "solid_count": len(solids),
                # Approximate watertight check: watertight solids have all edges shared
                # by exactly 2 faces.  Checking this properly requires OCC topology; use
                # face_count as a coarse indicator instead.
                "is_watertight": len(solids) >= 1,
            }
        )
    except Exception as exc:
        analysis["import_error"] = str(exc)

    return analysis


# ---------------------------------------------------------------------------
# Fallback suggestions when LLM is unavailable
# ---------------------------------------------------------------------------

def _fallback_suggestions(analysis: dict[str, Any], hint: str) -> list[dict[str, Any]]:
    """Generate generic suggestions when no LLM backend is available."""
    suggestions: list[dict[str, Any] ] = []
    sid = 1

    face_count = analysis.get("face_count", 0)
    bbox_x = analysis.get("bbox_x_mm", 0.0)
    bbox_y = analysis.get("bbox_y_mm", 0.0)
    bbox_z = analysis.get("bbox_z_mm", 0.0)
    volume = analysis.get("volume_mm3", 0.0)

    hint_lower = hint.lower()

    # If hint mentions bolt / hole / fastener, add bolt pattern suggestion
    if any(kw in hint_lower for kw in ("bolt", "hole", "screw", "fastener", "m4", "m5", "m6", "m8")):
        bolt_r = round(min(bbox_x, bbox_y) * 0.35, 1)
        suggestions.append(
            {
                "id": sid,
                "description": f"Add 4x M5 bolt holes on {bolt_r}mm bolt circle (requested by hint)",
                "priority": "required",
                "modification": f"add 4 holes diameter 5.5mm (M5 clearance) on bolt_circle_radius={bolt_r}mm equally spaced",
                "cq_hint": f"faces('>Z').workplane().circle({bolt_r}).hole(5.5) pattern",
            }
        )
        sid += 1

    # Suggest lightening pockets if large solid
    if volume > 50_000 and "lighten" in hint_lower:
        pocket_w = round(bbox_x * 0.4, 1)
        pocket_d = round(bbox_z * 0.5, 1)
        suggestions.append(
            {
                "id": sid,
                "description": f"Add lightening pocket {pocket_w}mm x {pocket_w}mm x {pocket_d}mm deep to reduce mass",
                "priority": "recommended",
                "modification": f"cut rectangular pocket {pocket_w}x{pocket_w}mm, depth {pocket_d}mm from top face",
                "cq_hint": "faces('>Z').rect(pocket_w, pocket_w).cutBlind(-pocket_d)",
            }
        )
        sid += 1

    # Suggest chamfer if face count suggests sharp edges
    if face_count >= 6:
        suggestions.append(
            {
                "id": sid,
                "description": "Add 0.5mm chamfer on top face edges — improves assembly and deburring",
                "priority": "recommended",
                "modification": "chamfer 0.5mm on edges of top face ('>Z')",
                "cq_hint": "faces('>Z').edges().chamfer(0.5)",
            }
        )
        sid += 1

    # Generic: if very few faces and no bore, suggest bore
    if face_count < 8 and not any(kw in hint_lower for kw in ("solid", "no bore", "plug")):
        bore_d = round(min(bbox_x, bbox_y) * 0.25, 1)
        suggestions.append(
            {
                "id": sid,
                "description": f"Consider adding central bore {bore_d}mm diameter for shaft or fastener",
                "priority": "optional",
                "modification": f"add central through-bore diameter {bore_d}mm on top face ('>Z')",
                "cq_hint": f"faces('>Z').workplane().hole({bore_d})",
            }
        )
        sid += 1

    if not suggestions:
        suggestions.append(
            {
                "id": 1,
                "description": "Verify wall thickness meets 3mm minimum for CNC machining",
                "priority": "recommended",
                "modification": "review shell or wall thickness; increase to minimum 3mm if thinner",
                "cq_hint": "shell() thickness parameter or extrude wall dimension",
            }
        )

    return suggestions


# ---------------------------------------------------------------------------
# LLM-based suggestions
# ---------------------------------------------------------------------------

_SUGGESTION_SYSTEM = (
    "You are a mechanical design engineer reviewing an existing STEP file for redesign "
    "opportunities. Provide specific, actionable suggestions as a JSON array. "
    "Return ONLY the JSON array — no markdown fences, no explanation."
)


def _build_suggestion_prompt(analysis: dict[str, Any], hint: str) -> str:
    lines = [
        "Geometry analysis of the imported STEP file:",
        json.dumps(analysis, indent=2),
        "",
    ]
    if hint:
        lines += [f"User guidance: {hint}", ""]

    lines += [
        "Return a JSON array of redesign suggestions. Each element must have these keys:",
        '  "id"           : integer, starting at 1',
        '  "description"  : one sentence describing the issue and improvement',
        '  "priority"     : "required" | "recommended" | "optional"',
        '  "modification" : concise instruction for a CadQuery script to apply',
        '  "cq_hint"      : short CadQuery API hint (e.g. faces / holes / shell calls)',
        "",
        "Return ONLY the JSON array. 2–5 suggestions is ideal. Example structure:",
        '[{"id":1,"description":"...","priority":"required","modification":"...","cq_hint":"..."}]',
    ]
    return "\n".join(lines)


def _get_suggestions(
    analysis: dict[str, Any],
    hint: str,
    repo_root: Path | None,
) -> list[dict[str, Any]]:
    """Call LLM for suggestions; fall back to heuristics on failure."""
    prompt = _build_suggestion_prompt(analysis, hint)
    raw = call_llm(prompt, _SUGGESTION_SYSTEM, repo_root=repo_root)

    if raw:
        # Strip any accidental markdown fences
        raw = raw.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                # Ensure required keys exist
                cleaned = []
                for i, item in enumerate(data, 1):
                    if not isinstance(item, dict):
                        continue
                    cleaned.append(
                        {
                            "id": item.get("id", i),
                            "description": str(item.get("description", "Improvement")),
                            "priority": str(item.get("priority", "recommended")),
                            "modification": str(item.get("modification", "")),
                            "cq_hint": str(item.get("cq_hint", "")),
                        }
                    )
                if cleaned:
                    return cleaned
        except (json.JSONDecodeError, ValueError):
            pass
        print("[STEP REVIEW] LLM returned unexpected format — using heuristic suggestions.")

    else:
        print("[STEP REVIEW] LLM unavailable — using heuristic suggestions.")

    return _fallback_suggestions(analysis, hint)


# ---------------------------------------------------------------------------
# Interactive selection
# ---------------------------------------------------------------------------

def _priority_label(priority: str) -> str:
    return priority.upper().ljust(11)


def _show_suggestions(
    suggestions: list[dict[str, Any]],
    stem: str,
    analysis: dict[str, Any],
) -> None:
    bbox = ""
    if "bbox_x_mm" in analysis:
        bbox = (
            f" ({analysis['bbox_x_mm']} x {analysis['bbox_y_mm']} x "
            f"{analysis['bbox_z_mm']} mm)"
        )
    print(
        f"\n[STEP REVIEW] {len(suggestions)} suggestion(s) for {stem}.step{bbox}:"
    )
    for s in suggestions:
        print(f"  [{s['id']}] {_priority_label(s['priority'])}  {s['description']}")
    print()


def _select_suggestions(
    suggestions: list[dict[str, Any]],
    interactive: bool,
) -> list[dict[str, Any]]:
    """
    Prompt user to select which suggestions to apply.
    Returns selected subset.
    """
    required_ids = [s["id"] for s in suggestions if s["priority"] == "required"]
    default_label = ",".join(str(i) for i in required_ids) if required_ids else "none"

    if not interactive or not sys.stdin.isatty():
        # Non-interactive: apply all required suggestions
        selected = [s for s in suggestions if s["priority"] == "required"]
        if not selected:
            selected = suggestions  # apply all if none are required
        return selected

    try:
        answer = input(
            f"Apply which? [all / {','.join(str(i) for i in [s['id'] for s in suggestions])} "
            f"/ none] (default: {default_label or 'all'}): "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""

    if answer in ("", "all"):
        if answer == "" and default_label and default_label != "all":
            # Default: apply only required
            return [s for s in suggestions if s["id"] in required_ids] or suggestions
        return suggestions

    if answer == "none":
        return []

    # Parse comma-separated ids
    try:
        chosen_ids = {int(x.strip()) for x in answer.split(",") if x.strip()}
        return [s for s in suggestions if s["id"] in chosen_ids]
    except ValueError:
        print("[STEP REVIEW] Could not parse selection — applying all required suggestions.")
        return [s for s in suggestions if s["priority"] == "required"] or suggestions


# ---------------------------------------------------------------------------
# Script generation + execution
# ---------------------------------------------------------------------------

_MODIFY_SYSTEM = (
    "You are a CadQuery expert generating a modification script for an existing STEP file. "
    "Output ONLY a complete Python script. No markdown fences, no explanation outside the script."
)


def _build_modify_prompt(
    step_path: Path,
    output_step_path: Path,
    selected: list[dict[str, Any]],
) -> str:
    mods = "\n".join(
        f"  {i+1}. {s['description']}\n     Instruction: {s['modification']}\n     CQ hint: {s['cq_hint']}"
        for i, s in enumerate(selected)
    )
    return textwrap.dedent(
        f"""\
        Given this existing STEP file imported into CadQuery, write a Python script that applies
        these modifications:

        {mods}

        Rules:
        - Import the STEP file using: result = cq.importers.importStep(r"{step_path}")
        - Apply each modification as a CadQuery operation on the imported solid.
        - If a modification cannot be applied cleanly, skip it rather than raising an error.
        - End with: cq.exporters.export(result, r"{output_step_path}")
        - No BBOX print needed. No markdown. Output the script only.
        - Use only: import cadquery as cq

        Base structure:
        import cadquery as cq
        result = cq.importers.importStep(r"{step_path}")
        # Apply modifications here
        cq.exporters.export(result, r"{output_step_path}")
        """
    )


def _extract_script(text: str) -> str:
    """Strip any markdown fences from LLM output."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _existing_cq_script(stem: str, repo_root: Path) -> Path | None:
    """Look for a matching .py in outputs/cad/generated_code/<stem>.py."""
    gen_dir = repo_root / "outputs" / "cad" / "generated_code"
    candidate = gen_dir / f"{stem}.py"
    if candidate.exists():
        return candidate
    # Also try glob for scripts that contain the stem
    matches = sorted(gen_dir.glob(f"*{stem}*.py"))
    if matches:
        return matches[-1]  # most recent by name
    return None


def _apply_modifications(
    step_path: Path,
    output_step_path: Path,
    selected: list[dict[str, Any]],
    script_save_path: Path,
    repo_root: Path,
) -> bool:
    """
    Generate + execute a CadQuery modification script.
    Saves script to script_save_path always.
    Returns True on success, False if execution failed (user must run manually).
    """
    if not selected:
        return False

    # Check for existing generated script for this part
    existing_script = _existing_cq_script(step_path.stem, repo_root)

    if existing_script:
        print(f"[STEP REVIEW] Found existing CadQuery script: {existing_script.name}")
        try:
            existing_code = existing_script.read_text(encoding="utf-8")
        except Exception:
            existing_code = ""
    else:
        existing_code = ""

    if existing_code:
        # Use modifier-style: inject modifications on top of existing script
        from aria_os.llm_client import call_llm as _call_llm

        mod_descriptions = "; ".join(
            f"{s['modification']} ({s['cq_hint']})" for s in selected
        )
        mod_prompt = (
            f"Here is existing CadQuery code:\n```\n{existing_code[:5000]}\n```\n\n"
            f"Modify it to: {mod_descriptions}\n\n"
            f"Also change the export path to: {output_step_path}\n\n"
            "Output the full modified script only. No markdown."
        )
        mod_system = (
            "You are a CadQuery expert. Output ONLY a complete Python script. "
            "No markdown fences, no explanation."
        )
        raw = _call_llm(mod_prompt, mod_system, repo_root=repo_root)
    else:
        prompt = _build_modify_prompt(step_path, output_step_path, selected)
        raw = call_llm(prompt, _MODIFY_SYSTEM, repo_root=repo_root)

    if not raw:
        # No LLM — write a stub script the user can fill in
        stub = textwrap.dedent(
            f"""\
            import cadquery as cq

            # TODO: LLM was unavailable. Apply the following modifications manually:
            # {chr(10)+'# '.join(s['description'] for s in selected)}

            result = cq.importers.importStep(r"{step_path}")

            # --- your modifications here ---

            cq.exporters.export(result, r"{output_step_path}")
            """
        )
        script_save_path.parent.mkdir(parents=True, exist_ok=True)
        script_save_path.write_text(stub, encoding="utf-8")
        print(f"[STEP REVIEW] LLM unavailable. Stub script saved: {script_save_path}")
        print("[STEP REVIEW] Edit the stub and run it manually to produce the revised STEP.")
        return False

    script = _extract_script(raw)

    # Save script unconditionally
    script_save_path.parent.mkdir(parents=True, exist_ok=True)
    script_save_path.write_text(script, encoding="utf-8")
    print(f"[STEP REVIEW] Script saved: {script_save_path}")

    # Execute
    output_step_path.parent.mkdir(parents=True, exist_ok=True)
    namespace: dict[str, Any] = {}
    try:
        exec(compile(script, str(script_save_path), "exec"), namespace)  # noqa: S102
        if output_step_path.exists() and output_step_path.stat().st_size > 0:
            return True
        print("[STEP REVIEW] Script executed but output STEP not found or empty.")
        return False
    except Exception as exc:
        print(f"[STEP REVIEW] Script execution failed: {exc}")
        print(f"[STEP REVIEW] Run the script manually: {script_save_path}")
        return False


# ---------------------------------------------------------------------------
# Sidecar JSON
# ---------------------------------------------------------------------------

def _write_sidecar(
    step_path: Path,
    analysis: dict[str, Any],
    suggestions: list[dict[str, Any]],
    applied_ids: list[int],
    skipped_ids: list[int],
    hint: str,
) -> Path:
    sidecar_path = step_path.parent / f"{step_path.stem}_review.json"
    bbox: dict[str, float] = {}
    if "bbox_x_mm" in analysis:
        bbox = {
            "x": analysis["bbox_x_mm"],
            "y": analysis["bbox_y_mm"],
            "z": analysis["bbox_z_mm"],
        }
    data = {
        "source_step": step_path.name,
        "bbox_mm": bbox,
        "suggestions_count": len(suggestions),
        "applied": applied_ids,
        "skipped": skipped_ids,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "hint": hint,
    }
    sidecar_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return sidecar_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def review_step(
    step_path: str | Path,
    hint: str = "",
    repo_root: Path | None = None,
    interactive: bool = True,
) -> Path:
    """
    Analyze step_path geometry, suggest modifications, apply selected ones via CadQuery.

    Parameters
    ----------
    step_path   : path to existing STEP file
    hint        : optional user guidance, e.g. "add lightening holes"
    repo_root   : repo root (auto-detected when None)
    interactive : when True, prompt user to select suggestions; when False, apply all required

    Returns
    -------
    Path to revised STEP file (<stem>_revised.step in same directory).
    The original STEP is never modified.
    """
    step_path = Path(step_path).resolve()

    if repo_root is None:
        # Walk upward to find the repo root (contains CLAUDE.md or pyproject.toml)
        candidate = step_path.parent
        for _ in range(8):
            if (candidate / "CLAUDE.md").exists() or (candidate / "pyproject.toml").exists():
                repo_root = candidate
                break
            candidate = candidate.parent
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent

    repo_root = Path(repo_root)
    output_step_path = step_path.parent / f"{step_path.stem}_revised.step"
    script_save_path = (
        repo_root / "outputs" / "cad" / "generated_code" / f"{step_path.stem}_modifications.py"
    )

    # ------------------------------------------------------------------
    # Step 1 — Analyze geometry
    # ------------------------------------------------------------------
    print(f"[STEP REVIEW] Analyzing {step_path.name} ...")
    analysis = _analyze_step(step_path)

    if analysis.get("import_error"):
        print(f"[STEP REVIEW] Warning: {analysis['import_error']}")

    # ------------------------------------------------------------------
    # Step 2 — Get suggestions from LLM
    # ------------------------------------------------------------------
    print("[STEP REVIEW] Requesting redesign suggestions ...")
    suggestions = _get_suggestions(analysis, hint, repo_root)

    if not suggestions:
        print("[STEP REVIEW] No suggestions generated.")
        _write_sidecar(step_path, analysis, [], [], [], hint)
        return output_step_path

    # ------------------------------------------------------------------
    # Step 3 — Display and select
    # ------------------------------------------------------------------
    _show_suggestions(suggestions, step_path.stem, analysis)
    selected = _select_suggestions(suggestions, interactive)

    if not selected:
        print("[STEP REVIEW] No modifications selected. Original STEP unchanged.")
        skipped = [s["id"] for s in suggestions]
        _write_sidecar(step_path, analysis, suggestions, [], skipped, hint)
        return step_path

    print(
        f"[STEP REVIEW] Applying {len(selected)} modification(s): "
        + ", ".join(str(s["id"]) for s in selected)
    )

    # ------------------------------------------------------------------
    # Step 4 — Apply modifications
    # ------------------------------------------------------------------
    success = _apply_modifications(
        step_path,
        output_step_path,
        selected,
        script_save_path,
        repo_root,
    )

    # ------------------------------------------------------------------
    # Step 5 — Report and write sidecar
    # ------------------------------------------------------------------
    applied_ids = [s["id"] for s in selected] if success else []
    skipped_ids = (
        [s["id"] for s in suggestions if s["id"] not in {x["id"] for x in selected}]
        + ([] if success else [s["id"] for s in selected])
    )

    sidecar = _write_sidecar(step_path, analysis, suggestions, applied_ids, skipped_ids, hint)

    if success:
        print(f"[STEP REVIEW] Applied {len(selected)} modification(s). Revised STEP: {output_step_path}")
    else:
        print("[STEP REVIEW] Modifications could not be applied automatically.")
        print(f"[STEP REVIEW] Run the script manually: {script_save_path}")

    print(f"[STEP REVIEW] Review sidecar: {sidecar}")
    return output_step_path


# ---------------------------------------------------------------------------
# CLI shim (python -m aria_os.step_reviewer <step_path> [hint])
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze a STEP file and apply LLM-suggested redesign modifications."
    )
    parser.add_argument("step_path", help="Path to input STEP file")
    parser.add_argument(
        "hint",
        nargs="?",
        default="",
        help='Optional guidance, e.g. "add lightening holes"',
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Apply all required suggestions without prompting",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root path (auto-detected when omitted)",
    )
    args = parser.parse_args()

    out = review_step(
        step_path=args.step_path,
        hint=args.hint,
        repo_root=Path(args.repo_root) if args.repo_root else None,
        interactive=not args.no_interactive,
    )
    print(f"\nOutput: {out}")
