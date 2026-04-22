"""LLM-driven ECAD planner — emits PCB feature ops for arbitrary
circuits (not just the hardcoded LED demo).

Targets the KiCad executor's op set (`beginBoard`, `setStackup`,
`addNet`, `placeComponent`, `addTrack`, `addVia`, `addZone`,
`routeBoard`). Uses the structured-output path where possible so
valid JSON is guaranteed.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..llm_client import call_llm
from .structured_llm import plan_from_llm_structured


_ECAD_OPS_SCHEMA = r"""
Emit a JSON array of PCB feature operations. Each op has:
  - kind:    one of beginBoard | setStackup | addNet | placeComponent
             | addTrack | addVia | addZone | routeBoard
  - params:  kind-specific (see below)
  - label:   short human description

Op kinds and their params:

  beginBoard:      {width_mm: float, height_mm: float, layers: int, name: str}
  setStackup:      {layers: [str], dielectric_mm: float, material: "FR4"}
  addNet:          {name: str}
  placeComponent:  {ref: str, footprint: "LIB:NAME", x_mm: float, y_mm: float,
                    rot_deg: float, layer: "F.Cu"|"B.Cu"}
  addTrack:        {net: str, x1_mm, y1_mm, x2_mm, y2_mm, width_mm, layer}
  addVia:          {net: str, x_mm, y_mm, drill_mm, diameter_mm}
  addZone:         {net: str, layer: "F.Cu"|"B.Cu", polygon: [[x,y],...]}
  routeBoard:      {timeout_s: int}   -- auto-routes all pending nets

Rules:
  1. First op MUST be beginBoard.
  2. Use setStackup right after beginBoard for 2+ layer boards.
  3. Declare every named net via addNet BEFORE any track/via references it.
  4. Place components inside the board outline.
  5. Standard footprints: `Connector_USB:USB_C_Receptacle`,
     `Resistor_SMD:R_0603_1608Metric`, `Capacitor_SMD:C_0603_1608Metric`,
     `LED_SMD:LED_0805_2012Metric`, `Package_QFP:LQFP-48_7x7mm_P0.5mm`.
  6. End with routeBoard to let Freerouting fill tracks automatically,
     OR emit explicit addTrack ops for critical signals.
  7. Return ONLY the JSON array. No prose.
""".strip()


_ECAD_FEW_SHOT = r"""
## Example (for "3.3V regulator on USB-C, 30x20mm 2-layer")
[
  {"kind": "beginBoard", "params": {"width_mm": 30, "height_mm": 20, "layers": 2, "name": "LDO Board"}, "label": "New 30×20 board"},
  {"kind": "setStackup", "params": {"layers": ["F.Cu", "B.Cu"], "dielectric_mm": 1.6, "material": "FR4"}, "label": "2-layer FR-4"},
  {"kind": "addNet", "params": {"name": "VBUS"}, "label": "Net: VBUS"},
  {"kind": "addNet", "params": {"name": "3V3"}, "label": "Net: 3V3"},
  {"kind": "addNet", "params": {"name": "GND"}, "label": "Net: GND"},
  {"kind": "placeComponent", "params": {"ref": "J1", "footprint": "Connector_USB:USB_C_Receptacle", "x_mm": 3, "y_mm": 10, "rot_deg": 0, "layer": "F.Cu"}, "label": "Place J1 USB-C"},
  {"kind": "placeComponent", "params": {"ref": "U1", "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "x_mm": 15, "y_mm": 10, "rot_deg": 0, "layer": "F.Cu"}, "label": "Place U1 LDO"},
  {"kind": "placeComponent", "params": {"ref": "C1", "footprint": "Capacitor_SMD:C_0603_1608Metric", "x_mm": 10, "y_mm": 15, "rot_deg": 0, "layer": "F.Cu"}, "label": "Place C1 input cap"},
  {"kind": "placeComponent", "params": {"ref": "C2", "footprint": "Capacitor_SMD:C_0603_1608Metric", "x_mm": 20, "y_mm": 15, "rot_deg": 0, "layer": "F.Cu"}, "label": "Place C2 output cap"},
  {"kind": "addZone", "params": {"net": "GND", "layer": "B.Cu", "polygon": [[0,0],[30,0],[30,20],[0,20]]}, "label": "GND pour on B.Cu"},
  {"kind": "routeBoard", "params": {"timeout_s": 90}, "label": "Auto-route all nets"}
]
""".strip()


from .engineering_prompt import ENGINEERING_PRACTICE_PROMPT

_ECAD_SYSTEM_PROMPT = (
    "You are a senior electrical engineer writing PCB feature plans for "
    "ARIA. You convert a natural-language circuit description into an "
    "ordered list of KiCad feature operations. Output is JSON ONLY — no "
    "prose, no markdown, no commentary. Every element MUST be an object "
    "`{kind, params, label}`.\n\n"
    + ENGINEERING_PRACTICE_PROMPT + "\n\n"
    + _ECAD_OPS_SCHEMA + "\n\n" + _ECAD_FEW_SHOT)


def plan_ecad_from_llm(goal: str, spec: dict,
                        *, quality: str = "balanced",
                        repo_root: Path | None = None,
                        host_context: dict | None = None) -> list[dict]:
    """LLM-driven PCB planner. Tries structured output first, falls back
    to free-text + tolerant parse."""
    user_prompt = (
        f"## Circuit description\n{goal.strip()}\n\n"
        f"## Parsed spec\n{json.dumps(spec, indent=2, default=str)}\n\n"
        "Produce the JSON feature-op array now."
    )
    # Structured output path (Anthropic tool_use / Gemini responseSchema)
    structured = plan_from_llm_structured(
        user_prompt, _ECAD_SYSTEM_PROMPT,
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
    # Free-text fallback
    raw = call_llm(user_prompt, _ECAD_SYSTEM_PROMPT,
                    quality=quality, repo_root=repo_root)
    if not raw:
        raise ValueError("No LLM backend available for ECAD planning")
    from .llm_planner import _extract_json_array
    plan = _extract_json_array(raw)
    if not plan:
        raise ValueError(
            f"ECAD LLM returned no parseable plan: {raw[:200]!r}")
    return plan
