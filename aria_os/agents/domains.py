"""Domain registry — maps each pipeline domain to its tools, validators, and prompts."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Domain detection — LLM-driven, no keywords
# ---------------------------------------------------------------------------

VALID_DOMAINS = ("cad", "cam", "ecad", "civil", "drawing", "assembly", "dfm")

_DOMAIN_PROMPT = """You are a routing agent. Given an engineering task description, classify it into exactly ONE domain.

Domains:
- cad: Physical parts, mechanical components, enclosures, consumer products, toys, brackets, gears, housings — anything that needs 3D geometry (STEP/STL)
- cam: CNC machining toolpaths, G-code generation, feeds/speeds calculation — ONLY when explicitly asked to generate machining instructions for an existing part
- ecad: Electronic circuit boards, PCB layout, schematic design — ONLY for actual electronic circuits with components like MCUs, resistors, capacitors
- civil: Civil engineering plans — roads, drainage, grading, site plans, construction drawings (DXF output)
- drawing: Engineering drawings, GD&T annotations, blueprints — ONLY when explicitly asked to create a 2D technical drawing of an existing part
- assembly: Multi-part assembly, mating constraints, clearance checking — ONLY when combining multiple existing parts
- dfm: Design for manufacturability analysis — ONLY when explicitly asked to analyze manufacturability, machinability, or DFM of an existing part

Rules:
- Default to "cad" when uncertain — most requests are for physical parts
- "cam" is ONLY for machining instructions, NOT for designing the part itself
- "ecad" requires actual electronic components (ESP32, STM32, resistors, PCBs), not just anything with "board" in the name
- "drawing" is ONLY for creating a 2D drawing of an EXISTING part, not designing new geometry

Respond with ONLY the domain name, nothing else."""


def detect_domain(goal: str, cad_tool: str = "") -> str:
    """Detect the pipeline domain using LLM reasoning. Falls back to 'cad'."""
    # Explicit CLI tool flags override LLM
    if cad_tool == "autocad":
        return "civil"

    # Ask Ollama to classify
    try:
        from .base_agent import _call_ollama, is_ollama_available
        from .ollama_config import AGENT_MODELS

        if not is_ollama_available():
            return "cad"

        response = _call_ollama(
            f"Classify this task: {goal}",
            _DOMAIN_PROMPT,
            AGENT_MODELS["spec"],
        )
        if response:
            domain = response.strip().lower().split()[0].strip('."\'')
            if domain in VALID_DOMAINS:
                return domain
    except Exception:
        pass

    return "cad"


# ---------------------------------------------------------------------------
# Tool factories (thin wrappers around existing pipeline functions)
# ---------------------------------------------------------------------------

def make_tools(domain: str, repo_root: Path) -> dict[str, Callable]:
    """Return domain-specific tool callables for agent use."""
    if domain == "cad":
        return _cad_tools(repo_root)
    elif domain == "ecad":
        return _ecad_tools(repo_root)
    elif domain == "civil":
        return _civil_tools(repo_root)
    elif domain == "drawing":
        return _drawing_tools(repo_root)
    elif domain == "assembly":
        return _assembly_tools(repo_root)
    elif domain == "dfm":
        return _dfm_tools(repo_root)
    return _cad_tools(repo_root)


def _cad_tools(repo_root: Path) -> dict[str, Callable]:
    def execute_cadquery(code: str, step_path: str = "", stl_path: str = "") -> dict:
        """Execute CadQuery code in sandbox, return bbox + error."""
        import cadquery as cq
        ns: dict[str, Any] = {"cq": cq, "math": __import__("math")}
        try:
            exec(compile(code, "<agent_cq>", "exec"), ns)
            result_obj = ns.get("result")
            if result_obj is not None:
                bb = result_obj.val().BoundingBox()
                bbox = {"x": round(bb.xlen, 3), "y": round(bb.ylen, 3), "z": round(bb.zlen, 3)}
                if step_path:
                    Path(step_path).parent.mkdir(parents=True, exist_ok=True)
                    cq.exporters.export(result_obj, step_path)
                if stl_path:
                    Path(stl_path).parent.mkdir(parents=True, exist_ok=True)
                    cq.exporters.export(result_obj, stl_path)
                return {"status": "success", "bbox": bbox}
            return {"status": "failure", "error": "No 'result' variable in generated code"}
        except Exception as exc:
            return {"status": "failure", "error": str(exc)[:500]}

    def get_cq_patterns() -> str:
        """Return CadQuery pattern reference for the designer agent."""
        return """CadQuery Patterns:
