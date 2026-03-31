"""Autonomous CAM agent — generates Fusion 360 CAM Python scripts from STEP files.

Uses Ollama LLM (qwen2.5-coder:14b) to generate toolpath scripts via the
BaseAgent tool-call pattern. The agent loop: analyze geometry -> select tools ->
calculate feeds/speeds -> generate script -> validate -> refine if needed.

Usage:
    from aria_os.agents.cam_agent import run_cam_agent
    result = run_cam_agent("outputs/cad/step/aria_housing.step", material="aluminium_6061")

    # or from orchestrator via --cam / --full flags
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .base_agent import BaseAgent
from .design_state import DesignState
from .ollama_config import DESIGNER_MODELS, CONTEXT_LIMITS
from .cam_tools import (
    TOOL_LIBRARY,
    SFM_TABLE,
    MACHINE_PROFILES,
    analyze_step,
    select_tools,
    calc_feeds,
    validate_cam_physics,
    estimate_cycle_time,
)


# ---------------------------------------------------------------------------
# Root and output paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_CAM = ROOT / "outputs" / "cam"


# ---------------------------------------------------------------------------
# CAM Designer system prompt
# ---------------------------------------------------------------------------

_CAM_SYSTEM_PROMPT = """You are a CNC manufacturing engineer generating Fusion 360 CAM Python scripts.

Given a STEP file analysis and tool library, generate a complete script that:
1. Creates a CAM setup (select STEP as stock from solid)
2. Adds 3D Adaptive Clearing (roughing) with the largest suitable endmill
3. Adds Parallel finishing pass with a smaller endmill
4. Adds Contour operation for walls
5. Adds drill cycles for any detected holes
6. Sets correct feeds/speeds from the provided SFM data
7. Posts G-code using a generic Fanuc post

The script must be runnable in Fusion 360's scripting console.

Available tools (call them using TOOL_CALL: syntax):
- TOOL_CALL: analyze_step(path) — get geometry: bbox, faces, holes, min feature size
- TOOL_CALL: select_tools(min_feature_mm, max_dim_mm, holes_csv) — get tool recommendations
- TOOL_CALL: calc_feeds(tool_dia_mm, material, n_flutes, depth_mm) — get RPM, feed, DOC
- TOOL_CALL: validate_cam(script_json) — check generated operations against machine limits

