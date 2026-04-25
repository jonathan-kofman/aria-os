"""LLM-driven native plan generator.

For part types without a hardcoded planner, prompt an LLM to emit a
JSON list of feature operations matching our handler schema. The
result goes through the same `native_op` streaming path as hardcoded
plans, so Fusion's real feature tree still fills in live.

Contract: returns list[dict] in the form
    [{"kind": str, "params": {...}, "label": "human label"}]

Raises ValueError if the LLM returns nothing usable.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..llm_client import call_llm


# --- Schema --------------------------------------------------------------

_OPS_SCHEMA = r"""
Emit a JSON array of feature operations. Each op has:
  - kind:    one of the kinds listed below
  - params:  kind-specific (see below)
  - label:   short human description (shown in the feature tree)

## Core ops

  beginPlan:        {}                                                      — MUST be first op
  newSketch:        {plane: "XY"|"XZ"|"YZ", alias: str, name: str, offset?: mm}
                                                                            — offset shifts the sketch plane along its normal (for lofts/cross-sections)
  sketchCircle:     {sketch: alias, cx: mm, cy: mm, r: mm}
  sketchRect:       {sketch: alias, cx: mm, cy: mm, w: mm, h: mm}
  extrude:          {sketch: alias, distance: mm, operation: "new"|"cut"|"join"|"intersect",
                     alias: str}                                             — positive distance = up, negative = down
  circularPattern:  {feature: alias, axis: "X"|"Y"|"Z", count: int, alias: str}
  fillet:           {body: alias, r: mm, alias: str}

## Extended sketch primitives — use whenever a circle/rect won't capture the profile

  sketchSpline:        {sketch: alias, points: [[x,y], …], tangents?: [[dx,dy], …]}
                       — fitted spline through ≥3 points. Use for cam profiles, airfoils, organic outlines.
  sketchPolyline:      {sketch: alias, points: [[x,y], …], closed?: bool}
                       — straight-segment chain. Use for non-rect polygons, weld outlines, port shapes.
  sketchTangentArc:    {sketch: alias, start: [x,y], end: [x,y], tangent: [dx,dy]}
                       — arc tangent to an existing curve. Use for fillet-like blends in 2D.
  sketchOffset:        {sketch: alias, source: alias, distance: mm}
                       — offset another curve outward (positive) or inward (negative).
  sketchProjection:    {sketch: alias, edge: face_or_edge_id}
                       — project a 3D edge onto the sketch plane (for wrapping a feature around existing geom).
  sketchEquationCurve: {sketch: alias, expr: "x=cos(t),y=sin(t)", t_min, t_max}
                       — parametric curve from a math expression (involutes, lemniscates, lissajous).

## Extended solid features — REQUIRED for swept, lofted, revolved, helical parts

  revolve:    {sketch: alias, axis: "X"|"Y"|"Z"|alias, angle: deg,
               operation: "new"|"cut"|"join"|"intersect", alias: str}
              — spin a 2D profile around an axis. Use for shafts, nozzles, bottles, lids, anything axisymmetric.
  sweep:      {profile_sketch: alias, path_sketch: alias,
               operation: "new"|"cut"|"join"|"intersect", alias: str}
              — drag a profile along a path. Use for pipes, hoses, ducts, threaded sections,
                  cooling passages. profile and path MUST be different sketches.
  loft:       {sections: [alias, alias, …], rails?: [alias, …],
               operation: "new"|"cut"|"join"|"intersect", alias: str}
              — blend ≥2 cross-section sketches. Use for transitions (rect→round), boat hulls,
                  turbine blades, ergonomic handles, draft-aware bosses.
  helix:      {axis: "X"|"Y"|"Z"|alias, pitch: mm, height: mm, diameter: mm, alias: str}
              — pure helical curve. Pair with sweep to make a thread or coiled pipe.
  coil:       {axis: "X"|"Y"|"Z"|alias, pitch: mm, turns: int, diameter: mm,
               section: sketch_alias, alias: str}
              — helix + section profile in one op (Fusion `coil` feature). Use for springs,
                  ACME lead screws, helical gears.
  rib:        {sketch: alias, thickness: mm, alias: str}
              — auto-thickened rib from an open profile sketch. Stress-stiffening webs.
  shell:      {body: alias, thickness: mm, faces: [face_id, …], alias: str}
              — hollow out a body, removing the listed faces. Use for housings, enclosures, cups.
  draft:      {body: alias, faces: [face_id, …], pull_direction: vec, angle: deg, alias: str}
              — taper faces for moldability. Always add for cast/molded parts (1-3°).
  boundarySurface: {edges: [edge_id, …], alias: str}
              — fill a closed loop of edges with a surface. Use to cap lofts/revolves into a solid.
  thicken:    {surface: alias, thickness: mm, operation: "new"|"join"|"cut", alias: str}
              — turn a surface into a solid with given thickness. Use for sheet-like parts.

