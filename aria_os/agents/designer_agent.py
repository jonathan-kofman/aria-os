"""DesignerAgent — generates domain-specific code (CadQuery, CAM, ECAD, etc.)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base_agent import BaseAgent
from .design_state import DesignState
from .domains import DESIGNER_PROMPTS
from .ollama_config import DESIGNER_MODELS, CONTEXT_LIMITS


class DesignerAgent(BaseAgent):
    """Generates code for the target domain using Ollama."""

    def __init__(self, domain: str, repo_root: Path, tools: dict | None = None):
        self.domain = domain
        self.repo_root = repo_root
        super().__init__(
            name=f"DesignerAgent[{domain}]",
            system_prompt=DESIGNER_PROMPTS.get(domain, DESIGNER_PROMPTS["cad"]),
            model=DESIGNER_MODELS.get(domain, "qwen2.5-coder:7b"),
            tools=tools or {},
            max_context_tokens=CONTEXT_LIMITS["designer"],
            fallback_to_cloud=True,  # designer is the most critical agent
        )
        self._prefer_cloud = True  # For code gen, cloud LLMs are far better than 7b

    def generate(self, state: DesignState) -> None:
        """Generate code and populate state.code, state.output_path, state.bbox.

        Strategy:
        1. Check if a CadQuery template exists for this part type — if so, use it
           directly with the agent-extracted params (instant, reliable).
        2. If no template or template output fails eval, fall back to LLM generation.
        The agent is still agentic: SpecAgent extracts params, EvalAgent validates.
        """
        if self.domain == "cad" and state.iteration <= 1 and not state.refinement_instructions:
            # ALWAYS try template first — templates are more reliable than LLM
            template_used = self._try_template(state)
            if template_used:
                return

            # No template — try CADSmith iterative loop for complex parts
            print(f"  [{self.name}] No template — trying CADSmith loop")
            try:
                from ..generators.cadsmith_generator import cadsmith_generate
                _cs_plan = {"part_id": state.part_id or "agent_part", "params": state.spec}
                _cs_result = cadsmith_generate(
                    state.goal, _cs_plan,
                    str(state.repo_root / "outputs" / "cad" / "step" / f"{state.part_id or state.session_id}.step"),
                    str(state.repo_root / "outputs" / "cad" / "stl" / f"{state.part_id or state.session_id}.stl"),
                    repo_root=state.repo_root,
                )
                if _cs_result.get("step_path") and Path(_cs_result["step_path"]).exists():
                    state.artifacts.update(_cs_result)
                    print(f"  [{self.name}] CADSmith generated geometry")
                    return
            except Exception as _cs_err:
                print(f"  [{self.name}] CADSmith failed: {_cs_err}, falling back to LLM")

        # LLM-based generation
        # Build the user prompt from state
        prompt_parts = [
            f"## Design Request\n{state.goal}\n",
            f"## Specifications\n{json.dumps(state.spec, indent=2, default=str)}\n",
        ]

        # Include build recipe if available (from Coordinator Phase 2)
        build_recipe = state.plan.get("build_recipe", "")
        if build_recipe:
            prompt_parts.append(
                f"## BUILD RECIPE (follow these steps EXACTLY)\n"
                f"The Coordinator Agent analyzed research and created this step-by-step recipe.\n"
                f"Translate each step into CadQuery Python code:\n\n"
                f"{build_recipe[:6000]}\n"
            )

        # Inject CadQuery operations reference (goal-specific)
        try:
            from ..cad_operations_reference import get_operations_for_goal
            ops_ref = get_operations_for_goal(state.goal)
            if ops_ref:
                prompt_parts.append(ops_ref)
        except Exception:
            pass

        # Inject closest template as reference (highest ROI for LLM quality)
        ref_code = state.plan.get("_reference_template_code", "")
        ref_name = state.plan.get("_reference_template_name", "")
        if not ref_code:
            try:
                from ..generators.cadquery_generator import _get_closest_template_source
                ref_name, ref_code = _get_closest_template_source(
                    state.goal, state.part_id or "", state.spec)
            except Exception:
                pass
        if ref_code:
            prompt_parts.append(
                f"## REFERENCE TEMPLATE (adapt this working code for your part)\n"
                f"This is a TESTED CadQuery script for a similar part type ('{ref_name}').\n"
                f"Use the SAME CadQuery patterns and structure — modify dimensions and features:\n\n"
                f"```python\n{ref_code}\n```\n\n"
                f"IMPORTANT: This reference code WORKS. Keep the same patterns:\n"
                f"- All dimensions as named constants at the top\n"
                f"- Build solid first, then cuts/holes\n"
                f"- result = ... as the final variable\n"
                f"- bb = result.val().BoundingBox() at the end\n"
                f"- NEVER use .cylinder() — use .circle(r).extrude(h)\n"
            )
            print(f"  [{self.name}] Injecting reference template: {ref_name}")

        # Include web research context if available
        research = state.plan.get("research_context", "")
        if research and not build_recipe:
            # Only include raw research if no build recipe (recipe already incorporates research)
            prompt_parts.append(
                f"## Reference Information (from web research)\n"
                f"Use these real-world specs and design features as guidance:\n"
                f"{research[:2000]}\n"
            )

        if state.cem_params:
            prompt_parts.append(
                f"## Physics Parameters (CEM)\n{json.dumps(state.cem_params, indent=2, default=str)}\n"
            )

        if state.refinement_instructions:
            prompt_parts.append(
                f"## REFINEMENT FROM PREVIOUS ATTEMPT\n"
                f"The previous attempt had these failures. Fix them:\n"
                f"{state.refinement_instructions}\n"
            )
            if state.parameter_overrides:
                prompt_parts.append(
                    f"## Parameter Overrides\n"
                    f"Apply these specific changes:\n"
                    f"{json.dumps(state.parameter_overrides, indent=2, default=str)}\n"
                )

        prompt = "\n".join(prompt_parts)

        # Store iteration so _call_llm can route correctly
        self._current_iteration = state.iteration

        # Call the LLM
        response = self.run(prompt, state)

        if not response:
            # LLM failed — try Zoo.dev text-to-CAD before giving up
            if self.domain == "cad" and self._try_zoo(state):
                return
            state.generation_error = "DesignerAgent: LLM returned empty response, Zoo.dev fallback failed or unavailable"
            return

        # Extract code from response (JSON-first, then markdown, then raw)
        code = _extract_code(response)
        if not code:
            # LLM returned garbage — try Zoo.dev text-to-CAD before giving up
            if self.domain == "cad" and self._try_zoo(state):
                return
            state.generation_error = "DesignerAgent: no code block in LLM response, Zoo.dev fallback failed or unavailable"
            # Do NOT store raw response as state.code — it poisons the RefinerAgent
            # which tries to "fix" a non-code string as if it were Python
            return

        # Post-process: Gemini/Gemma sometimes define functions but never call them,
        # or build geometry without assigning to `result`. Fix common patterns.
        if "result" not in code and self.domain == "cad":
            # Try to find the last CadQuery variable assignment and alias it
            import re as _re
            # Match patterns like: solid = cq.Workplane... or base = base.union(...)
            _assignments = _re.findall(r'^(\w+)\s*=\s*(?:cq\.|.*\.union|.*\.cut|.*\.shell|.*\.fillet)', code, _re.MULTILINE)
            if _assignments:
                last_var = _assignments[-1]
                code += f"\nresult = {last_var}\n"
                code += 'bb = result.val().BoundingBox()\n'
                code += 'print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")\n'

        state.code = code
        state.generation_error = ""

        # For CAD domain: pre-check spec vs code before spending compute on execution.
        # If critical mismatches are found (wrong count, missing dimension), regenerate
        # once with explicit correction notes embedded in the prompt.
        if self.domain == "cad" and not state.refinement_instructions:
            spec_issues = _precheck_code_spec(code, state.spec)
            if spec_issues:
                print(f"  [{self.name}] Spec/code mismatch on iter {state.iteration} — regenerating:")
                for issue in spec_issues:
                    print(f"    ! {issue}")
                correction_prompt = (
                    "\n".join(prompt_parts)
                    + "\n\n## CRITICAL CORRECTIONS FOR THIS ATTEMPT\n"
                    + "The previous code had these spec mismatches that MUST be fixed:\n"
                    + "\n".join(f"- {issue}" for issue in spec_issues)
                    + "\n\nGenerate corrected code that addresses all of the above."
                )
                corrected_response = self.run(correction_prompt, state)
                corrected_code = _extract_code(corrected_response) if corrected_response else ""
                if corrected_code:
                    # Verify correction improved things (fewer issues)
                    remaining = _precheck_code_spec(corrected_code, state.spec)
                    if len(remaining) < len(spec_issues):
                        print(f"  [{self.name}] Correction reduced issues: {len(spec_issues)} → {len(remaining)}")
                        code = corrected_code
                        state.code = code
                    else:
                        print(f"  [{self.name}] Correction did not improve — keeping original")

        # For CAD domain: execute the code to produce STEP/STL
        if self.domain == "cad":
            self._execute_cad(state, code)

    def _call_llm(self, prompt: str) -> str | None:
        """Override: for CAD code generation, use cloud LLMs (local 7b too unreliable).

        Iteration 1 — Anthropic first (highest quality for initial generation).
        Iteration 2+ — Gemini first (faster/cheaper for refinement; Claude as fallback).
        Gemma 4 31B is tried last if both cloud providers fail.
        """
        if self._prefer_cloud and self.domain == "cad":
            iteration = getattr(self, "_current_iteration", 1)
            from ..llm_client import _try_anthropic, _try_gemini, _try_gemma

            if iteration <= 1:
                # Iter 1: best quality matters most — Claude first
                providers = [
                    ("Anthropic", lambda: _try_anthropic(prompt, self.system_prompt)),
                    ("Gemini",    lambda: _try_gemini(prompt, self.system_prompt)),
                    ("Gemma4",    lambda: _try_gemma(prompt, self.system_prompt)),
                ]
            else:
                # Iter 2+: refinement — Gemini is faster and cheaper, Claude as backup
                providers = [
                    ("Gemini",    lambda: _try_gemini(prompt, self.system_prompt)),
                    ("Anthropic", lambda: _try_anthropic(prompt, self.system_prompt)),
                    ("Gemma4",    lambda: _try_gemma(prompt, self.system_prompt)),
                ]

            for name, fn in providers:
                try:
                    response = fn()
                    if response:
                        return response
                except Exception:
                    pass

            print(f"  [{self.name}] All cloud LLMs unavailable — falling back to template")
            return None
        # Non-CAD domains: use Ollama (standard path)
        return super()._call_llm(prompt)

    def _try_template(self, state: DesignState) -> bool:
        """Try to generate using a CadQuery template with agent-extracted params.
        Returns True if successful (state populated), False to fall back to LLM.
        For fuzzy matches, stores closest template as LLM reference (not executed directly)."""
        try:
            from ..generators.cadquery_generator import _find_template_fuzzy, _get_closest_template_source

            part_type = state.spec.get("part_type", "")
            part_id = state.part_id or ""

            print(f"  [{self.name}] Template check: part_id='{part_id}', part_type='{part_type}'")

            # Fuzzy matching: exact/keyword matches get direct execution,
            # goal/fuzzy matches store reference for LLM prompt
            template_fn, match_type = _find_template_fuzzy(
                part_type or part_id, goal=state.goal, spec=state.spec)

            if not template_fn:
                return False

            # Fuzzy matches are unreliable for direct execution — use as LLM reference only
            if match_type == "fuzzy":
                ref_name, ref_code = _get_closest_template_source(
                    state.goal, part_id, state.spec)
                if ref_code:
                    state.plan["_reference_template_name"] = ref_name
                    state.plan["_reference_template_code"] = ref_code
                    print(f"  [{self.name}] Fuzzy match → storing '{ref_name}' as LLM reference")
                return False

            # Sanitize spec: remove None values so template defaults kick in
            _safe_spec = {k: v for k, v in state.spec.items() if v is not None}
            # Generate code using the template with agent-extracted params
            code = template_fn(_safe_spec)
            if not code or len(code) < 50:
                return False

            print(f"  [{self.name}] Using template for '{part_type or part_id}' with agent params")

            state.code = code
            state.generation_error = ""
            self._execute_cad(state, code)

            # Check if execution succeeded
            if state.generation_error:
                print(f"  [{self.name}] Template execution failed: {state.generation_error}")
                state.generation_error = ""  # clear for LLM retry
                return False

            return True

        except Exception as exc:
            print(f"  [{self.name}] Template lookup failed: {exc}")
            return False

    def _try_zoo(self, state: DesignState) -> bool:
        """Try Zoo.dev text-to-CAD API as fallback when LLMs fail.
        Returns True if Zoo produced a STEP file successfully."""
        try:
            from ..zoo_bridge import is_zoo_available, generate_step_from_zoo
            if not is_zoo_available(self.repo_root):
                return False

            print(f"  [{self.name}] LLMs unavailable — trying Zoo.dev text-to-CAD")
            from ..exporter import get_output_paths
            paths = get_output_paths(state.part_id or state.goal, self.repo_root)
            step_dir = str(Path(paths["step_path"]).parent)

            result = generate_step_from_zoo(state.goal, step_dir, repo_root=self.repo_root)
            if result.get("status") != "ok":
                print(f"  [{self.name}] Zoo.dev failed: {result.get('error', 'unknown')}")
                return False

            step_path = result["step_path"]
            if not Path(step_path).exists():
                print(f"  [{self.name}] Zoo.dev reported success but STEP file not found: {step_path}")
                return False
            state.output_path = step_path
            state.generation_error = ""
            # Parse bbox from STEP if possible
            try:
                import trimesh
                mesh = trimesh.load(step_path)
                bb = mesh.bounding_box.extents
                state.bbox = {"x": round(float(bb[0]), 2), "y": round(float(bb[1]), 2), "z": round(float(bb[2]), 2)}
                print(f"  [{self.name}] Zoo.dev STEP: bbox {state.bbox}")
            except Exception:
                pass

            print(f"  [{self.name}] Zoo.dev generated STEP successfully")
            return True
        except Exception as exc:
            print(f"  [{self.name}] Zoo.dev fallback failed: {exc}")
            return False

    def _execute_cad(self, state: DesignState, code: str) -> None:
        """Execute CadQuery code and capture output files + bbox."""
        from ..exporter import get_output_paths

        paths = get_output_paths(state.part_id or state.goal, self.repo_root)
        step_path = paths["step_path"]
        stl_path = paths["stl_path"]

        # Add export footer to code
        export_code = code + f"""

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = r"{step_path}"
_stl  = r"{stl_path}"
try:
    _os.makedirs(_os.path.dirname(_step), exist_ok=True)
