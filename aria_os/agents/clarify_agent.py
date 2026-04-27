"""Clarifying-question agent.

Before the planner runs, this agent looks at the user's prompt + any
spec info already extracted and asks: "what production-critical fields
are missing that a real engineer would specify?"

The LLM returns 0–5 questions, each with:
  - field name (e.g. "flange_type")
  - question text (e.g. "What flange hub style?")
  - options (e.g. ["weld_neck", "slip_on", "blind"])
  - default (our best-guess smart default)
  - rationale (why we need it)

The panel renders these as an inline form. User picks/edits → we
re-submit with the rich spec so the planner emits a production-grade
part instead of a toy one.

Design choice: LLM decides what to ask, not a hardcoded schema. This
way adding a new part type (gasket, coupling, enclosure, PCB variant)
doesn't require any Python changes — the LLM handles it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..llm_client import call_llm


_CLARIFY_SYSTEM = r"""
You are a senior engineer reviewing a part-design prompt. Your job is
to ask the SMALLEST useful set of questions (0–6) that would let a
mechanical or electrical engineer fully constrain the design.

ALWAYS COVER (when not already explicit in the prompt):
  - Indoor vs outdoor / operating environment. Outdoor implies UV,
    rain, temperature swings, IP rating, corrosion — material + finish
    decisions hinge on this. NEVER skip this question if the prompt
    doesn't say "indoor", "outdoor", "subsea", "in-space", or similar.
  - Load case / payload / weight the part must carry, when structural.
  - Material constraint, if the prompt names neither material nor a
    process that implies one (e.g. "machined-from-billet" → metal).
  - Quantity (one-off vs production) when the answer would change the
    fab method (3D-print → CNC → mold).
  - Lead-time or deadline when the answer would change the fab method.
  - Regulatory / safety scope when the part touches food, medical,
    aerospace, automotive crash, pressure-vessel, or load-bearing
    structural service (call it out explicitly).

NEVER ask about:
  - Pressure class (unless prompt mentions pipe, pump, pressure, psi,
    bar, vacuum, or a service like "steam/gas/oil flange").
  - Hub type / face type / gasket (unless it's a pipeline flange).
  - PCB impedance control (unless prompt mentions RF, high-speed,
    controlled impedance, diff pair, USB3+, HDMI, DDR).
  - Component sourcing / fab house (unless prompt requests it).
  - Color, brand, finish-only aesthetics — those are downstream of
    material + environment.

Set enough_info=true ONLY when the prompt carries enough explicit
constraints that asking would just be friction:
  - 3+ numeric dimensions AND one of (material, environment, regulatory).
  - The prompt names a hardcoded planner family with all required
    fields explicit.

Examples that need NO clarification (enough_info=true):
  - "flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick, 6061 aluminum,
    indoor industrial use"
  - "impeller 120mm OD, 6 backward-curved blades, 20mm bore, ABS, indoor"
  - "PCB for ESP32 with USB-C and 2 status LEDs, 30x40mm, indoor"
  - "L-bracket 80mm wide, 60mm tall, 40mm deep, 5mm thick, 4 M6 mounting
    holes, stainless steel, outdoor marine"

Examples that genuinely need clarification (enough_info=false):
  - "make me a flange for my pump" → asks: indoor vs outdoor, OD, bolt
    pattern, material, pressure class.
  - "breakout board for STM32" → asks: indoor vs outdoor, board size,
    connectors, layer count, regulatory scope.
  - "motor mount" → asks: indoor/outdoor, motor frame size, orientation,
    payload, material.
  - "drone frame" → asks: indoor (FPV race) vs outdoor, payload weight,
    propeller size, material (carbon fibre vs printed nylon).

Return JSON only — no prose, no markdown fences:
  {
    "enough_info": bool,
    "part_family": str,
    "summary": str,
    "clarifications": [
      {"field": str, "question": str, "options": [str],
       "default": str, "rationale": str}
    ]
  }
""".strip()


def _extract_json_object(text: str) -> dict | None:
    """Tolerant JSON-object extractor — same pattern as the planner's."""
    # Direct parse
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Strip markdown fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # Balance-match first {...}
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i+1])
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        pass
                    break
    return None