Output ONLY valid Python code in a ```python code fence. The script must use:
- adsk.core, adsk.fusion, adsk.cam imports
- def run(context): entry point
- Proper setup creation with fixed box stock
- Tool creation with correct diameters and flute counts
- Operations with correct feeds/speeds values
- G-code post output to a specified folder

CRITICAL: Use the EXACT RPM and feed values returned by calc_feeds. Do not recalculate.
CRITICAL: Every operation MUST have a tool assigned and valid feed/speed parameters.
CRITICAL: Include comments showing tool ID, diameter, RPM, and feed for each operation."""


# ---------------------------------------------------------------------------
# CAM Agent class
# ---------------------------------------------------------------------------

class CAMAgent(BaseAgent):
    """Autonomous CAM agent that generates Fusion 360 CAM scripts from STEP files."""

    def __init__(self, repo_root: Path | None = None):
        self._repo_root = repo_root or ROOT
        tools = {
            "analyze_step": analyze_step,
            "select_tools": self._select_tools_wrapper,
            "calc_feeds": self._calc_feeds_wrapper,
            "validate_cam": self._validate_cam_wrapper,
        }
        super().__init__(
            name="CAMAgent",
            system_prompt=_CAM_SYSTEM_PROMPT,
            model=DESIGNER_MODELS.get("cam", "qwen2.5-coder:14b"),
            tools=tools,
            max_context_tokens=CONTEXT_LIMITS["designer"],
            fallback_to_cloud=True,
        )
        self._step_path: str = ""
        self._material: str = "aluminium_6061"
        self._machine: str = "generic_vmc"
        self._geom: dict[str, Any] = {}
        self._selected_tools: list[dict] = []
        self._operations: list[dict] = []

    # -- Tool wrappers (coerce string args from LLM to correct types) --------

    def _select_tools_wrapper(self, min_feat: str = "10", max_dim: str = "100",
                               holes: str = "") -> str:
        result = select_tools(min_feat, max_dim, holes)
        self._selected_tools = result
        return json.dumps(result, default=str)

    def _calc_feeds_wrapper(self, tool_dia: str = "10", material: str = "",
                             n_flutes: str = "3", depth: str = "0") -> str:
        mat = material or self._material
        return json.dumps(calc_feeds(tool_dia, mat, n_flutes, depth), default=str)

    def _validate_cam_wrapper(self, ops_json: str = "[]") -> str:
        try:
            ops = json.loads(ops_json)
        except (json.JSONDecodeError, ValueError):
            ops = self._operations
        result = validate_cam_physics(ops, self._machine)
        return json.dumps(result, default=str)

    # -- Main entry ----------------------------------------------------------

    def generate(
        self,
        step_path: str | Path,
        material: str = "aluminium_6061",
        machine: str = "generic_vmc",
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Generate a Fusion 360 CAM script for the given STEP file.

        Args:
            step_path: Path to the STEP file to machine.
            material: Material key from SFM_TABLE.
            machine: Machine profile key.
            max_attempts: Maximum LLM generation attempts.

        Returns dict with:
            script_path, summary_path, setup_path, operations, cycle_time_min,
            tools_used, passed, violations
        """
        self._step_path = str(step_path)
        self._material = material
        self._machine = machine
        step_p = Path(step_path)
        part_id = step_p.stem

        print(f"\n  [CAM] {'=' * 56}")
        print(f"  [CAM] Autonomous CAM Agent")
        print(f"  [CAM] Part: {part_id}")
        print(f"  [CAM] Material: {material}")
        print(f"  [CAM] Machine: {MACHINE_PROFILES.get(machine, {}).get('name', machine)}")
        print(f"  [CAM] {'=' * 56}")

        # Phase 1: Analyze geometry
        print(f"  [CAM] Analyzing geometry...")
        self._geom = analyze_step(self._step_path)
        if self._geom.get("error"):
            print(f"  [CAM] Geometry error: {self._geom['error']}")
            return {"passed": False, "violations": [self._geom["error"]]}

        bbox = self._geom.get("bbox", {})
        bbox_str = (f"{bbox.get('x_mm', 0):.0f}x"
                    f"{bbox.get('y_mm', 0):.0f}x"
                    f"{bbox.get('z_mm', 0):.0f}mm")
        print(f"  [CAM] Analyzing geometry: {bbox_str}, "
              f"{self._geom['face_count']} faces, "
              f"{len(self._geom.get('holes', []))} holes")

        # Phase 2: Select tools
        print(f"  [CAM] Selecting tools...")
        holes_csv = ",".join(str(h) for h in self._geom.get("holes", []))
        self._selected_tools = select_tools(
            self._geom["min_feature_mm"],
            self._geom["max_dim_mm"],
            holes_csv,
        )
        tool_summary = []
        for t in self._selected_tools:
            role = t.get("role", "?")
            tid = t["id"]
            if role == "drill" and t.get("target_hole_mm"):
                tool_summary.append(f"{tid} ({role} x{t['target_hole_mm']})")
            else:
                tool_summary.append(f"{tid} ({role})")
        print(f"  [CAM] Tools selected: {', '.join(tool_summary)}")

        # Phase 3: Calculate feeds/speeds for each tool
        print(f"  [CAM] Calculating feeds/speeds...")
        self._operations = self._build_operations()
        self._print_operations()

        # Phase 4: Generate CAM script — deterministic first (instant), LLM only if needed
        print(f"\n  [CAM] Generating script (deterministic)...")
        try:
            script = self._generate_deterministic_script(part_id)

            # Validate the deterministic script
            validation = validate_cam_physics(self._operations, self._machine)
            violations = validation.get("violations", [])

            if not violations:
                # Deterministic script passed — write outputs and return (no LLM needed)
                result = self._write_outputs(part_id, script)
                print(f"  [CAM] All validations passed (deterministic — no LLM needed)")
                return result

            # Deterministic script has issues — try LLM refinement
            print(f"  [CAM] {len(violations)} violations — trying LLM refinement...")
        except Exception as _det_exc:
            print(f"  [CAM] Deterministic generation failed: {_det_exc}")
            print(f"  [CAM] Falling back to LLM...")

        result = self._generate_with_refinement(part_id, max_attempts)

        return result

    # -- Internal helpers ----------------------------------------------------

    def _build_operations(self) -> list[dict[str, Any]]:
        """Build operation list from selected tools + feeds/speeds."""
        operations: list[dict[str, Any]] = []
        bbox = self._geom.get("bbox", {})
        z_depth = bbox.get("z_mm", 20)

        for tool in self._selected_tools:
            role = tool.get("role", "")
            dia = tool["diameter_mm"]
            flutes = tool.get("flutes", 3)
            feeds = calc_feeds(dia, self._material, flutes)

            op: dict[str, Any] = {
                "name": "",
                "role": role,
                "tool_id": tool["id"],
                "tool_type": tool["type"],
                "tool_dia_mm": dia,
                "flutes": flutes,
                "rpm": feeds["rpm"],
                "feed_mm_per_min": feeds["feed_mm_per_min"],
                "depth_of_cut_mm": feeds["depth_of_cut_mm"],
                "width_of_cut_mm": feeds["width_of_cut_mm"],
                "plunge_rate_mmpm": feeds["plunge_rate_mmpm"],
                "overhang_mm": round(dia * 3, 1),
                "material": self._material,
            }

            if role == "roughing":
                op["name"] = "3D Adaptive"
                op["stock_to_leave_mm"] = 0.3
            elif role == "finishing":
                op["name"] = "Parallel"
                op["width_of_cut_mm"] = round(dia * 0.1, 2)  # 10% stepover
                op["depth_of_cut_mm"] = round(min(0.5, feeds["depth_of_cut_mm"]), 2)
                op["stock_to_leave_mm"] = 0.0
            elif role == "contour":
                op["name"] = "Contour"
                op["stock_to_leave_mm"] = 0.0
            elif role == "drill":
                op["name"] = "Drill"
                op["hole_depth_mm"] = z_depth
                op["peck_mm"] = round(dia * 0.5, 1)
                op["n_holes"] = 1
                if tool.get("target_hole_mm"):
                    op["target_hole_mm"] = tool["target_hole_mm"]

            operations.append(op)

        return operations

    def _print_operations(self) -> None:
        """Print the operation plan in the specified format."""
        print(f"  [CAM] Operations:")
        for i, op in enumerate(self._operations, 1):
            role = op["role"]
            name = op["name"]
            tid = op["tool_id"]
            rpm = op["rpm"]
            feed = op["feed_mm_per_min"]
            doc = op["depth_of_cut_mm"]

            if role == "drill":
                peck = op.get("peck_mm", 2.0)
                print(f"  [CAM]   {i}. {name:<12s} — {tid}, "
                      f"{rpm} RPM, pecking {peck}mm")
            elif role == "contour":
                print(f"  [CAM]   {i}. {name:<12s} — {tid}, "
                      f"{rpm} RPM, {feed} mm/min")
            else:
                print(f"  [CAM]   {i}. {name:<12s} — {tid}, "
                      f"{rpm} RPM, {feed} mm/min, {doc}mm DOC")

        # Cycle time
        cycle = estimate_cycle_time(
            self._operations,
            bbox=self._geom.get("bbox"),
            volume_cm3=self._geom.get("volume_cm3"),
        )
        print(f"  [CAM] Cycle time: ~{cycle} min")

    def _write_outputs(self, part_id: str, script: str, violations: list[str] | None = None) -> dict[str, Any]:
        """Write CAM script + summary JSON + setup sheet to disk."""
        out_dir = OUT_CAM / part_id
        out_dir.mkdir(parents=True, exist_ok=True)

        script_path = out_dir / f"{part_id}_cam.py"
        summary_path = out_dir / f"{part_id}_cam_summary.json"
        setup_path = out_dir / "setup_sheet.md"

        script_path.write_text(script, encoding="utf-8")
        print(f"  [CAM] Script: {script_path}")

        cycle_time = estimate_cycle_time(
            self._operations,
            bbox=self._geom.get("bbox"),
            volume_cm3=self._geom.get("volume_cm3"),
        )

        summary = self._build_summary(part_id, cycle_time, violations or [])
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        setup_md = self._build_setup_sheet(part_id, cycle_time)
        setup_path.write_text(setup_md, encoding="utf-8")
        print(f"  [CAM] Setup sheet: {setup_path}")

        return {
            "script_path": str(script_path),
            "summary_path": str(summary_path),
            "setup_path": str(setup_path),
            "operations": self._operations,
            "cycle_time_min": cycle_time,
            "tools_used": [t["id"] for t in self._selected_tools],
            "passed": len(violations or []) == 0,
            "violations": violations or [],
        }

    def _generate_with_refinement(
        self, part_id: str, max_attempts: int
    ) -> dict[str, Any]:
        """LLM generation loop with validation and refinement."""
        out_dir = OUT_CAM / part_id
        out_dir.mkdir(parents=True, exist_ok=True)

        script_path = out_dir / f"{part_id}_cam.py"
        summary_path = out_dir / f"{part_id}_cam_summary.json"
        setup_path = out_dir / "setup_sheet.md"

        previous_failures: list[str] = []
        best_script: str = ""
        best_violations: list[str] = []
        best_count = 999

        for attempt in range(1, max_attempts + 1):
            print(f"\n  [CAM] Generation attempt {attempt}/{max_attempts}...")

            # Build prompt
            prompt = self._build_generation_prompt(part_id, previous_failures)

            # Call LLM via BaseAgent (handles Ollama + cloud fallback + tool dispatch)
            state = DesignState(
                goal=f"Generate CAM script for {part_id}",
                repo_root=self._repo_root,
                domain="cam",
                part_id=part_id,
                material=self._material,
            )
            state.artifacts["step_path"] = self._step_path

            response = self.run(prompt, state)

            if not response:
                previous_failures.append("LLM returned empty response")
                continue

            # Extract code from response
            script = _extract_code(response)
            if not script:
                # Fallback: generate deterministic script if LLM fails
                print(f"  [CAM] No code in LLM response — using deterministic fallback")
                script = self._generate_deterministic_script(part_id)

            # Validate the operations
            validation = validate_cam_physics(self._operations, self._machine)
            violations = validation.get("violations", [])
            warnings = validation.get("warnings", [])

            # Additional script-level checks
            script_issues = self._validate_script(script)
            violations.extend(script_issues)

            n_issues = len(violations)
            print(f"  [CAM] Attempt {attempt}: {n_issues} violations, "
                  f"{len(warnings)} warnings")

            if n_issues < best_count:
                best_count = n_issues
                best_script = script
                best_violations = violations

            if n_issues == 0:
                print(f"  [CAM] All validations passed on attempt {attempt}")
                break

            # Inject failures for next attempt
            previous_failures = violations
            for v in violations:
                print(f"  [CAM]   violation: {v}")

        # Use best script (or deterministic fallback)
        if not best_script:
            print(f"  [CAM] All LLM attempts failed — using deterministic fallback")
            best_script = self._generate_deterministic_script(part_id)
            best_violations = []

        # Write outputs
        script_path.write_text(best_script, encoding="utf-8")
        print(f"  [CAM] Script: {script_path}")

        # Cycle time
        cycle_time = estimate_cycle_time(
            self._operations,
            bbox=self._geom.get("bbox"),
            volume_cm3=self._geom.get("volume_cm3"),
        )

        # Summary JSON
        summary = self._build_summary(part_id, cycle_time, best_violations)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        # Setup sheet
        setup_md = self._build_setup_sheet(part_id, cycle_time)
        setup_path.write_text(setup_md, encoding="utf-8")
        print(f"  [CAM] Setup sheet: {setup_path}")

        return {
            "script_path": str(script_path),
            "summary_path": str(summary_path),
            "setup_path": str(setup_path),
            "operations": self._operations,
            "cycle_time_min": cycle_time,
            "tools_used": [t["id"] for t in self._selected_tools],
            "passed": len(best_violations) == 0,
            "violations": best_violations,
        }

    def _build_generation_prompt(
        self, part_id: str, previous_failures: list[str]
    ) -> str:
        """Build the user prompt for the LLM."""
        bbox = self._geom.get("bbox", {})
        sections = [
            f"## CAM Script Generation Request",
            f"Part: {part_id}",
            f"STEP file: {self._step_path}",
            f"Material: {self._material}",
            f"Machine: {MACHINE_PROFILES.get(self._machine, {}).get('name', self._machine)}",
            "",
            f"## Geometry Analysis",
            f"Bounding box: {bbox.get('x_mm', 0):.1f} x {bbox.get('y_mm', 0):.1f} x {bbox.get('z_mm', 0):.1f} mm",
            f"Faces: {self._geom.get('face_count', 0)}",
            f"Edges: {self._geom.get('edge_count', 0)}",
            f"Holes detected: {self._geom.get('holes', [])}",
            f"Min feature size: {self._geom.get('min_feature_mm', 10)} mm",
            f"Volume: {self._geom.get('volume_cm3', 'unknown')} cm3",
            "",
            f"## Selected Tools",
        ]

        for t in self._selected_tools:
            sections.append(
                f"- {t['id']}: {t['type']} dia={t['diameter_mm']}mm "
                f"flutes={t.get('flutes', 'N/A')} role={t.get('role', '?')}")

        sections.append("")
        sections.append("## Operations with Pre-calculated Feeds/Speeds")
        sections.append("Use these EXACT values in the script:")
        sections.append("")

        for op in self._operations:
            sections.append(
                f"- {op['name']} ({op['tool_id']}): "
                f"RPM={op['rpm']}, feed={op['feed_mm_per_min']}mm/min, "
                f"DOC={op['depth_of_cut_mm']}mm, WOC={op['width_of_cut_mm']}mm, "
                f"plunge={op['plunge_rate_mmpm']}mm/min"
            )

        if previous_failures:
            sections.append("")
            sections.append("## PREVIOUS ATTEMPT FAILURES — FIX THESE:")
            for f in previous_failures:
                sections.append(f"- {f}")

        sections.append("")
        sections.append("## Output Requirements")
        sections.append("Generate a complete Fusion 360 Python CAM script with:")
        sections.append("- def run(context): entry point")
        sections.append("- Setup with FixedBoxStock (1mm side offset, 1.5mm top)")
        sections.append("- All operations listed above with correct tools and feeds")
        sections.append(f"- G-code output to: {OUT_CAM / part_id / 'gcode'}")
        sections.append("- Wrap in ```python code fence")

        return "\n".join(sections)

    def _validate_script(self, script: str) -> list[str]:
        """Additional validation checks on the generated script text."""
        issues: list[str] = []

        if "def run(context)" not in script and "def run(" not in script:
            issues.append("Missing 'def run(context):' entry point")

        if "adsk.cam" not in script:
            issues.append("Missing 'adsk.cam' import — not a valid Fusion CAM script")

        if "adsk.core" not in script:
            issues.append("Missing 'adsk.core' import")

        # Check that tool diameters from our selection appear in the script
        for tool in self._selected_tools:
            dia = tool["diameter_mm"]
            # Check for the diameter value in script (as float or int)
            dia_strs = [str(dia), str(int(dia)) if dia == int(dia) else ""]
            found = any(ds and ds in script for ds in dia_strs if ds)
            if not found and tool.get("role") in ("roughing", "finishing"):
                issues.append(
                    f"Tool {tool['id']} ({dia}mm) not referenced in script")

        if len(script) < 500:
            issues.append("Script is too short (< 500 chars) — likely incomplete")

        return issues

    def _generate_deterministic_script(self, part_id: str) -> str:
        """Generate a deterministic Fusion 360 CAM script without LLM.

        Uses the pre-calculated operations and feeds/speeds to build a
        complete, working script from a template.
        """
        bbox = self._geom.get("bbox", {})
        stock_offset_cm = 0.1   # 1mm
        stock_z_cm = 0.15       # 1.5mm

        # Build tool creation code
        tool_defs: list[str] = []
        op_blocks: list[str] = []
        tool_var_map: dict[str, str] = {}  # tool_id -> variable name

        for i, tool in enumerate(self._selected_tools):
            var_name = f"tool_{i}"
            tool_var_map[tool["id"]] = var_name
            dia_cm = round(tool["diameter_mm"] / 10.0, 4)
            flutes = tool.get("flutes", 3)

            feeds = calc_feeds(tool["diameter_mm"], self._material, flutes)
            rpm = feeds["rpm"]
            feed_cm = round(feeds["feed_mm_per_min"] / 10.0, 4)
            plunge_cm = round(feeds["plunge_rate_mmpm"] / 10.0, 4)

            if tool["type"] == "drill":
                tool_defs.append(
                    f'        # Drill: {tool["id"]} dia={tool["diameter_mm"]}mm\n'
                    f'        {var_name} = _make_drill("{tool["id"]}", {dia_cm})'
                )
            else:
                ttype = "Ball" if tool["type"] == "ball_nose" else "Flat"
                tool_defs.append(
                    f'        # {tool["type"]}: {tool["id"]} dia={tool["diameter_mm"]}mm '
                    f'{flutes}fl\n'
                    f'        {var_name} = _make_{ttype.lower()}_mill('
                    f'"{tool["id"]}", {dia_cm}, {flutes}, {rpm}, {feed_cm}, {plunge_cm})'
                )

        # Build operation blocks
        for i, op in enumerate(self._operations):
            tool_var = tool_var_map.get(op["tool_id"], "tool_0")
            role = op["role"]
            name = op["name"]
            rpm = op["rpm"]
            feed = op["feed_mm_per_min"]
            doc_cm = round(op["depth_of_cut_mm"] / 10.0, 4)
            woc_cm = round(op["width_of_cut_mm"] / 10.0, 4)
            stock_leave_cm = round(op.get("stock_to_leave_mm", 0) / 10.0, 4)

            if role == "roughing":
                op_blocks.append(
                    f'        # Op {i+1}: {name} — {op["tool_id"]}, '
                    f'{rpm} RPM, {feed} mm/min, {op["depth_of_cut_mm"]}mm DOC\n'
                    f'        _inp = setup.operations.createInput("adaptive")\n'
                    f'        _inp.tool = {tool_var}\n'
                    f'        _inp.parameters.itemByName("optimalLoad").expression = '
                    f'"{woc_cm}"\n'
                    f'        _inp.parameters.itemByName("maximumStepdown").expression = '
                    f'"{doc_cm}"\n'
                    f'        _inp.parameters.itemByName("stockToLeave").expression = '
                    f'"{stock_leave_cm}"\n'
                    f'        _op_{i} = setup.operations.add(_inp)\n'
                    f'        _op_{i}.name = "3D_Adaptive_Rough"'
                )
            elif role == "finishing":
                stepover_cm = round(op["width_of_cut_mm"] / 10.0, 4)
                op_blocks.append(
                    f'        # Op {i+1}: {name} — {op["tool_id"]}, '
                    f'{rpm} RPM, {feed} mm/min\n'
                    f'        _inp = setup.operations.createInput("parallel")\n'
                    f'        _inp.tool = {tool_var}\n'
                    f'        _inp.parameters.itemByName("stepover").expression = '
                    f'"{stepover_cm}"\n'
                    f'        _inp.parameters.itemByName("maximumStepdown").expression = '
                    f'"{doc_cm}"\n'
                    f'        _inp.parameters.itemByName("stockToLeave").expression = '
                    f'"{stock_leave_cm}"\n'
                    f'        _op_{i} = setup.operations.add(_inp)\n'
                    f'        _op_{i}.name = "Parallel_Finish"'
                )
            elif role == "contour":
                op_blocks.append(
                    f'        # Op {i+1}: {name} — {op["tool_id"]}, '
                    f'{rpm} RPM, {feed} mm/min\n'
                    f'        _inp = setup.operations.createInput("contour")\n'
                    f'        _inp.tool = {tool_var}\n'
                    f'        _inp.parameters.itemByName("stockToLeave").expression = '
                    f'"{stock_leave_cm}"\n'
                    f'        _op_{i} = setup.operations.add(_inp)\n'
                    f'        _op_{i}.name = "Contour_Walls"'
                )
            elif role == "drill":
                peck = op.get("peck_mm", 2.0)
                op_blocks.append(
                    f'        # Op {i+1}: {name} — {op["tool_id"]}, '
                    f'{rpm} RPM, peck {peck}mm\n'
                    f'        _inp = setup.operations.createInput("drill")\n'
                    f'        _inp.tool = {tool_var}\n'
                    f'        _inp.parameters.itemByName("cycleType").expression = '
                    f'"\\\"chip_breaking\\\""\n'
                    f'        _op_{i} = setup.operations.add(_inp)\n'
                    f'        _op_{i}.name = "Drill_{op["tool_dia_mm"]:.0f}mm"'
                )

        gcode_dir = str(OUT_CAM / part_id / "gcode").replace("\\", "\\\\")
        _machine_profile = MACHINE_PROFILES.get(self._machine, {})
        _machine_name = _machine_profile.get("name", self._machine) if _machine_profile else self._machine

        script = f'''"""
Auto-generated Fusion 360 CAM script.
Generated by aria_os/agents/cam_agent.py (deterministic fallback)

Part:     {part_id}
Material: {self._material}
Machine:  {_machine_name}
Bbox:     {bbox.get("x_mm", 0):.1f} x {bbox.get("y_mm", 0):.1f} x {bbox.get("z_mm", 0):.1f} mm

HOW TO USE:
  1. In Fusion 360, open the STEP file: {self._step_path}
  2. Go to Tools > Add-Ins > Scripts and Add-Ins
  3. Click + to add this script, then Run
  4. Toolpaths generate automatically. Review then post to gcode.
"""
import adsk.core
import adsk.fusion
import adsk.cam
import traceback


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)

        # Switch to Manufacturing workspace
        camWs = ui.workspaces.itemById("CAMEnvironment")
        camWs.activate()

        cam = adsk.cam.CAM.cast(app.activeProduct)
        if cam is None:
            ui.messageBox("Could not get CAM object — ensure a part is open.")
            return

        # Create Setup
        setups = cam.setups
        setupInput = setups.createInput(adsk.cam.OperationTypes.MillingOperation)
        setupInput.stockMode = adsk.cam.SetupStockModes.FixedBoxStock
        boxStockInput = adsk.cam.FixedStockSizeInput.cast(setupInput.stock)
        boxStockInput.xOffset = adsk.core.ValueInput.createByReal({stock_offset_cm})
        boxStockInput.yOffset = adsk.core.ValueInput.createByReal({stock_offset_cm})
        boxStockInput.zOffset = adsk.core.ValueInput.createByReal({stock_z_cm})
        setup = setups.add(setupInput)
        setup.name = "{part_id}_setup"

        # Tool library
        toolLib = cam.documentToolLibrary

        def _make_flat_mill(name, dia_cm, flutes, rpm, feed_cmpm, plunge_cmpm):
            t = adsk.cam.ToolingData.createFlatMill()
            t.name = name
            t.diameter = adsk.core.ValueInput.createByReal(dia_cm)
            t.numberOfFlutes = flutes
            t.spindleSpeed = adsk.core.ValueInput.createByReal(rpm)
            t.feedrate = adsk.core.ValueInput.createByReal(feed_cmpm)
            t.plungeFeedrate = adsk.core.ValueInput.createByReal(plunge_cmpm)
            return toolLib.add(t)

        def _make_ball_mill(name, dia_cm, flutes, rpm, feed_cmpm, plunge_cmpm):
            t = adsk.cam.ToolingData.createBallMill()
            t.name = name
            t.diameter = adsk.core.ValueInput.createByReal(dia_cm)
            t.numberOfFlutes = flutes
            t.spindleSpeed = adsk.core.ValueInput.createByReal(rpm)
            t.feedrate = adsk.core.ValueInput.createByReal(feed_cmpm)
            t.plungeFeedrate = adsk.core.ValueInput.createByReal(plunge_cmpm)
            return toolLib.add(t)

        def _make_drill(name, dia_cm):
            t = adsk.cam.ToolingData.createDrill()
            t.name = name
            t.diameter = adsk.core.ValueInput.createByReal(dia_cm)
            return toolLib.add(t)

        # Create tools
{chr(10).join(tool_defs)}

        # Create operations
{chr(10).join(op_blocks)}

        # Generate all toolpaths
        cam.generateAllToolpaths(False)

        # Post to G-code
        postConfig = cam.postConfigurations.itemByName("Generic Milling")
        outputFolder = r"{gcode_dir}"
        postInput = adsk.cam.PostOutputUnitOptions.DocumentUnitsOutput
        if postConfig:
            cam.postProcess(setup, postConfig, outputFolder, postInput, "{part_id}")
            ui.messageBox(f"CAM complete! Gcode saved to:\\n{gcode_dir}")
        else:
            ui.messageBox("Toolpaths generated. Select a post config to export gcode.")

    except Exception:
        if ui:
            ui.messageBox(f"CAM generation failed:\\n{{traceback.format_exc()}}")
'''
        return script

    def _build_summary(
        self, part_id: str, cycle_time: float, violations: list[str]
    ) -> dict[str, Any]:
        """Build the CAM summary JSON."""
        bbox = self._geom.get("bbox", {})
        return {
            "part": part_id,
            "material": self._material,
            "machine": MACHINE_PROFILES.get(self._machine, {}).get("name", self._machine),
            "bbox_mm": bbox,
            "min_feature_mm": self._geom.get("min_feature_mm"),
            "holes_mm": self._geom.get("holes", []),
            "tools": [
                {"id": t["id"], "type": t["type"], "dia_mm": t["diameter_mm"],
                 "role": t.get("role", "")}
                for t in self._selected_tools
            ],
            "operations": [
                {"name": op["name"], "tool": op["tool_id"],
                 "rpm": op["rpm"], "feed_mmpm": op["feed_mm_per_min"],
                 "doc_mm": op["depth_of_cut_mm"],
                 "woc_mm": op["width_of_cut_mm"]}
                for op in self._operations
            ],
            "cycle_time_min": cycle_time,
            "validation": {
                "passed": len(violations) == 0,
                "violations": violations,
            },
        }

    def _build_setup_sheet(self, part_id: str, cycle_time: float) -> str:
        """Build a CNC operator setup sheet in Markdown."""
        bbox = self._geom.get("bbox", {})
        machine = MACHINE_PROFILES.get(self._machine, {})
        x = bbox.get("x_mm", 0)
        y = bbox.get("y_mm", 0)
        z = bbox.get("z_mm", 0)

        # Stock dimensions
        stock_x = round(x + 2, 1)
        stock_y = round(y + 2, 1)
        stock_z = round(z + 3, 1)

        # Fixturing recommendation
        max_xy = max(x, y)
        if max_xy <= 150:
            fixture = "6\" machine vise"
        elif max_xy <= 300:
            fixture = "Fixture plate with toe clamps"
        else:
            fixture = "Fixture plate with step blocks"

        lines = [
            f"# CNC Setup Sheet: {part_id}",
            "",
            f"**Generated by:** ARIA-OS CAM Agent",
            f"**Material:** {self._material}",
            f"**Machine:** {machine.get('name', self._machine)}",
            "",
            "## Stock",
            f"- Raw stock: {stock_x} x {stock_y} x {stock_z} mm",
            f"- Part envelope: {x:.1f} x {y:.1f} x {z:.1f} mm",
            f"- Stock allowance: 1mm sides, 1.5mm top",
            "",
            "## Fixturing",
            f"- **Recommended:** {fixture}",
            f"- Datum: bottom-left corner of stock, top face Z=0",
            f"- Verify parallels / soft jaws are clean",
            "",
            "## Tool List",
            "",
            "| # | Tool ID | Type | Dia (mm) | Role |",
            "|---|---------|------|----------|------|",
        ]
        for i, t in enumerate(self._selected_tools, 1):
            lines.append(
                f"| {i} | {t['id']} | {t['type']} | {t['diameter_mm']} | "
                f"{t.get('role', '')} |")

        lines.extend([
            "",
            "## Operations",
            "",
            "| # | Operation | Tool | RPM | Feed (mm/min) | DOC (mm) |",
            "|---|-----------|------|-----|---------------|----------|",
        ])
        for i, op in enumerate(self._operations, 1):
            lines.append(
                f"| {i} | {op['name']} | {op['tool_id']} | {op['rpm']} | "
                f"{op['feed_mm_per_min']} | {op['depth_of_cut_mm']} |")

        lines.extend([
            "",
            f"## Estimated Cycle Time: {cycle_time} min",
            "",
            "## Notes",
            "- Run all tools at programmed feeds/speeds — do not override",
            "- Listen for chatter on first piece and reduce feed 10% if needed",
            "- Deburr all sharp edges after machining",
            f"- Material: {self._material} — verify stock cert matches",
        ])

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Code extraction helper
# ---------------------------------------------------------------------------

