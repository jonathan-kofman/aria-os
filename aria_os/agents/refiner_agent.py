"""RefinerAgent — interprets failures and proposes specific fixes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base_agent import BaseAgent
from .design_state import DesignState
from .ollama_config import AGENT_MODELS, CONTEXT_LIMITS

_SYSTEM_PROMPT = """You are a design refinement engineer. Given geometry validation failures and the current design parameters, propose specific fixes.

For each failure, provide:
1. The root cause
2. A specific parameter change or code modification

Output a JSON object:
{
  "instructions": "Human-readable refinement guidance for the designer",
  "parameter_overrides": {"param_name": new_value, ...}
}

Be SPECIFIC: "reduce OD_MM from 120 to 100" not "make it smaller".
Use TOOL_CALL: get_failure_fix(failure_text) to look up common fixes."""

# Deterministic lookup table for common failures — handles 80% of cases without LLM
_FAILURE_FIXES: dict[str, str] = {
    # CAD
    "not watertight":        "Simplify boolean operations. Avoid thin features and narrow cuts. Use .extrude() + .cut() instead of complex .shell().",
    "bbox.*too large":       "Reduce the outer dimensions. Check OD_MM, WIDTH_MM parameters.",
    "bbox.*too small":       "Increase dimensions. Check that extrude height and diameters match spec.",
    "bore not detected":     "Add a center bore: result.faces('>Z').workplane().circle(BORE_MM/2).cutThruAll().",
    "no axis matches od":    "The outer diameter doesn't match any bbox axis. Verify circle radius = OD_MM/2.",
    "no axis matches height":"Height mismatch — check extrude direction. CadQuery extrudes along Z by default.",
    "no axis matches width": "Width mismatch — verify .box() or .rect() first two args match spec.",
    "face_count.*low":       "Geometry is too simple. Add the required features (bores, cutouts, fillets).",
    "feature_complexity.*teeth": (
        "The part needs actual tooth geometry, not a plain cylinder. "
        "Use a for-loop to create tooth profiles as polyline triangles and union them to the base ring. "
        "Example pattern for N teeth:\n"
        "  base = cq.Workplane('XY').circle(root_r).circle(bore/2).extrude(face_w)\n"
        "  for i in range(N):\n"
        "      angle = i * 2*math.pi/N\n"
        "      # Compute 3 points: root_back, tip, root_front\n"
        "      tooth = cq.Workplane('XY').polyline([p1, p2, p3]).close().extrude(face_w)\n"
        "      base = base.union(tooth)\n"
        "  result = base\n"
        "IMPORTANT: Do NOT use .cylinder() — it doesn't exist in CadQuery. "
        "Use .circle(r).extrude(h) for cylinders."
    ),
    "feature_complexity.*toothed": (
        "Generate actual tooth profiles using polyline triangles unioned to a base ring. "
        "Do NOT use .cylinder() — use .circle(r).extrude(h). "
        "Each tooth is a triangle: 3 points at (root, tip, root) rotated by i*360/N degrees."
    ),
    "feature_complexity.*hollow": "Shell the part: outer.cut(inner_void). Don't just make a solid block.",
    "feature_complexity.*hole": "Cut holes using: result.faces('>Z').workplane().pushPoints(pts).circle(d/2).cutThruAll()",
    "Workplane.cylinder":    "CadQuery has NO .cylinder() method. Use .circle(radius).extrude(height) instead.",
    "got an unexpected keyword": "Check CadQuery API — .extrude() takes (distance), not (depth=). Remove named kwargs.",
    "No pending wires":      "The .polyline() or .moveTo() call failed. Ensure points are valid and the wire is closed before .extrude().",
    "solid_count.*disconnected": (
        "The part has multiple separate bodies that are not joined. "
        "You MUST union all features into one solid: result = base.union(pocket).union(boss). "
        "If .union() fails, the features may not be touching — move them so they overlap the base. "
        "Check that .workplane(offset=...) positions features ON the base, not floating above it."
    ),
    "STEP not readable":     "CadQuery export failed. Simplify geometry — remove complex booleans.",
    # CAM
    "undercut":              "Part has undercuts requiring 4/5-axis. Add draft angles or redesign to be 3-axis machinable.",
    "thin wall":             "Wall thickness below minimum. Increase wall_mm or add ribs.",
    "deflection":            "Tool deflection too high. Use shorter tool, reduce depth of cut, or increase tool diameter.",
    "spindle power":         "Operation exceeds machine spindle power. Reduce width of cut or depth of cut.",
    # ECAD
    "missing decoupling":    "Add 100nF ceramic cap on each MCU VCC pin. Place within 5mm of pin.",
    "trace too narrow":      "Widen trace to meet IPC-2221 current capacity. Use copper weight + current table.",
    "floating.*net":         "Net has no power source. Connect to VCC or GND via appropriate resistor/regulator.",
    # Civil
    "too sparse":            "Plan has too few entities. Add more detail: station labels, dimension lines, general notes.",
    "too few layers":        "Use proper NCS layers. Add ROAD-CENTERLINE, ANNO-TEXT, UTIL-STORM at minimum.",
    "missing.*label":        "Add text labels for all pipes, manholes, inlets with RIM/INV elevations.",
    # Physics
    "SF.*below":             "Safety factor too low. Increase wall thickness, use stronger material, or reduce applied load.",
    "stress.*exceed":        "Stress exceeds yield. Increase cross-section area or use higher-strength material.",
    "deceleration.*exceed":  "Impact deceleration too high. Add energy-absorbing features (thicker corners, softer material).",
}


class RefinerAgent(BaseAgent):
    """Interprets validation failures and proposes specific design fixes."""

    def __init__(self, repo_root: Path):
        def get_failure_fix(failure_text: str) -> str:
            """Look up deterministic fix for a known failure pattern."""
            import re
            for pattern, fix in _FAILURE_FIXES.items():
                if re.search(pattern, failure_text, re.IGNORECASE):
                    return fix
            return "No automatic fix available — use engineering judgment."

        super().__init__(
            name="RefinerAgent",
            system_prompt=_SYSTEM_PROMPT,
            model=AGENT_MODELS["refiner"],
            tools={"get_failure_fix": get_failure_fix},
            max_context_tokens=CONTEXT_LIMITS["refiner"],
        )
        self.repo_root = repo_root

    def refine(self, state: DesignState) -> None:
        """Analyze failures and populate state.refinement_instructions.

        The refiner is CODE-AWARE: it reads the actual generated code and
        proposes specific line-level fixes, not generic advice.
        """
        if not state.failures:
            state.refinement_instructions = ""
            state.parameter_overrides = {}
            return

        # Always use LLM for refinement — it reads the actual code and failures
        # to produce specific fixes. Deterministic lookup is too generic.
        prompt = self._build_code_aware_prompt(state)
        response = self.run(prompt, state)

        if response:
            try:
                json_match = _extract_json(response)
                if json_match:
                    parsed = json.loads(json_match)
                    state.refinement_instructions = parsed.get("instructions", response)
                    state.parameter_overrides = parsed.get("parameter_overrides", {})
                else:
                    state.refinement_instructions = response
            except Exception:
                state.refinement_instructions = response
        else:
            # LLM failed — fall back to deterministic
            import re
            deterministic_fixes = []
            for failure in state.failures:
                for pattern, fix in _FAILURE_FIXES.items():
                    if re.search(pattern, failure, re.IGNORECASE):
                        deterministic_fixes.append(f"- {failure} -> FIX: {fix}")
                        break
            state.refinement_instructions = (
                "Fix these issues in the next iteration:\n" +
                "\n".join(deterministic_fixes) if deterministic_fixes else
                "Previous attempt failed. Try a different approach."
            )
            print(f"  [RefinerAgent] {len(deterministic_fixes)} deterministic fixes (LLM unavailable)")
            return

        n_overrides = len(state.parameter_overrides)
        print(f"  [RefinerAgent] Code-aware refinement ({n_overrides} param overrides)")

    def _build_code_aware_prompt(self, state: DesignState) -> str:
        """Build a prompt that includes the actual generated code + failures.

        This lets the LLM pinpoint exactly which line/value needs to change,
        instead of giving generic "check your dimensions" advice.
        """
        # Truncate code to fit context
        code = state.code[:2000] if state.code else "(no code available)"

        parts = [
            f"## FAILURES from the validator\n"
            + "\n".join(f"- {f}" for f in state.failures),

            f"\n## ACTUAL GENERATED CODE (this is what produced the wrong geometry)\n"
            f"```python\n{code}\n```",

            f"\n## CURRENT BBOX: {json.dumps(state.bbox, default=str)}",
            f"\n## SPEC (what the user asked for): {json.dumps(state.spec, indent=2, default=str)}",
        ]

        if state.plan.get("build_recipe"):
            parts.append(f"\n## BUILD RECIPE (what the geometry should look like):\n{state.plan['build_recipe'][:1000]}")

        parts.append(
            "\n## YOUR TASK\n"
            "Read the actual code above. Find the SPECIFIC lines that cause the failures.\n"
            "Tell the designer EXACTLY what to change — which variable, which value, which line.\n"
            "Example: 'Change .extrude(10) on line 15 to .extrude(4) so the pocket stays within the 6mm base thickness'\n"
            "Output JSON: {\"instructions\": \"specific fix instructions\", \"parameter_overrides\": {\"key\": value}}"
        )

        return "\n".join(parts)


def _extract_json(text: str) -> str | None:
    """Extract first JSON object from text."""
    import re
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    return match.group(0) if match else None