def _baseline_must_haves(goal: str) -> list[dict]:
    """Deterministic must-have questions used when the LLM bails out
    OR returns a list missing the always-cover items. Lifts logic from
    aria_os.clarify.clarify._baseline_questions but in the
    {field, question, options, default, rationale} shape this module
    contracts to."""
    try:
        from aria_os.clarify.clarify import _baseline_questions
    except Exception:
        return []
    out = []
    for q in _baseline_questions(goal):
        out.append({
            "field":     q["id"],
            "question":  q["label"],
            "options":   list(q.get("options") or []),
            "default":   "",
            "rationale": q.get("hint", ""),
        })
    return out


def _has_environment_question(items: list[dict]) -> bool:
    for c in items:
        f = (c.get("field") or "").lower()
        q = (c.get("question") or "").lower()
        if f in ("environment", "indoor_outdoor", "operating_environment",
                  "use_environment", "in_outdoor"):
            return True
        if "indoor" in q and "outdoor" in q: return True
        if "operating environment" in q: return True
    return False


def clarify(goal: str, spec: dict | None = None,
             *, quality: str = "fast",
             repo_root: Path | None = None) -> dict:
    """Ask the LLM to review the prompt and list critical missing fields.

    Returns a dict with:
        enough_info: bool
        part_family: str
        summary: str
        clarifications: list[dict]

    Self-healing: if the LLM bails out OR returns a list missing the
    "always cover" axes (chiefly indoor/outdoor), we merge in the
    deterministic baseline so the user is never silently shipped a
    half-defined prompt. Per the autonomy-first rule the recovery is
    invisible to the caller.
    """
    prompt = (
        f"## User's part description\n{goal.strip()}\n\n"
        f"## Already-extracted spec\n"
        f"{json.dumps(spec or {}, indent=2, default=str)}\n\n"
        "Emit the clarification JSON now."
    )
    try:
        raw = call_llm(prompt, _CLARIFY_SYSTEM,
                        quality=quality, repo_root=repo_root)
    except Exception as exc:
        # LLM failed — fall back to the deterministic baseline.
        baseline = _baseline_must_haves(goal)
        return {"enough_info": len(baseline) == 0,
                "part_family": "unknown",
                "summary": goal,
                "clarifications": baseline,
                "error": f"clarify LLM failed: {exc}"}
    if not raw:
        baseline = _baseline_must_haves(goal)
        return {"enough_info": len(baseline) == 0,
                "part_family": "unknown",
                "summary": goal,
                "clarifications": baseline}
    data = _extract_json_object(raw)
    if not data:
        baseline = _baseline_must_haves(goal)
        return {"enough_info": len(baseline) == 0,
                "part_family": "unknown",
                "summary": goal,
                "clarifications": baseline,
                "error": f"unparseable clarify output: {raw[:200]!r}"}
    # Normalize
    data.setdefault("enough_info", False)
    data.setdefault("part_family", "unknown")
    data.setdefault("summary", goal)
    data.setdefault("clarifications", [])
    if not isinstance(data["clarifications"], list):
        data["clarifications"] = []
    # Cap at 6 questions (was 5 — bumped to make room for env + load).
    data["clarifications"] = data["clarifications"][:6]
    # Ensure each clarification has minimum shape
    cleaned = []
    for c in data["clarifications"]:
        if not isinstance(c, dict):
            continue
        if "field" not in c or "question" not in c:
            continue
        cleaned.append({
            "field":     str(c["field"]),
            "question":  str(c["question"]),
            "options":   list(c.get("options") or []),
            "default":   c.get("default", ""),
            "rationale": str(c.get("rationale", "")),
        })
    # Self-healing: if the LLM didn't ask about indoor/outdoor and the
    # prompt doesn't already specify it, splice the env question to the
    # front. This is the single most-often-missed clarification.
    pl = goal.lower()
    try:
        from aria_os.clarify.clarify import _ENV_WORDS
    except Exception:
        _ENV_WORDS = {"indoor", "outdoor", "subsea", "underwater"}
    env_in_prompt = any(w in pl for w in _ENV_WORDS)
    if not data.get("enough_info") and not env_in_prompt \
            and not _has_environment_question(cleaned):
        cleaned.insert(0, {
            "field":     "environment",
            "question":  "Will this be used indoors, outdoors, or both?",
            "options":   ["indoor", "outdoor", "both",
                            "subsea / marine", "in-space / vacuum"],
            "default":   "",
            "rationale": "Drives material, finish, IP rating, and "
                          "tolerance bracket. Always asked when not "
                          "specified.",
        })
        cleaned = cleaned[:6]
    data["clarifications"] = cleaned
    if not cleaned:
        data["enough_info"] = True
    return data
