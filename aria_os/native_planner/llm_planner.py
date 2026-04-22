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
  - kind:    one of beginPlan | newSketch | sketchCircle | sketchRect
             | extrude | circularPattern | fillet
  - params:  kind-specific (see below)
  - label:   short human description (shown in the feature tree)

Op kinds and their params:

  beginPlan:        {}                                                      — MUST be first op
  newSketch:        {plane: "XY"|"XZ"|"YZ", alias: str, name: str}          — alias is a reference used by later ops
  sketchCircle:     {sketch: alias, cx: mm, cy: mm, r: mm}
  sketchRect:       {sketch: alias, cx: mm, cy: mm, w: mm, h: mm}
  extrude:          {sketch: alias, distance: mm, operation: "new"|"cut"|"join",
                     alias: str}                                             — positive distance = up, negative = down
  circularPattern:  {feature: alias, axis: "X"|"Y"|"Z", count: int, alias: str}
  fillet:           {body: alias, r: mm, alias: str}

Rules:
  1. First op MUST be beginPlan.
  2. Every sketch alias must be created by newSketch before being referenced.
  3. First extrude MUST use operation="new" — it creates the body.
  4. Subsequent extrudes usually use "cut" (bolt holes, bores) or "join".
  5. Cut extrudes should use distance = thickness * 1.5 to ensure through-cut.
  6. circularPattern.feature must reference a previously created cut/extrude alias.
  7. All dimensions in millimetres. Use the numeric values from the spec.
  8. Emit 6-20 ops — avoid both trivial plans and bloated 100-op plans.
  9. Return ONLY the JSON array. No markdown fences, no commentary.
""".strip()


_FEW_SHOT_EXAMPLE = r"""
## Example output (for a flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick)
[
  {"kind": "beginPlan", "params": {}, "label": "Reset registry"},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_body", "name": "Body"}, "label": "Sketch on XY"},
  {"kind": "sketchCircle", "params": {"sketch": "sk_body", "cx": 0, "cy": 0, "r": 50}, "label": "Outer Ø100mm"},
  {"kind": "extrude", "params": {"sketch": "sk_body", "distance": 6, "operation": "new", "alias": "body"}, "label": "Extrude 6mm"},
  {"kind": "newSketch", "params": {"plane": "XY", "alias": "sk_hole", "name": "Bolt Hole"}, "label": "Sketch for hole"},
  {"kind": "sketchCircle", "params": {"sketch": "sk_hole", "cx": 40, "cy": 0, "r": 3}, "label": "Bolt hole Ø6mm"},
  {"kind": "extrude", "params": {"sketch": "sk_hole", "distance": 9, "operation": "cut", "alias": "cut_hole"}, "label": "Cut through"},
  {"kind": "circularPattern", "params": {"feature": "cut_hole", "axis": "Z", "count": 4, "alias": "pat"}, "label": "Pattern × 4"}
]

CRITICAL RULES for circularPattern:
  - NEVER pattern a body created with operation="new" — rotating the whole body
    is a no-op that produces the same part back.
  - ALWAYS pattern a CUT or JOIN feature (like one blade slot, one bolt hole)
    so the pattern actually replicates the feature around the axis.

CRITICAL RULES for impellers / fans / turbines:
  - Build the hub first (circle + extrude operation="new")
  - Sketch ONE blade as a narrow rectangle or airfoil cross-section OFF-CENTER
  - Extrude with operation="join" to add it to the hub
  - circularPattern that ONE joined blade N times around Z
  - THEN add the bore (small circle + extrude operation="cut")
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