## Standard hardware — saves 20+ low-level ops each

  threadFeature: {face: face_id, spec: "M8x1.25"|"1/4-20-UNC"|"1/4-NPT", length?: mm,
                  modeled: bool, alias: str}
                — adds a real threaded face per ISO/ANSI/NPT standards. Auto-picks pitch
                  from `spec`. Set `modeled: true` for visible thread geometry, false for cosmetic.
  gearFeature:   {sketch: alias, module: mm, n_teeth: int, thickness: mm,
                  pressure_angle?: deg, helix_angle?: deg, alias: str}
                — emits a real involute spur (or helical if helix_angle set) gear.
                  This is body-creating — equivalent to extrude(operation="new").

## Rules

  1. First op MUST be beginPlan.
  2. Every sketch alias must be created by newSketch before being referenced.
  3. First body-creating op (extrude/revolve/loft/sweep with operation="new", OR gearFeature)
     MUST come before any cut/join feature.
  4. Cut features use distance = thickness * 1.5 to ensure through-cut.
  5. circularPattern.feature must reference a CUT or JOIN feature — never a "new" body.
  6. revolve angle is in degrees; use 360 for a full revolve.
  7. sweep profile and path sketches MUST be different sketches.
  8. loft needs ≥2 sections; place each on a separate sketch with appropriate `offset`.
  9. helix is a curve only — pair with sweep + a profile sketch to get geometry.
 10. coil is helix + section in one step — preferred for threads, springs, leadscrews.
 11. shell needs an existing body and ≥1 face to remove (typically the open top).
 12. threadFeature attaches to an existing cylindrical face — emit the bore extrude first.
 13. All dimensions in millimetres. Use the numeric values from the spec.
 14. Emit 6-30 ops — avoid both trivial plans and bloated 100-op plans.
 15. Return ONLY the JSON array. No markdown fences, no commentary.

## When to pick which body-creating op

  Axisymmetric (shafts, nozzles, bottles, lids, flanges):  revolve
  Constant cross-section (plates, brackets, blocks, ribs): extrude
  Variable cross-section (transitions, blades, hulls):     loft
  Profile dragged along a curve (pipes, threads):          sweep / coil
  Standard threaded fastener / hole:                       threadFeature
  Real involute gear:                                      gearFeature
""".strip()


_FEW_SHOT_EXAMPLE = r"""
## Example 1 — Flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick (extrude + cut + circularPattern)
[
  {"kind": "beginPlan", "params": {}, "label": "Reset registry"},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_body", "name": "Body"}, "label": "Sketch on XY"},
  {"kind": "sketchCircle", "params": {"sketch": "sk_body", "cx": 0, "cy": 0, "r": 50}, "label": "Outer Ø100mm"},
  {"kind": "extrude", "params": {"sketch": "sk_body", "distance": 6, "operation": "new", "alias": "body"}, "label": "Extrude 6mm"},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_hole", "name": "Bolt Hole"}, "label": "Sketch for hole"},
  {"kind": "sketchCircle", "params": {"sketch": "sk_hole", "cx": 40, "cy": 0, "r": 3.3}, "label": "M6 clearance Ø6.6mm (ISO 273)"},
  {"kind": "extrude", "params": {"sketch": "sk_hole", "distance": 9, "operation": "cut", "alias": "cut_hole"}, "label": "Cut through"},
  {"kind": "circularPattern", "params": {"feature": "cut_hole", "axis": "Z", "count": 4, "alias": "pat"}, "label": "Pattern × 4"}
]

