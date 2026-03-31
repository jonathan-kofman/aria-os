"""SpecAgent — extracts dimensional constraints, material, and physics requirements."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base_agent import BaseAgent
from .design_state import DesignState
from .ollama_config import AGENT_MODELS, CONTEXT_LIMITS

_SYSTEM_PROMPT = """You are a specification extraction agent. Given a natural language engineering description, extract structured constraints.

Output a JSON object with these fields (omit any that aren't specified):
{
  "od_mm": <outer diameter in mm>,
  "bore_mm": <inner bore diameter in mm>,
  "height_mm": <height/thickness in mm>,
  "width_mm": <width in mm>,
  "depth_mm": <depth in mm>,
  "length_mm": <length in mm>,
  "wall_mm": <wall thickness in mm>,
  "n_teeth": <tooth count>,
  "n_bolts": <bolt count>,
  "bolt_dia_mm": <bolt diameter in mm>,
  "material": <material name>,
  "part_type": <type like "bracket", "housing", "phone_case", "gear">
}

Use TOOL_CALL: extract_dimensions(goal) to get regex-based extraction as a starting point.
Use TOOL_CALL: resolve_cem(goal, part_id, params_json) to get physics-derived parameters.

Output ONLY the JSON object, no other text."""


class SpecAgent(BaseAgent):
    """Extracts structured design spec from natural language goal."""

    def __init__(self, repo_root: Path, tools: dict | None = None):
        super().__init__(
            name="SpecAgent",
            system_prompt=_SYSTEM_PROMPT,
            model=AGENT_MODELS["spec"],
            tools=tools or {},
            max_context_tokens=CONTEXT_LIMITS["spec"],
        )
        self.repo_root = repo_root

    def extract(self, state: DesignState) -> None:
        """Run spec extraction and populate state.spec + state.cem_params."""
        import json

        # Always run deterministic extraction first (fast, reliable)
        try:
            from ..spec_extractor import extract_spec
            regex_spec = extract_spec(state.goal)
            state.spec.update(regex_spec)
        except Exception:
            regex_spec = {}

        # Try CEM resolution
        try:
            from ..cem_generator import resolve_and_compute
            cem_result = resolve_and_compute(
                state.goal, state.part_id, state.spec, self.repo_root)
            if cem_result:
                state.cem_params.update(cem_result)
                # Inject CEM params into spec without overwriting user values
                for k, v in cem_result.items():
                    if k != "part_family" and k not in state.spec:
                        state.spec[k] = v
        except Exception:
            pass

        # If regex extraction got reasonable results, skip LLM (saves time)
        if len(regex_spec) >= 3:
            print(f"  [SpecAgent] Extracted {len(state.spec)} params via regex (skipping LLM)")
            return

        # LLM enrichment for complex/ambiguous goals
        try:
            prompt = f"Extract specifications from: {state.goal}\n\nRegex extracted: {json.dumps(regex_spec)}"
            response = self.run(prompt, state)

            # Parse JSON from response
            json_match = _extract_json(response)
            if json_match:
                llm_spec = json.loads(json_match)
                # LLM values only fill gaps (don't override regex or user values)
                for k, v in llm_spec.items():
                    if k not in state.spec or state.spec[k] is None:
                        state.spec[k] = v
                print(f"  [SpecAgent] LLM enriched to {len(state.spec)} params")
            else:
                print(f"  [SpecAgent] LLM response not parseable, using regex only")
        except Exception as exc:
            print(f"  [SpecAgent] LLM failed ({exc}), using regex only")

        # Detect material from goal if not in spec
        if "material" not in state.spec:
            try:
                from ..physics_analyzer import _detect_material_from_goal
                state.material = _detect_material_from_goal(state.goal, state.spec)
                state.spec["material"] = state.material
            except Exception:
                pass
        else:
            state.material = str(state.spec.get("material", ""))


def _extract_json(text: str) -> str | None:
    """Extract first JSON object from text."""
    import re
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    return match.group(0) if match else None