- Box: cq.Workplane("XY").box(W, D, H)
- Cylinder: cq.Workplane("XY").circle(R).extrude(H)
- Ring: cq.Workplane("XY").circle(OD/2).circle(ID/2).extrude(H)
- Bore: result.faces(">Z").workplane().circle(R).cutThruAll()
- Bolt holes: result.faces(">Z").workplane().pushPoints(pts).circle(R).cutThruAll()
- Shell: result.shell(-wall)
- Cut: result.cut(other)
- Union: result.union(other)
- Revolve: wp.polyline(pts).close().revolve(360, (0,0,0), (0,1,0))
- ALWAYS end with: bb = result.val().BoundingBox(); print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")
- NEVER use fillets on first attempt
- Select faces by direction: faces(">Z"), faces("<X"), not by index"""

    def get_few_shot(goal: str, part_id: str) -> str:
        """Return relevant few-shot examples from learning log."""
        try:
            from ..cad_learner import get_few_shot_examples
            examples = get_few_shot_examples(goal, part_id, repo_root)
            return examples[:3000] if examples else "No examples available."
        except Exception:
            return "No examples available."

    def get_template_reference(part_id: str) -> str:
        """Return template code for reference (not for direct use)."""
        try:
            from ..generators.cadquery_generator import _find_template_fn
            fn = _find_template_fn(part_id)
            if fn:
                code = fn({"od_mm": 100, "bore_mm": 50, "height_mm": 40, "width_mm": 100,
                          "depth_mm": 60, "thickness_mm": 10, "wall_mm": 3, "n_bolts": 4})
                return f"Reference template for {part_id} (adapt, don't copy):\n{code[:2000]}"
        except Exception:
            pass
        return "No template reference available."

    return {
        "execute_cadquery": execute_cadquery,
        "get_cq_patterns": get_cq_patterns,
        "get_few_shot": get_few_shot,
        "get_template_reference": get_template_reference,
    }



    def get_machine_profile(name: str = "tormach_1100") -> dict:
        try:
            from ..cam_physics import get_machine_profile
            return get_machine_profile(name)
        except Exception as exc:
            return {"error": str(exc)}

    def validate_cam(operations: str = "[]") -> dict:
        return _agent_validate(operations)

    return {
        "analyze_step": analyze_step_wrapper,
        "select_tools": select_tools_wrapper,
        "calc_feeds": calc_feeds_wrapper,
        "check_machinability": check_machinability,
        "get_machine_profile": get_machine_profile,
        "validate_cam": validate_cam,
    }


def _ecad_tools(repo_root: Path) -> dict[str, Callable]:
    def extract_firmware_pins() -> dict:
        try:
            from ..ecad_generator import extract_firmware_pins
            return extract_firmware_pins(repo_root)
        except Exception as exc:
            return {"error": str(exc)}

    def validate_bom(bom_path: str) -> dict:
        try:
            import json as _json
            bom = _json.loads(Path(bom_path).read_text())
            # Basic schema checks
            has_components = bool(bom.get("components"))
            has_schema = bool(bom.get("schema_version"))
            return {"valid": has_components and has_schema,
                    "n_components": len(bom.get("components", []))}
        except Exception as exc:
            return {"error": str(exc)}

    return {
        "extract_firmware_pins": extract_firmware_pins,
        "validate_bom": validate_bom,
    }


def _civil_tools(repo_root: Path) -> dict[str, Callable]:
    def get_standard(state: str, discipline: str = "drainage") -> dict:
        try:
            from ..autocad.standards_library import get_standard as _get
            return _get(state, discipline)
        except Exception as exc:
            return {"error": str(exc)}

    def design_pipe(flow_cfs: str, slope: str = "0.005") -> dict:
        try:
            from ...cem.cem_civil import design_storm_pipe
            return design_storm_pipe(float(flow_cfs), float(slope))
        except Exception as exc:
            return {"error": str(exc)}

    return {
        "get_standard": get_standard,
        "design_pipe": design_pipe,
    }


def _drawing_tools(repo_root: Path) -> dict[str, Callable]:
    def load_step_projections(step_path: str) -> dict:
        try:
            from ..drawing_generator import _load_projections
            bb, front, top, right = _load_projections(Path(step_path))
            return {"bbox": {"x": round(bb.xlen, 1), "y": round(bb.ylen, 1), "z": round(bb.zlen, 1)},
                    "has_front": bool(front), "has_top": bool(top), "has_right": bool(right)}
        except Exception as exc:
            return {"error": str(exc)}

    return {"load_step_projections": load_step_projections}


def _assembly_tools(repo_root: Path) -> dict[str, Callable]:
    def check_clearance(parts_config: str) -> dict:
        try:
            import json as _json
            from ..clearance_checker import check_clearance as _check
            parts = _json.loads(parts_config)
            return _check(parts)
        except Exception as exc:
            return {"error": str(exc)}

    return {"check_clearance": check_clearance}


def _dfm_tools(repo_root: Path) -> dict[str, Callable]:
    from .dfm_tools import (
        analyze_step_geometry,
        estimate_wall_thickness,
        check_undercuts,
        classify_machining_axes,
        estimate_feature_complexity,
    )

    def dfm_analyze_geometry(step_path: str) -> dict:
        return analyze_step_geometry(step_path)

    def dfm_wall_thickness(step_path: str) -> str:
        t = estimate_wall_thickness(step_path)
        return f"{t:.3f} mm"

    def dfm_undercuts(step_path: str) -> dict:
        return check_undercuts(step_path)

    def dfm_axes(step_path: str) -> str:
        return classify_machining_axes(step_path)

    def dfm_complexity(face_count: str, edge_count: str) -> str:
        return estimate_feature_complexity(int(face_count), int(edge_count))

    return {
        "analyze_geometry": dfm_analyze_geometry,
        "wall_thickness": dfm_wall_thickness,
        "check_undercuts": dfm_undercuts,
        "classify_axes": dfm_axes,
        "feature_complexity": dfm_complexity,
    }


# ---------------------------------------------------------------------------
# Validator factories
# ---------------------------------------------------------------------------

def make_validators(domain: str, repo_root: Path) -> list[Callable]:
    """Return domain-specific validator callables."""
    validators = []

    if domain == "cad":
        from ..post_gen_validator import check_geometry, check_output_quality
        from ..geometry_validator import validate_geometry
        validators = [check_geometry, validate_geometry, check_output_quality]

    elif domain == "cam":
        try:
            from ..cam_validator import check_machinability, check_undercuts
            validators = [check_machinability, check_undercuts]
        except ImportError:
            pass

    elif domain == "ecad":
        # ECAD validators are run inline by the ecad_generator
        pass

    elif domain == "civil":
        # Civil validators are standards-based checks
        pass

    elif domain == "dfm":
        from .dfm_agent import run_dfm_analysis
        validators = [run_dfm_analysis]

    return validators


# ---------------------------------------------------------------------------
# Domain-specific system prompts for the DesignerAgent
# ---------------------------------------------------------------------------

DESIGNER_PROMPTS: dict[str, str] = {
    "cad": """You are a CadQuery mechanical CAD engineer. Generate Python code using CadQuery to create the requested part.