## Example 2 — Bottle (revolve a profile around axis)
[
  {"kind": "beginPlan", "params": {}},
  {"kind": "newSketch", "params": {"plane": "XZ", "alias": "sk_p", "name": "Profile"}},
  {"kind": "sketchPolyline", "params": {"sketch": "sk_p",
       "points": [[0,0],[35,0],[35,80],[15,100],[15,120],[0,120]], "closed": false},
   "label": "Bottle outline (R-axis x H-axis)"},
  {"kind": "revolve", "params": {"sketch": "sk_p", "axis": "Z", "angle": 360,
       "operation": "new", "alias": "body"}, "label": "Revolve 360°"},
  {"kind": "shell", "params": {"body": "body", "thickness": 1.5, "faces": ["top"],
       "alias": "shelled"}, "label": "Hollow, open top"}
]

## Example 3 — Transition duct (loft round → rect)
[
  {"kind": "beginPlan", "params": {}},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_in", "name": "Inlet round", "offset": 0}},
  {"kind": "sketchCircle", "params": {"sketch": "sk_in", "cx": 0, "cy": 0, "r": 50}},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_out", "name": "Outlet rect", "offset": 200}},
  {"kind": "sketchRect", "params": {"sketch": "sk_out", "cx": 0, "cy": 0, "w": 80, "h": 40}},
  {"kind": "loft", "params": {"sections": ["sk_in", "sk_out"],
       "operation": "new", "alias": "duct"}, "label": "Loft round → rect"},
  {"kind": "shell", "params": {"body": "duct", "thickness": 1.5,
       "faces": ["inlet", "outlet"], "alias": "shelled"},
   "label": "Hollow, both ends open"}
]

## Example 4 — M16x2 socket-head cap screw, 60mm long (extrude + threadFeature + helix-cosmetic alternative)
[
  {"kind": "beginPlan", "params": {}},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_head", "name": "Head"}},
  {"kind": "sketchCircle", "params": {"sketch": "sk_head", "cx": 0, "cy": 0, "r": 12}},
  {"kind": "extrude", "params": {"sketch": "sk_head", "distance": 16, "operation": "new", "alias": "head"},
   "label": "Cap head Ø24×16mm"},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_shank", "name": "Shank"}},
  {"kind": "sketchCircle", "params": {"sketch": "sk_shank", "cx": 0, "cy": 0, "r": 8}},
  {"kind": "extrude", "params": {"sketch": "sk_shank", "distance": -60, "operation": "join", "alias": "shank"},
   "label": "Shank Ø16×60mm down"},
  {"kind": "threadFeature", "params": {"face": "shank.cyl", "spec": "M16X2",
       "length": 50, "modeled": true, "alias": "thread"},
   "label": "M16×2 thread, 50mm length"}
]

## Example 5 — Centrifugal pump volute (sweep a circle along a spline path)
[
  {"kind": "beginPlan", "params": {}},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_path", "name": "Volute spiral"}},
  {"kind": "sketchSpline", "params": {"sketch": "sk_path",
       "points": [[40,0],[28,28],[0,40],[-28,28],[-45,0],[-30,-30],[0,-50],[35,-30],[60,0]]},
   "label": "Archimedean spiral"},
  {"kind": "newSketch", "params": {"plane": "YZ", "alias": "sk_prof", "name": "Tube profile"}},
  {"kind": "sketchCircle", "params": {"sketch": "sk_prof", "cx": 40, "cy": 0, "r": 12}},
  {"kind": "sweep", "params": {"profile_sketch": "sk_prof", "path_sketch": "sk_path",
       "operation": "new", "alias": "volute"}, "label": "Sweep tube along spiral"},
  {"kind": "shell", "params": {"body": "volute", "thickness": 2,
       "faces": ["volute.inlet"], "alias": "shelled"},
   "label": "2mm wall, inlet open"}
]

