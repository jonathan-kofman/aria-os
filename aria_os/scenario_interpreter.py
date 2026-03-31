"""
ARIA Scenario Interpreter

Converts a real-world scenario + constraints into a list of CAD goal strings,
then optionally triggers the generation pipeline for each.

Two modes:
  --scenario      Single-pass: identify all parts for a focused task (≤~15 parts)
  --system        Two-pass:    decompose into subsystems, then expand each into parts
                               (suitable for whole machines, robots, vehicles, etc.)

Usage
-----
    python run_aria_os.py --scenario "mount a 500W BLDC on 40x40 extrusion..."
    python run_aria_os.py --scenario-dry-run "..."

    python run_aria_os.py --system "design a desktop CNC router 300x300x100mm work envelope"
    python run_aria_os.py --system-dry-run "..."

    # From code:
    from aria_os.scenario_interpreter import interpret_scenario, interpret_system
    goals = interpret_scenario(scenario_text, repo_root=repo_root)
    result = interpret_system(scenario_text, repo_root=repo_root)
    # result -> {"subsystems": [...], "parts": [...], "assembly_path": str|None}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# System prompts — single-pass
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert mechanical design engineer. Your task is to decompose a
real-world engineering scenario into a list of individual CAD parts that need
to be designed and generated.

## Output format

Return ONLY a JSON array with no extra text, markdown fences, or explanation.
Each element must follow this exact schema:

{
  "goal": "<natural-language goal string with specific dimensions>",
  "part_id": "<snake_case_identifier>",
  "priority": <integer, 1=generate first>,
  "reason": "<one sentence: why this part is needed>",
  "depends_on": ["<part_id>", ...],
  "safety_critical": <true|false>
}

## Rules for good goal strings

Goal strings must:
- Be written as natural language descriptions suitable for a CAD pipeline
- Include ALL dimensions extracted from the scenario (OD, bore, thickness, bolt patterns, etc.)
- Specify material when mentioned or implied (aluminium 6061, steel, PETG, etc.)
- Name the part type explicitly (bracket, housing, shaft, spacer, flange, etc.)
- Follow the style used in these examples:
  * "ARIA ratchet ring, 213mm OD, 24 teeth, 21mm thick, 6061 aluminium"
  * "motor mount bracket, 80x60mm base, 4x M5 bolts at 60mm bolt circle, 3mm wall, aluminium 6061"
  * "shaft coupler, 8mm bore, 12mm OD, 30mm length, M4 set screw, steel"
  * "rope guide bracket, 45mm width, 30mm height, 6mm thickness, 2x M4 mounting holes"
  * "housing cover plate, 120x80mm, 4mm thick, 4x M3 corner bolts, ABS"
  * "spacer ring, 25mm OD, 10mm ID, 5mm thick, aluminium"

## Thinking process

For every scenario, think about ALL of these part categories — include only
those that are genuinely needed:
- Primary structural parts (the main component)
- Mounting / fastening hardware (brackets, flanges, plates)
- Shafts, couplers, keys, pins
- Spacers, standoffs, bushings, washers
- Housings, covers, enclosures
- Guides, rails, clips, retainers
- Any safety-critical parts (mark safety_critical=true)

Extract ALL specific dimensions mentioned in the scenario. If a dimension is
implied but not stated (e.g. "standard M4 bolt circle" for a 42mm NEMA 17
motor), derive it from engineering conventions and state it explicitly in the
goal string.

Priority ordering: 1 = generate first (structural, safety-critical).
Higher numbers depend on earlier parts.

Return ONLY the JSON array. No prose. No markdown. No explanations outside the JSON.
"""

# ---------------------------------------------------------------------------
# System prompts — two-pass (--system)
# ---------------------------------------------------------------------------

_SUBSYSTEM_PROMPT = """\
You are a senior systems architect and mechanical engineer designing a complete
machine or device from a high-level scenario.

## Your task: PASS 1 — Subsystem decomposition

Break the scenario into logical engineering subsystems. Each subsystem is a
self-contained functional group of parts (e.g. "X-axis drive train",
"spindle assembly", "electronics enclosure", "bed and frame").

## Output format

Return ONLY a JSON array. No markdown. No prose outside the JSON.

Each element:
{
  "name": "<human-readable subsystem name>",
  "subsystem_id": "<snake_case_id>",
  "description": "<one sentence: what this subsystem does>",
  "key_constraints": "<dimensions, loads, materials, interfaces extracted from the scenario>",
  "part_count_estimate": <integer, how many distinct CAD parts this subsystem needs>,
  "priority": <integer, 1=design first, typically structural/safety subsystems>
}

## Guidance

Think about:
- Structural frame / chassis / base plate
- Each independent drive axis (X, Y, Z for CNC; joints for robot arms)
- Power transmission (belts, screws, gears, couplers)
- Spindle / end effector / tool interface
- Motion guidance (rails, carriages, linear bearings)
- Electronics and cable management enclosures
- Cooling, lubrication, sealing systems
- User interface / operator safety guarding
- Fasteners and hardware if they need custom geometry

Keep subsystems cohesive. A desktop CNC router should have 6-10 subsystems.
A 6-DOF robot arm should have 7-10. A clock mechanism should have 4-6.

Return ONLY the JSON array.
"""