def _extract_code(response: str) -> str:
    """Extract Python code from LLM response (markdown fences or raw)."""
    # Try markdown code fence
    match = re.search(r'```(?:python)?\s*\n(.*?)```', response, re.DOTALL)
    if match:
        code = match.group(1).strip()
        if len(code) > 200:
            return code

    # If response starts with import or comment, treat as code
    stripped = response.strip()
    if stripped.startswith(("import ", "from ", "#", '"""')):
        return stripped

    # Find first import line
    lines = stripped.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith(("import ", "from ")):
            return "\n".join(lines[i:])

    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_cam_agent(
    step_path: str | Path,
    material: str = "aluminium_6061",
    machine: str = "generic_vmc",
    repo_root: Path | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Run the autonomous CAM agent on a STEP file.

    This is the primary entry point. Called from:
    - `run_aria_os.py --cam <step_file>` (standalone)
    - `run_aria_os.py --full "part description"` (as part of full pipeline)

    Args:
        step_path: Path to the STEP file.
        material: Material key (e.g. 'aluminium_6061', 'steel_4140').
        machine: Machine profile key (e.g. 'generic_vmc', 'haas_vf2').
        repo_root: Repository root path.
        max_attempts: Max LLM generation attempts.

    Returns dict with script_path, summary_path, operations, cycle_time_min,
    tools_used, passed, violations.
    """
    agent = CAMAgent(repo_root=repo_root)
    return agent.generate(
        step_path=step_path,
        material=material,
        machine=machine,
        max_attempts=max_attempts,
    )
