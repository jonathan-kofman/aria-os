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
to decide whether the prompt is already specific enough to design OR
has a GENUINELY AMBIGUOUS critical field that must be clarified.

DEFAULT BIAS: enough_info=true. Only ask when the prompt is genuinely
under-specified in a way that would change the geometry or material.

NEVER ask about:
  - Pressure class (unless prompt mentions pipe, pump, pressure, psi,
    bar, vacuum, or a service like "steam/gas/oil flange")
  - Hub type / face type / gasket (unless it's a pipeline flange)
  - PCB impedance control (unless prompt mentions RF, high-speed,
    controlled impedance, diff pair, USB3+, HDMI, DDR)
  - Component sourcing / fab house (unless prompt requests it)

OK to ask when genuinely missing:
  - Material if the prompt doesn't name one AND the part type cares
    (e.g. a bracket with no material given)
  - Thread type/clearance when ambiguous (M6 is clear — clearance hole)
  - Thickness if not specified and strongly affects design
  - Mounting/orientation for brackets without a clear direction
  - Layer count for a PCB without any hint

Examples of prompts that need NO clarification (enough_info=true):
  - "flange 100mm OD, 4 M6 holes on 80mm PCD, 6mm thick, 6061 aluminum"
    → structural flange, every dim + material named, go.
  - "impeller 120mm OD, 6 backward-curved blades, 20mm bore"
    → blade count, sweep, OD, bore all given, go.
  - "PCB for ESP32 with USB-C and 2 status LEDs, 30x40mm"
    → board size, MCU, connectors given, go.
  - "L-bracket 80mm wide, 60mm tall, 40mm deep, 5mm thick, 4 M6 mounting holes"
    → all dims + hole pattern + fastener size given, go.

Examples that genuinely need clarification:
  - "make me a flange for my pump" → needs OD, bolt pattern, material
  - "breakout board for STM32" → needs board size, connectors, layer count
  - "motor mount" → needs motor frame size, orientation, mounting hole pattern

Return JSON:
  {
    "enough_info": bool,
    "part_family": str,
    "summary": str,
    "clarifications": [
      {"field": str, "question": str, "options": [str],
       "default": str, "rationale": str}
    ]
  }

Output JSON ONLY — no prose, no markdown fences.
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


def clarify(goal: str, spec: dict | None = None,
             *, quality: str = "fast",
             repo_root: Path | None = None) -> dict:
    """Ask the LLM to review the prompt and list critical missing fields.

    Returns a dict with:
        enough_info: bool
        part_family: str
        summary: str
        clarifications: list[dict]

    On LLM failure, returns enough_info=True (proceed as-is) so we
    never block on clarifier errors.
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
        return {"enough_info": True,
                "part_family": "unknown",
                "summary": goal,
                "clarifications": [],
                "error": f"clarify LLM failed: {exc}"}
    if not raw:
        return {"enough_info": True,
                "part_family": "unknown",
                "summary": goal,
                "clarifications": []}
    data = _extract_json_object(raw)
    if not data:
        return {"enough_info": True,
                "part_family": "unknown",
                "summary": goal,
                "clarifications": [],
                "error": f"unparseable clarify output: {raw[:200]!r}"}
    # Normalize
    data.setdefault("enough_info", False)
    data.setdefault("part_family", "unknown")
    data.setdefault("summary", goal)
    data.setdefault("clarifications", [])
    if not isinstance(data["clarifications"], list):
        data["clarifications"] = []
    # Cap at 5 questions
    data["clarifications"] = data["clarifications"][:5]
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
    data["clarifications"] = cleaned
    if not cleaned:
        data["enough_info"] = True
    return data