_PART_EXPANSION_PROMPT = """\
You are an expert mechanical design engineer. You are given one subsystem from
a larger machine design. Your task is to decompose it into individual CAD parts.

## Output format

Return ONLY a JSON array. No markdown. No prose outside the JSON.

Each element:
{
  "goal": "<natural-language goal with specific dimensions>",
  "part_id": "<snake_case — prefix with subsystem_id + underscore>",
  "subsystem_id": "<same subsystem_id as input>",
  "priority": <integer within this subsystem, 1=generate first>,
  "reason": "<one sentence: role of this part>",
  "depends_on": ["<part_id>", ...],
  "safety_critical": <true|false>,
  "approx_pos_mm": [x, y, z]
}

## Rules for goal strings

- Always include specific dimensions (derive from engineering standards if not stated)
- Always name the part type (bracket, housing, shaft, flange, spacer, bearing block, etc.)
- Include material (aluminium 6061, mild steel, PETG, nylon PA12, etc.)
- Examples:
  * "linear rail mounting plate, 200mm long, 40mm wide, 6mm thick, 4x M5 holes at 40mm pitch, aluminium 6061"
  * "NEMA 23 motor mount plate, 57x57mm bolt pattern, 38.1mm central bore, 3mm wall, aluminium 6061"
  * "lead screw support bearing block, 12mm bore, 30mm OD flange, 4x M4 at 40mm bolt circle, aluminium"

## approx_pos_mm

Estimate the 3D position of this part's centroid in the assembled machine,
in mm from the machine origin (bottom-left-front corner). Use [0,0,0] if uncertain.
These will be used to build an approximate assembly layout.

Return ONLY the JSON array.
"""


# ---------------------------------------------------------------------------
# Single-pass: interpret_scenario
# ---------------------------------------------------------------------------