Rules:
- Build solid first, then cuts/holes. Never fillet on first attempt.
- Use faces(">Z"), faces("<X") for face selection, never by index.
- NEVER use .cylinder() — it does NOT exist in CadQuery. Use .circle(r).extrude(h) instead.
- NEVER use named kwargs like depth=, r=, height= in CadQuery methods. Use positional args.
- For a flat plate with holes: cq.Workplane("XY").box(W, D, T) then .faces(">Z").workplane().pushPoints(pts).circle(r).cutThruAll()
- End with: result = <your_solid>
- Print BBOX at end: bb = result.val().BoundingBox(); print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")
- Use TOOL_CALL: get_cq_patterns() to see the CadQuery pattern reference.
- Use TOOL_CALL: get_template_reference(part_id) to see a reference implementation.
- Use TOOL_CALL: execute_cadquery(code) to test your code and get the bounding box.
CRITICAL OUTPUT FORMAT: Return a JSON object with a single key "code" containing the Python source.
Example: {"code": "import cadquery as cq
result = cq.Workplane(\"XY\").box(10,10,10)
bb = result.val().BoundingBox()
print(f\"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}\")"}
The code field must contain ONLY valid executable Python. No markdown fences.""",

    "cam": """You are a CNC manufacturing engineer. Generate a Fusion 360 CAM Python script for the given STEP file.

Rules:
- Use TOOL_CALL: analyze_step(step_path) to get geometry info.
- Use TOOL_CALL: get_machine_profile(name) to get machine capabilities.
- Use TOOL_CALL: check_machinability(step_path) to verify the part can be machined.
- Select appropriate tools (endmills, drills) based on feature sizes.
- Calculate feeds/speeds from SFM tables and chip load.
- Generate operations: 3D Adaptive (rough) -> Parallel (finish) -> Contour -> Drill.
- Output a complete Fusion 360 Python CAM script.""",

    "ecad": """You are a KiCad PCB design engineer. Generate a pcbnew Python script for the given board description.

Rules:
- Extract board dimensions, MCU type, interfaces, power requirements from the spec.
- Use TOOL_CALL: extract_firmware_pins() to get pin definitions from firmware source.
- Select appropriate components (MCU, regulators, connectors, passives).
- Place components on a grid with proper spacing (MCU center, power top-right, connectors edges).
- Add mounting holes (M3, 3mm from board edge).
- Add silkscreen labels for all components.
- Output a complete KiCad pcbnew Python script.""",

    "civil": """You are a civil engineer generating AutoCAD DXF plans using ezdxf.

Rules:
- Use TOOL_CALL: get_standard(state, discipline) to load state-specific design standards.
- Use proper NCS layer naming (ROAD-CENTERLINE, UTIL-STORM, ANNO-TEXT, etc.).
- Include: title block, north arrow, scale bar, legend, general notes.
- For drainage: size pipes using Manning's equation, show rim/invert elevations.
- For roads: show centerline, ROW, EOP, lane markings, station labels.
- Output ezdxf Python code that writes a .dxf file.""",

    "drawing": """You are a GD&T engineering drawing specialist. Generate SVG markup for an A3 engineering drawing.

Rules:
- Include 4 views: front, top, section A-A, isometric.
- Add proper dimension lines with extension lines and arrowheads.
- Add GD&T feature control frames per ISO/ASME standards.
- Add datum references, center lines, section cut lines.
- Include title block with part ID, material, date, tolerances.
- Output SVG markup.""",

    "assembly": """You are a mechanical assembly engineer. Position parts and verify clearances.

Rules:
- Load each STEP file and apply position/rotation transforms.
- Use TOOL_CALL: check_clearance(parts_json) to verify no interpenetration.
- Suggest mating constraints (coaxial, face-to-face, tangent).
- Report clearances between all nearby pairs.
- Output a JSON assembly configuration.""",

    "dfm": """You are a manufacturing engineer specializing in Design for Manufacturability (DFM) analysis.

Rules:
- Use TOOL_CALL: analyze_geometry(step_path) to extract geometry metrics.
- Use TOOL_CALL: wall_thickness(step_path) to estimate minimum wall.
- Use TOOL_CALL: check_undercuts(step_path) to find undercut faces.
- Use TOOL_CALL: classify_axes(step_path) to determine machining axes needed.
- Use TOOL_CALL: feature_complexity(face_count, edge_count) to classify complexity.
- Recommend the optimal manufacturing process based on geometry.
- Flag critical DFM issues: thin walls, deep pockets, tight radii, high aspect ratio.
- Output a structured JSON DFM report with score, issues, and recommendations.""",
}