except OSError:
    pass
try:
    _os.makedirs(_os.path.dirname(_stl), exist_ok=True)
except OSError:
    pass
_exp.export(result, _step, _exp.ExportTypes.STEP)
_exp.export(result, _stl,  _exp.ExportTypes.STL)
"""

        # Execute in sandbox (permissive — code needs import, os, Path)
        import cadquery as cq
        import math
        import os as _os_mod
        ns: dict[str, Any] = {
            "cq": cq,
            "math": math,
            "os": _os_mod,
            "Path": Path,
            "__builtins__": __builtins__,
        }

        try:
            exec(compile(export_code, f"<{state.part_id}_agent>", "exec"), ns)

            # Capture bbox from the result object
            result_obj = ns.get("result")
            if result_obj is not None:
                bb = result_obj.val().BoundingBox()
                state.bbox = {"x": round(bb.xlen, 3), "y": round(bb.ylen, 3),
                              "z": round(bb.zlen, 3)}

            state.output_path = step_path
            state.artifacts = {"step_path": step_path, "stl_path": stl_path}
            print(f"  [{self.name}] Generated STEP ({Path(step_path).stat().st_size // 1024}KB) "
                  f"bbox: {state.bbox}")

        except Exception as exc:
            state.generation_error = str(exc)[:500]
            print(f"  [{self.name}] Execution failed: {state.generation_error}")


def _precheck_code_spec(code: str, spec: dict) -> list[str]:
    """Scan generated CadQuery code for obvious spec-count/dimension mismatches.

    Returns a list of human-readable issues.  Empty list = no problems found.

    Catches the most common LLM mistake: using hardcoded defaults instead of the
    spec values (e.g. range(4) when n_blades=6, or OD=160 when od_mm=150).
    False positives are harmless — they add a correction note to the next prompt,
    not a hard failure.
    """
    import re
    issues: list[str] = []

    # --- Count checks ---
    # Collect all range(N) and standalone N = <int> from the code
    loop_counts = [int(x) for x in re.findall(r'\brange\((\d+)\)\b', code) if 2 < int(x) < 500]
    assign_counts = [int(x) for x in re.findall(r'\b[Nn]\s*=\s*(\d+)\b', code) if 2 < int(x) < 500]
    all_counts = set(loop_counts + assign_counts)

    for param, label in [
        ("n_blades", "blades/vanes"),
        ("n_fins",   "fins"),
        ("n_spokes", "spokes"),
        ("n_teeth",  "teeth"),
    ]:
        if not spec.get(param):
            continue
        expected = int(spec[param])
        if expected in all_counts:
            continue
        # Literal might appear inside an expression, not just range()
        if re.search(rf'\b{expected}\b', code):
            continue
        if all_counts:
            closest = min(all_counts, key=lambda x: abs(x - expected))
            issues.append(
                f"COUNT MISMATCH: spec requires {expected} {label} "
                f"but code has range({closest}) / N={closest}. "
                f"Change to range({expected}) / N={expected}."
            )

    # --- Key dimension checks ---
    def _has_value_near(val: float, tol: float = 0.15) -> bool:
        lo, hi = val * (1 - tol), val * (1 + tol)
        for m in re.finditer(r'\b(\d+(?:\.\d+)?)\b', code):
            v = float(m.group(1))
            if lo <= v <= hi:
                return True
        return False

    for param, label in [("od_mm", "OD"), ("bore_mm", "bore"), ("height_mm", "height")]:
        if not spec.get(param):
            continue
        expected = float(spec[param])
        if expected < 5:
            continue  # too small — too many false positives from line numbers etc.
        if not _has_value_near(expected):
            issues.append(
                f"DIMENSION MISSING: {label}={expected:.0f}mm from spec not found in code "
                f"(no literal within 15% of {expected:.0f}). "
                f"Add {label.upper()} = {expected:.1f} as a named constant."
            )

    # --- Blade sweep direction check ---
    if spec.get("blade_sweep"):
        sweep = spec["blade_sweep"]
        code_lower = code.lower()
        if "backward" in sweep and "forward" in code_lower and "backward" not in code_lower:
            issues.append(
                "SWEEP DIRECTION: spec requires backward-swept blades but code appears to use "
                "forward geometry. Reverse the sweep angle sign (make it negative)."
            )
        elif "forward" in sweep and "backward" in code_lower and "forward" not in code_lower:
            issues.append(
                "SWEEP DIRECTION: spec requires forward-swept blades but code uses backward "
                "geometry. Reverse the sweep angle sign."
            )

    return issues


def _extract_code(response: str) -> str:
    """
    Extract Python code from LLM response.

    Tries in order (AutoBE-style structured output first for highest reliability):
    1. JSON with "code" key (structured output from JSON mode)
    2. JSON embedded in text
    3. Markdown code fences
    4. Raw code (starts with import/from/#)
    """
    # 1. Try JSON structured output first (highest reliability — AutoBE pattern)
    try:
        parsed = json.loads(response.strip())
        if isinstance(parsed, dict) and "code" in parsed:
            code = parsed["code"].strip()
            if code:
                return code
    except (ValueError, json.JSONDecodeError):
        pass

    # 2. Try extracting JSON object from within the response text
    json_match = re.search(r'\{[^{}]*"code"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[^{}]*\}', response, re.DOTALL)
    if json_match:
        code = json_match.group(1)
        # Unescape JSON string
        code = code.replace("\\n", "\n").replace("\\\\", "\\").replace('\\"', '"').replace("\\t", "\t")
        if "import" in code or "cq." in code:
            return code.strip()

    # 3. Try markdown code fence
    match = re.search(r'```(?:python)?\s*\n(.*?)```', response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # 4. If response starts with import or comment, treat whole thing as code
    stripped = response.strip()
    if stripped.startswith(("import ", "from ", "#", "import\n")):
        return stripped

    # 5. Look for first line that starts with import
    lines = stripped.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith(("import ", "from ")):
            return "\n".join(lines[i:])

    return ""