def interpret_scenario(
    scenario: str,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Parse a focused scenario → list of goal dicts.

    Each dict: {"goal", "part_id", "priority", "reason", "depends_on", "safety_critical"}
    Falls back to a single heuristic goal if LLM is unavailable.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    from .llm_client import call_llm

    prompt = (
        f"Scenario:\n{scenario}\n\n"
        "Decompose this into individual CAD parts. "
        "Return ONLY the JSON array as specified in your instructions."
    )

    raw = call_llm(prompt, _SYSTEM_PROMPT, repo_root=repo_root)

    if raw:
        goals = _parse_llm_response(raw)
        if goals:
            return _sort_by_priority(goals)
        print(f"[SCENARIO] {_FALLBACK_INTRO}")

    return _heuristic_fallback(scenario)


def interpret_and_generate(
    scenario: str,
    repo_root: Path | None = None,
    *,
    auto_confirm: bool = False,
) -> list[dict[str, Any]]:
    """Single-pass: interpret scenario, show plan, confirm, generate each part."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    goals = interpret_scenario(scenario, repo_root=repo_root)
    _print_plan(goals, scenario)

    if not goals:
        print("[SCENARIO] No parts identified. Nothing to generate.")
        return goals

    if not auto_confirm:
        if not _sys_confirm("\nProceed with generation? [Y/n]: "):
            print("[SCENARIO] Aborted by user.")
            return goals

    from .orchestrator import run as orchestrator_run

    _run_generation_loop(goals, orchestrator_run, repo_root, auto_confirm=auto_confirm)
    _print_summary(goals)
    return goals


# ---------------------------------------------------------------------------
# Two-pass: interpret_system
# ---------------------------------------------------------------------------

def interpret_system(
    scenario: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Two-pass hierarchical decomposition for whole-machine scenarios.

    Returns:
        {
            "subsystems": list[dict],     # pass-1 output
            "parts":      list[dict],     # all parts across all subsystems (flat)
            "assembly_path": str | None,  # path to generated assembly config JSON
        }
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    from .llm_client import call_llm

    # ── Pass 1: subsystem decomposition ──────────────────────────────────────
    print("[SYSTEM] Pass 1 — decomposing scenario into subsystems...")
    p1_prompt = (
        f"Machine/system scenario:\n{scenario}\n\n"
        "Decompose into engineering subsystems. Return ONLY the JSON array."
    )
    p1_raw = call_llm(p1_prompt, _SUBSYSTEM_PROMPT, repo_root=repo_root)
    subsystems = _parse_subsystems(p1_raw, scenario)
    print(f"[SYSTEM] Found {len(subsystems)} subsystem(s).")

    # ── Pass 2: expand each subsystem into parts ──────────────────────────────
    all_parts: list[dict[str, Any]] = []
    for ss in sorted(subsystems, key=lambda s: s.get("priority", 99)):
        ss_id   = ss["subsystem_id"]
        ss_name = ss["name"]
        print(f"[SYSTEM] Pass 2 — expanding subsystem: {ss_name}...")

        p2_prompt = (
            f"Machine context:\n{scenario}\n\n"
            f"Subsystem to expand:\n"
            f"  Name: {ss_name}\n"
            f"  ID:   {ss_id}\n"
            f"  Description: {ss.get('description', '')}\n"
            f"  Key constraints: {ss.get('key_constraints', 'none stated')}\n"
            f"  Estimated part count: {ss.get('part_count_estimate', 'unknown')}\n\n"
            "Decompose this subsystem into individual CAD parts. "
            "Prefix every part_id with the subsystem_id. Return ONLY the JSON array."
        )
        p2_raw  = call_llm(p2_prompt, _PART_EXPANSION_PROMPT, repo_root=repo_root)
        parts   = _parse_llm_response(p2_raw) if p2_raw else []
        if not parts:
            print(f"  [SYSTEM] Could not expand {ss_name} — using placeholder.")
            parts = [_placeholder_part(ss_id, ss_name)]

        # Tag each part with subsystem info
        for p in parts:
            p.setdefault("subsystem_id",   ss_id)
            p.setdefault("subsystem_name", ss_name)
            p.setdefault("approx_pos_mm",  [0, 0, 0])
        all_parts.extend(parts)
        print(f"  → {len(parts)} part(s) identified.")

    # ── Generate assembly config ──────────────────────────────────────────────
    assembly_path = _write_assembly_config(all_parts, scenario, repo_root)

    return {
        "subsystems":    subsystems,
        "parts":         all_parts,
        "assembly_path": str(assembly_path) if assembly_path else None,
    }


def interpret_system_and_generate(
    scenario: str,
    repo_root: Path | None = None,
    *,
    auto_confirm: bool = False,
) -> dict[str, Any]:
    """Two-pass: decompose into subsystems, expand to parts, generate each part."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    result = interpret_system(scenario, repo_root=repo_root)
    parts  = result["parts"]

    _print_system_plan(result["subsystems"], parts, scenario)

    if not parts:
        print("[SYSTEM] No parts identified. Nothing to generate.")
        return result

    if not auto_confirm:
        if not _sys_confirm(
            f"\nProceed? This will generate {len(parts)} part(s) across "
            f"{len(result['subsystems'])} subsystem(s). [Y/n]: "
        ):
            print("[SYSTEM] Aborted by user.")
            return result

    from .orchestrator import run as orchestrator_run

    _run_generation_loop(parts, orchestrator_run, repo_root, auto_confirm=auto_confirm)
    _print_summary(parts)

    if result["assembly_path"]:
        print(f"\n[SYSTEM] Assembly config: {result['assembly_path']}")
        print("  Run:  python run_aria_os.py --assemble " + result["assembly_path"])

    return result


# ---------------------------------------------------------------------------
# Internal: parsing helpers
# ---------------------------------------------------------------------------

_FALLBACK_INTRO = (
    "Could not parse LLM response as JSON. "
    "Attempting heuristic extraction from prose..."
)


def _parse_llm_response(raw: str) -> list[dict[str, Any]] | None:
    """Try to extract a JSON array from the LLM response string."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [_normalise_item(d) for d in data if isinstance(d, dict)]
    except json.JSONDecodeError:
        pass

    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [_normalise_item(d) for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass

    return None


def _parse_subsystems(raw: str | None, scenario: str) -> list[dict[str, Any]]:
    """Parse pass-1 subsystem response; fallback to a single generic subsystem."""
    if raw:
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, list) and data:
                return data
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                    if isinstance(data, list) and data:
                        return data
                except json.JSONDecodeError:
                    pass

    print("[SYSTEM] Could not parse subsystem response — using single subsystem fallback.")
    return [
        {
            "name":               "Main system",
            "subsystem_id":       "main_system",
            "description":        "All parts derived from the scenario.",
            "key_constraints":    scenario[:200],
            "part_count_estimate": 10,
            "priority":            1,
        }
    ]


def _normalise_item(d: dict[str, Any]) -> dict[str, Any]:
    """Ensure every goal dict has required keys with safe defaults."""
    return {
        "goal":            str(d.get("goal", "unknown part")),
        "part_id":         str(d.get("part_id", "aria_part")),
        "subsystem_id":    str(d.get("subsystem_id", "")),
        "subsystem_name":  str(d.get("subsystem_name", "")),
        "priority":        int(d.get("priority", 99)),
        "reason":          str(d.get("reason", "")),
        "depends_on":      list(d.get("depends_on") or []),
        "safety_critical": bool(d.get("safety_critical", False)),
        "approx_pos_mm":   list(d.get("approx_pos_mm") or [0, 0, 0]),
    }


def _sort_by_priority(goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(goals, key=lambda g: g["priority"])


def _heuristic_fallback(scenario: str) -> list[dict[str, Any]]:
    goal = scenario.strip()
    if len(goal) > 200:
        goal = goal[:197] + "..."
    print("[SCENARIO] LLM unavailable — using scenario text as a single goal.")
    return [
        {
            "goal":            goal,
            "part_id":         "scenario_part",
            "subsystem_id":    "",
            "subsystem_name":  "",
            "priority":        1,
            "reason":          "Heuristic fallback: full scenario passed as goal.",
            "depends_on":      [],
            "safety_critical": False,
            "approx_pos_mm":   [0, 0, 0],
        }
    ]


def _placeholder_part(ss_id: str, ss_name: str) -> dict[str, Any]:
    return {
        "goal":            f"{ss_name} main structural part",
        "part_id":         f"{ss_id}_main_part",
        "subsystem_id":    ss_id,
        "subsystem_name":  ss_name,
        "priority":        1,
        "reason":          "LLM expansion unavailable — placeholder.",
        "depends_on":      [],
        "safety_critical": False,
        "approx_pos_mm":   [0, 0, 0],
    }


# ---------------------------------------------------------------------------
# Internal: assembly config generation
# ---------------------------------------------------------------------------

def _write_assembly_config(
    parts: list[dict[str, Any]],
    scenario: str,
    repo_root: Path,
) -> Path | None:
    """Generate an approximate assembly JSON config from the parts list."""
    import re as _re

    # Derive a filename slug from the first 6 words of the scenario
    words = _re.sub(r"[^\w\s]", "", scenario.lower()).split()
    slug  = "_".join(words[:6])
    slug  = _re.sub(r"_+", "_", slug).strip("_")

    config_dir = repo_root / "cad-pipeline" / "assembly_configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    out_path = config_dir / f"{slug}.json"

    config = {
        "name": scenario[:80] + ("..." if len(scenario) > 80 else ""),
        "_generated_by": "scenario_interpreter.interpret_system",
        "_note": "Positions are LLM estimates — adjust before final assembly.",
        "parts": [],
    }

    for p in parts:
        pos = p.get("approx_pos_mm") or [0, 0, 0]
        if len(pos) < 3:
            pos = list(pos) + [0] * (3 - len(pos))
        pid  = p["part_id"]
        slug_step = re.sub(r"[^\w]+", "_", pid.lower()).strip("_")
        config["parts"].append({
            "id":   pid,
            "step": f"outputs/cad/step/{slug_step}.step",
            "pos":  [round(float(v), 1) for v in pos[:3]],
            "rot":  [0, 0, 0],
        })

    try:
        out_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print(f"[SYSTEM] Assembly config written: {out_path}")
        return out_path
    except Exception as exc:
        print(f"[SYSTEM] Could not write assembly config: {exc}")
        return None


# ---------------------------------------------------------------------------
# Internal: generation loop
# ---------------------------------------------------------------------------

def _run_generation_loop(
    parts: list[dict[str, Any]],
    orchestrator_run,
    repo_root: Path,
    *,
    auto_confirm: bool,
) -> None:
    total = len(parts)
    for i, item in enumerate(parts, 1):
        goal_str = item["goal"]
        part_id  = item.get("part_id", "")
        critical = item.get("safety_critical", False)
        ss_tag   = f"[{item['subsystem_name']}] " if item.get("subsystem_name") else ""
        label    = f"[{i}/{total}] {ss_tag}{part_id}" + (" [SAFETY-CRITICAL]" if critical else "")

        if not auto_confirm:
            if not _sys_confirm(f"\nGenerate {label}?\n  Goal: {goal_str}\n  [Y/n]: "):
                print(f"  Skipped: {part_id}")
                item["generated"] = False
                continue

        print(f"\n{'='*64}")
        print(f"GENERATING: {label}")
        print(f"{'='*64}")
        try:
            session = orchestrator_run(goal_str, repo_root=repo_root)
            item["session"]   = session
            item["generated"] = True
            step = session.get("step_path", "")
            stl  = session.get("stl_path", "")
            if step:
                print(f"[DONE] {part_id}  STEP: {step}")
            elif stl:
                print(f"[DONE] {part_id}  STL: {stl}")
            else:
                print(f"[DONE] {part_id}")
            try:
                from .cem_registry import resolve_cem_module
                from .cem_generator import resolve_and_compute
                _cem_mod = resolve_cem_module(goal_str, part_id)
                if _cem_mod:
                    _cem_r = resolve_and_compute(goal_str, part_id, session.get("params") or {}, repo_root)
                    if _cem_r and not _cem_r.get("passed", True):
                        print(f"  [CEM WARN] {part_id}: SF={_cem_r.get('min_sf', '?')} — below threshold")
                        item["cem_warning"] = True
                    elif _cem_r:
                        print(f"  [CEM OK] {part_id}: SF={_cem_r.get('min_sf', '?')}")
            except Exception as _ce:
                pass  # CEM is advisory in batch generation, never block
        except Exception as exc:
            print(f"[ERROR] {part_id}: {exc}")
            item["generated"] = False
            item["error"]     = str(exc)


# ---------------------------------------------------------------------------
# Internal: display
# ---------------------------------------------------------------------------

def _print_plan(goals: list[dict[str, Any]], scenario: str) -> None:
    width = 64
    print()
    print("=" * width)
    print("SCENARIO INTERPRETATION PLAN")
    print("=" * width)
    preview = scenario.strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    print(f"Scenario: {preview}")
    print(f"Parts identified: {len(goals)}")
    print("-" * width)

    if not goals:
        print("  (none)")
    else:
        for item in goals:
            critical_tag = " [SAFETY-CRITICAL]" if item.get("safety_critical") else ""
            deps = item.get("depends_on") or []
            dep_str = f"  depends on: {', '.join(deps)}" if deps else ""
            print(
                f"  [{item['priority']}] {item['part_id']}{critical_tag}\n"
                f"      Goal:   {item['goal']}\n"
                f"      Reason: {item['reason']}"
                + (f"\n      {dep_str}" if dep_str else "")
            )
    print("=" * width)


def _print_system_plan(
    subsystems: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    scenario: str,
) -> None:
    width = 72
    print()
    print("=" * width)
    print("SYSTEM DESIGN PLAN")
    print("=" * width)
    preview = scenario.strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    print(f"Scenario:    {preview}")
    print(f"Subsystems:  {len(subsystems)}")
    print(f"Total parts: {len(parts)}")
    print()

    for ss in sorted(subsystems, key=lambda s: s.get("priority", 99)):
        ss_id   = ss["subsystem_id"]
        ss_name = ss["name"]
        ss_desc = ss.get("description", "")
        ss_parts = [p for p in parts if p.get("subsystem_id") == ss_id]

        print(f"  ── {ss_name} ({ss_id}) ──")
        print(f"     {ss_desc}")
        print(f"     {len(ss_parts)} part(s):")
        for p in sorted(ss_parts, key=lambda x: x.get("priority", 99)):
            crit = " *" if p.get("safety_critical") else ""
            print(f"       [{p['priority']}] {p['part_id']}{crit}")
            print(f"            {p['goal'][:80]}{'...' if len(p['goal'])>80 else ''}")
        print()

    print("=" * width)
    print("  * = safety-critical")
    print("=" * width)


def _print_summary(goals: list[dict[str, Any]]) -> None:
    generated = [g for g in goals if g.get("generated")]
    skipped   = [g for g in goals if g.get("generated") is False and not g.get("error")]
    failed    = [g for g in goals if g.get("error")]
    print()
    print("=" * 64)
    print("GENERATION SUMMARY")
    print("=" * 64)
    print(f"  Generated: {len(generated)}")
    print(f"  Skipped:   {len(skipped)}")
    print(f"  Failed:    {len(failed)}")
    for g in failed:
        print(f"  [FAIL] {g['part_id']}: {g.get('error', '')}")
    print("=" * 64)


def _sys_confirm(prompt: str) -> bool:
    """Prompt for Y/n. Non-interactive defaults to True."""
    if not sys.stdin.isatty():
        return True
    try:
        ans = input(prompt).strip().lower()
        return ans in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