## Example 6 — Spur gear, module 2, 24 teeth (gearFeature one-liner)
[
  {"kind": "beginPlan", "params": {}},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_gear", "name": "Gear blank"}},
  {"kind": "gearFeature", "params": {"sketch": "sk_gear", "module": 2, "n_teeth": 24,
       "thickness": 10, "pressure_angle": 20, "alias": "gear"},
   "label": "Involute spur, m=2, N=24, b=10"},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_bore", "name": "Bore"}},
  {"kind": "sketchCircle", "params": {"sketch": "sk_bore", "cx": 0, "cy": 0, "r": 5}},
  {"kind": "extrude", "params": {"sketch": "sk_bore", "distance": 15, "operation": "cut", "alias": "bore"},
   "label": "Ø10 bore through"}
]

## CRITICAL RULES

  - circularPattern: NEVER pattern a body created with operation="new". ALWAYS pattern a CUT or JOIN feature.
  - sweep: profile and path MUST be different sketches; the path can be a spline.
  - loft: place each section on a sketch with appropriate `offset`; ≥2 sections required.
  - helix vs coil: helix is a curve only (pair with sweep). coil is one-shot helix + profile.
  - threadFeature is the standard way to add threads — do NOT model threads with helix+sweep
    unless the user asks for a "modeled thread for FEA" or similar.
  - gearFeature is body-creating (counts as operation="new"); the bore comes after as a cut.
  - Impellers / fans / turbines: hub first (extrude new) → ONE blade off-center (extrude join)
    → circularPattern that one blade → bore last (extrude cut).
""".strip()


from .engineering_prompt import ENGINEERING_PRACTICE_PROMPT

_SYSTEM_PROMPT = (
    "You are a senior mechanical engineer writing CAD feature plans for "
    "ARIA. You convert a natural-language part description plus a parsed "
    "spec dict into an ordered list of Fusion 360 feature operations. "
    "Output is a JSON array ONLY — no prose, no markdown fences, no "
    "commentary. Every element MUST be an object `{kind, params, label}` "
    "— NEVER a bare string.\n\n"
    + ENGINEERING_PRACTICE_PROMPT + "\n\n"
    + _OPS_SCHEMA + "\n\n" + _FEW_SHOT_EXAMPLE)


# --- Main call -----------------------------------------------------------

def plan_from_llm(goal: str, spec: dict,
                   *, quality: str = "balanced",
                   repo_root: Path | None = None,
                   host_context: dict | None = None,
                   mode: str = "new") -> list[dict]:
    """Ask an LLM to turn (goal, spec) into a native feature-op plan.

    Prefers STRUCTURED OUTPUT (Anthropic tool_use / Gemini
    responseSchema) when available — guarantees valid JSON matching
    the plan schema, no parse failures. Falls back to free-text + our
    tolerant parser only when structured output is unavailable (Ollama
    / fast tier).

    If `host_context` is provided, include the current design's user
    parameters + feature tree summary so the LLM can emit consistent
    dims and target existing geometry for EXTEND prompts.

    `mode` can be 'new' (default — fresh plan starting with beginPlan)
    or 'extend' (append features on top of existing design — skip
    beginPlan, don't addParameter existing names)."""
    context_blocks = []
    if host_context:
        params = host_context.get("user_parameters") or []
        if params:
            lines = [f"  - {p['name']} = {p.get('expression', '?')}"
                     for p in params[:20]]
            context_blocks.append(
                "## Current Fusion design parameters\n"
                "These already exist — reference them by name when possible.\n"
                + "\n".join(lines))
        tree = host_context.get("feature_tree") or {}
        feats = tree.get("features") if isinstance(tree, dict) else None
        if feats and isinstance(feats, list):
            context_blocks.append(
                "## Current feature tree (in order)\n"
                + "\n".join(f"  - {f}" for f in feats[:30]))
        sel = host_context.get("selection") or []
        if sel:
            context_blocks.append(
                "## User has selected these entities — target them "
                "when the prompt says 'this', 'that', or is ambiguous\n"
                + "\n".join(f"  - {s.get('type','?')}: {s.get('id','')[:40]}"
                            for s in sel[:8]))

    mode_instructions = ""
    if mode == "extend":
        mode_instructions = (
            "\n## EXTEND MODE — THIS IS NOT A NEW PART\n"
            "The design already has geometry. Do NOT emit `beginPlan` "
            "(which resets the registry) and do NOT redeclare existing "
            "user parameters. Emit ONLY the new sketches/extrudes/"
            "patterns/fillets that realize the user's request. Reference "
            "existing user parameters by name where appropriate.\n"
        )

    user_prompt = (
        f"## Part description\n{goal.strip()}\n\n"
        f"## Parsed spec\n{json.dumps(spec, indent=2, default=str)}\n\n"
        + ("\n\n".join(context_blocks) + "\n\n" if context_blocks else "")
        + mode_instructions
        + "Produce the JSON feature-op array now."
    )

    # PREFERRED: structured output (tool_use / responseSchema).
    # Guarantees valid JSON matching the plan schema.
    try:
        from .structured_llm import plan_from_llm_structured
        structured = plan_from_llm_structured(
            user_prompt, _SYSTEM_PROMPT,
            quality=quality, repo_root=repo_root)
        if structured:
            plan = []
            for op in structured:
                if isinstance(op, dict) and "kind" in op:
                    op.setdefault("params", {})
                    op.setdefault("label", op.get("kind", "op"))
                    plan.append(op)
            if plan:
                return plan
    except Exception as _se:
        # Structured path crashed — fall back to free-text
        print(f"[LLM] structured output failed, falling back: {_se}")

    # FALLBACK: free-text LLM + tolerant JSON parser.
    raw = call_llm(user_prompt, _SYSTEM_PROMPT,
                    repo_root=repo_root, quality=quality)
    if not raw:
        raise ValueError("No LLM backend available for native planning")
    plan = _extract_json_array(raw)
    if not plan:
        raise ValueError(
            f"LLM returned no parseable plan (first 200 chars): {raw[:200]!r}")
    # Ensure every op has a label (fallback: just the kind)
    for op in plan:
        op.setdefault("label", op.get("kind", "op"))
        op.setdefault("params", {})
    return plan


# --- Helpers -------------------------------------------------------------

def _normalize_op(item) -> dict | None:
    """Coerce an LLM-emitted array element into a valid op dict.

    The fast-tier qwen/gemma models sometimes emit bare strings like
    `"beginPlan"` where they should emit `{"kind": "beginPlan", "params": {}}`.
    We tolerate that here rather than rejecting the whole plan — the
    validator catches anything truly malformed downstream.
    """
    if isinstance(item, dict):
        if "kind" in item:
            item.setdefault("params", {})
            return item
        return None
    if isinstance(item, str):
        # Bare string → treat as a parameterless op name
        return {"kind": item, "params": {}, "label": item}
    return None


def _parse_candidates(text: str) -> list[str]:
    """Yield progressively-more-lenient candidate JSON array strings."""
    cands = []
    s = text.strip()
    cands.append(s)
    # Markdown fence
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m: cands.append(m.group(1))
    # First top-level bracketed region
    m = re.search(r"(\[\s*[\{\"].*?\s*\])", text, re.DOTALL)
    if m: cands.append(m.group(1))
    # Aggressive: balance-match the first [ … ]
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "[": depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    cands.append(text[start:i+1])
                    break
    return cands


def _extract_json_array(text: str) -> list[dict] | None:
    """Pull a JSON array of ops out of an LLM reply. Tolerant of
    surrounding prose, markdown fences, bare-string elements, and
    trailing commas."""
    for raw in _parse_candidates(text):
        # Strip trailing commas before } or ] — common LLM mistake
        cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            data = json.loads(cleaned)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        normalized = [op for op in (_normalize_op(x) for x in data) if op]
        if normalized:
            return normalized
    return None
