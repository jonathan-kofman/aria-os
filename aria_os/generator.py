"""Generate CadQuery code from plan + context. Templates for known parts; Claude API for arbitrary parts."""
from .context_loader import get_mechanical_constants, load_context
from typing import Any, Optional
from pathlib import Path

# When True, route all parts through LLM (for testing). When False, only generic parts use LLM.
FORCE_LLM = False
KNOWN_PART_IDS = frozenset({
    "aria_housing", "aria_spool", "aria_cam_collar",
    "aria_rope_guide", "aria_motor_mount",
    "aria_ratchet_ring", "aria_catch_pawl", "aria_flyweight",
    "aria_brake_drum", "aria_spool_hub", "aria_trip_lever", "aria_shaft_collar",
})


# CadQuery best practices and failure patterns for system prompt
CADQUERY_BEST_PRACTICES = """
CadQuery best practices (MUST follow):
- Always use cq.Workplane("XY") as the base.
- Box first, then shells/cuts — never the reverse. Build solid first, then cut interior or add holes.
- For hollow parts: create solid, then cut inner void (separate operation). Do not use annular/donut profiles for initial extrusion.
- Bores: use .faces(">Z").workplane().center(x,y).circle(r).cutBlind(-depth) for blind holes, or .hole(diameter) for through.
- Select faces by direction: faces(">Z"), faces("<Z"), faces(">Y"), not by index.
- Use .translate() to position features when needed.
- At the end, print bbox for validation: bb = result.val().BoundingBox(); print(f"BBOX:{bb.xlen:.2f},{bb.ylen:.2f},{bb.zlen:.2f}")
- Code must define a single variable: result = (CQ Workplane or solid).
"""

FAILURE_PATTERNS = """
Known failure patterns (from aria_failures.md — avoid these):
- Never use annular profile (outer rect minus inner rect) for the initial extrusion. Solid box first, then cut interior.
- Always sketch/cut on an EXISTING FACE of the body (e.g. result.faces(">Z").workplane()), never construct planes independently.
- Ensure upstream operations succeed before join/cut. Do not reference faces by index (e.g. faces[0]); use normal direction.
"""


def build_system_prompt(plan: dict[str, Any], context: dict[str, str]) -> str:
    """Build detailed system prompt: mechanical constants, failure patterns, best practices, plan."""
    constants = get_mechanical_constants(context)
    failures_raw = context.get("aria_failures", "")
    constants_block = "Mechanical constants (mm) from aria_mechanical.md:\n" + "\n".join(f"  {k}: {v}" for k, v in sorted(constants.items()))
    plan_block = "Part requirements (structured plan):\n" + plan.get("text", "")
    build_order = plan.get("build_order", [])
    if build_order:
        plan_block += "\n\nBuild order (follow exactly):\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(build_order))
    return f"""{constants_block}

{FAILURE_PATTERNS}

{CADQUERY_BEST_PRACTICES}

{plan_block}

Generate self-contained CadQuery Python code that:
1. Imports cadquery as cq
2. Builds the part following the build order
3. Assigns the final solid/workplane to variable: result
4. Prints bbox at end: bb = result.val().BoundingBox(); print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
"""


def _generate_lattice_from_nl(goal: str) -> str:
    """
    Convert a natural-language lattice request into a small stub that the lattice CLI can handle.
    This returns a comment-only CadQuery script so the validator has something to run.
    Actual geometry is generated via run_aria_os.py --lattice, not this path.
    """
    return f'''import cadquery as cq

# Lattice generation is handled via dedicated CLI:
#   python run_aria_os.py --lattice "..." 
# Goal was:
#   {goal!r}
#
# No geometry generated in this path; use lattice CLI instead.
result = cq.Workplane("XY").box(10, 10, 1)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def generate(
    plan: dict[str, Any] | str,
    context: dict[str, str],
    repo_root: Optional[Path] = None,
    previous_code: Optional[str] = None,
    previous_error: Optional[str] = None,
    goal: Optional[str] = None,
) -> str:
    """
    Generate CadQuery code. Known part_ids use templates; generic/unrecognized use LLM.
    When FORCE_LLM is True, all parts go through LLM.
    Raises on LLM failure (API error or no code extracted).
    """
    if isinstance(plan, str):
        plan = _plan_text_to_struct(plan, context)
    # Lattice routing: special-case plan route before standard part_id logic
    route = plan.get("route")
    if route == "lattice_generator" and goal:
        return _generate_lattice_from_nl(goal)

    part_id = plan.get("part_id", "aria_part")
    is_generic = part_id not in KNOWN_PART_IDS
    force_llm = plan.get("force_llm", False) or bool(plan.get("route_reason"))
    use_llm = FORCE_LLM or is_generic or force_llm

    if use_llm:
        from . import llm_generator
        try:
            code = llm_generator.generate(
                plan, context, repo_root=repo_root,
                previous_code=previous_code, previous_error=previous_error,
            )
        except Exception as e:
            raise RuntimeError(f"LLM generation failed: {e}") from e
        part_name = (goal or part_id or "llm_part").strip()[:60]
        try:
            llm_generator.save_generated_code(code, part_name, repo_root)
        except Exception:
            pass
        return code

    code = _generate_from_structured_plan(plan, context)
    if "print(f\"BBOX:" not in code and "print('BBOX:" not in code:
        code = code.rstrip()
        if not code.endswith("result = result") and "result =" in code:
            code += "\n# Self-check for validator\nbb = result.val().BoundingBox()\nprint(f\"BBOX:{bb.xlen:.2f},{bb.ylen:.2f},{bb.zlen:.2f}\")"
        else:
            code += "\nbb = result.val().BoundingBox()\nprint(f\"BBOX:{bb.xlen:.2f},{bb.ylen:.2f},{bb.zlen:.2f}\")"
    return code


def _plan_text_to_struct(plan_text: str, context: dict[str, str]) -> dict[str, Any]:
    """Legacy: turn plan string into minimal struct for generator."""
    pl = (plan_text or "").lower()
    constants = get_mechanical_constants(context)
    if "housing shell" in pl or "aria housing" in pl:
        return {
            "part_id": "aria_housing",
            "text": plan_text,
            "base_shape": {"type": "box", "width": constants.get("housing_width", 700), "height": constants.get("housing_height", 680), "depth": constants.get("housing_depth", 344)},
            "hollow": True,
            "wall_mm": constants.get("wall_thickness", 10),
            "features": [],
            "build_order": [],
        }
    if "spool" in pl:
        return {
            "part_id": "aria_spool",
            "text": plan_text,
            "base_shape": {"type": "cylinder", "diameter": constants.get("rope_spool_dia", 600), "height": 50},
            "hollow": True,
            "wall_mm": 10,
            "features": [],
            "build_order": [],
        }
    return {"part_id": "aria_part", "text": plan_text, "base_shape": {"type": "box", "width": 100, "height": 100, "depth": 100}, "hollow": False, "wall_mm": None, "features": [], "build_order": []}


def _generate_from_structured_plan(plan: dict[str, Any], context: dict[str, str]) -> str:
    """Emit CadQuery code from structured plan (base_shape, hollow, features, build_order)."""
    part_id = plan.get("part_id", "aria_part")
    base = plan.get("base_shape", {})
    hollow = plan.get("hollow", False)
    wall_mm = plan.get("wall_mm")
    features = plan.get("features", [])
    constants = get_mechanical_constants(context)

    # Dispatch by part_id for known parts (so we get correct code); generic by base_shape type
    if part_id == "aria_housing":
        return _code_housing(constants)
    if part_id == "aria_spool":
        return _code_spool(constants, plan)
    if part_id == "aria_cam_collar":
        return _code_cam_collar(constants)
    if part_id == "aria_rope_guide":
        return _code_rope_guide(constants)
    if part_id == "aria_motor_mount":
        return _code_motor_mount(constants)
    if part_id == "aria_ratchet_ring":
        return _code_ratchet_ring(plan, context)
    if part_id == "aria_catch_pawl":
        return _code_catch_pawl(plan, context)
    if part_id == "aria_flyweight":
        return _code_flyweight(plan, context)
    if part_id == "aria_brake_drum":
        return _code_brake_drum(plan, context)
    if part_id == "aria_spool_hub":
        return _code_spool_hub(plan, context)
    if part_id == "aria_trip_lever":
        return _code_trip_lever(plan, context)
    if part_id == "aria_shaft_collar":
        return _code_shaft_collar(plan, context)
    return _code_generic(base, hollow, wall_mm, features)


def _code_housing(c: dict[str, float]) -> str:
    w = c.get("housing_width", 700.0)
    h = c.get("housing_height", 680.0)
    d = c.get("housing_depth", 344.0)
    wall = c.get("wall_thickness", 10.0)
    bore_d = c.get("bearing_od", 47.2)
    cx = c.get("spool_center_x", 350.0)
    cy = c.get("spool_center_y", 330.0)
    ratchet_dia = c.get("ratchet_pocket_dia", 213.0)
    ratchet_depth = c.get("ratchet_pocket_depth", 21.0)
    slot_w = c.get("rope_slot_width", 30.0)
    slot_l = c.get("rope_slot_length", 80.0)
    return f'''import cadquery as cq

# 1. Solid box first (no annular profile)
box = cq.Workplane("XY").box({w}, {h}, {d})
# 2. Cut interior void
inner = cq.Workplane("XY").box({w - 2*wall}, {h - 2*wall}, {d - 2*wall})
result = box.cut(inner)
# 3. Front face bearing bore
result = result.faces(">Z").workplane().center({cx - w/2}, {cy - h/2}).circle({bore_d/2}).cutBlind(-12)
# 4. Back face bearing bore
result = result.faces("<Z").workplane().center({cx - w/2}, {cy - h/2}).circle({bore_d/2}).cutBlind(-12)
# 5. Back face ratchet pocket
result = result.faces("<Z").workplane().center({cx - w/2}, {cy - h/2}).circle({ratchet_dia/2}).cutBlind(-{ratchet_depth})
# 6. Top face rope slot
result = result.faces(">Y").workplane().center(0, 0).rect({slot_w}, {slot_l}).cutBlind(-15)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_spool(c: dict[str, float], plan: Optional[dict[str, Any]] = None) -> str:
    base = (plan or {}).get("base_shape", {}) if isinstance(plan, dict) else {}
    features = (plan or {}).get("features", []) if isinstance(plan, dict) else []
    dia = float(base.get("diameter", c.get("rope_spool_dia", 600.0)))
    height = float(base.get("height", 50.0))
    bore = c.get("bearing_od", 47.2)
    for f in features:
        if isinstance(f, dict) and f.get("type") == "bore":
            bore = float(f.get("diameter", bore))
            break
    wall = 10.0
    return f'''import cadquery as cq

outer = cq.Workplane("XY").circle({dia/2}).extrude({height})
inner = cq.Workplane("XY").circle({dia/2 - wall}).extrude({height})
result = outer.cut(inner).faces(">Z").workplane().hole({bore})
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_cam_collar(c: dict[str, float]) -> str:
    od = c.get("bearing_shoulder_od", 55.0)
    length = 40.0
    bore_d = 25.0
    # Helical ramp is complex in CadQuery; we output a simple cylinder with bore. Ramp can be noted in comment.
    return f'''import cadquery as cq

# Cylindrical collar OD {od} mm, length {length} mm; center bore {bore_d} mm. (Helical ramp omitted for simplicity.)
result = cq.Workplane("XY").circle({od/2}).extrude({length}).faces(">Z").workplane().hole({bore_d})
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_rope_guide(c: dict[str, float]) -> str:
    slot_w = c.get("rope_slot_width", 30.0)
    # Base 80x40x10; slot 30mm centered; 4x 6.5mm holes 15mm from edges
    return f'''import cadquery as cq

result = cq.Workplane("XY").box(80, 40, 10)
# Centered slot (rope slot width from aria_mechanical)
result = result.faces(">Z").workplane().center(0, 0).rect({slot_w}, 40).cutBlind(-10)
# 4x M6 holes at corners: 15mm from edges -> centers at ±(40-15)=±25, ±(20-15)=±5
for (dx, dy) in [(25, 5), (-25, 5), (-25, -5), (25, -5)]:
    result = result.faces(">Z").workplane().center(dx, dy).hole(6.5)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_motor_mount(c: dict[str, float]) -> str:
    # 120x120x8 plate; center bore 22mm; 4x M5 on 98mm BCD (radius 49); 4x M6 corners 10mm from edges
    import math
    r = 49.0  # 98/2
    bolt_pos = [(r * math.cos(a), r * math.sin(a)) for a in [0, math.pi/2, math.pi, 3*math.pi/2]]
    corners = [(50, 50), (-50, 50), (-50, -50), (50, -50)]
    return f'''import cadquery as cq

result = cq.Workplane("XY").box(120, 120, 8)
result = result.faces(">Z").workplane().center(0, 0).hole(22)
for x, y in {bolt_pos!r}:
    result = result.faces(">Z").workplane().center(x, y).hole(6.5)
for x, y in {corners!r}:
    result = result.faces(">Z").workplane().center(x, y).hole(6.5)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_ratchet_ring(plan: dict[str, Any], context: dict[str, str]) -> str:
    base = plan.get("base_shape", {})
    od = float(base.get("od", 240.0))
    bore = float(base.get("bore", 120.0))
    h = float(base.get("height", 187.79))
    n = 66
    for f in plan.get("features", []) or []:
        if isinstance(f, dict) and f.get("type") == "teeth":
            n = int(f.get("count", 66))
            break
    tooth_h = 4.0
    tooth_w = round((3.14159 * od / max(n, 1)) * 0.45, 2)
    return f'''import cadquery as cq
import math
OD = {od}
BORE = {bore}
H = {h}
N = {n}
TOOTH_H = {tooth_h}
TOOTH_W = {tooth_w}
outer = cq.Workplane("XY").circle(OD / 2).extrude(H)
result = outer.faces(">Z").workplane().circle(BORE / 2).cutThruAll()
pitch = 360.0 / N
for i in range(N):
    a = math.radians(i * pitch)
    cx = math.cos(a) * (OD / 2 - TOOTH_H / 2)
    cy = math.sin(a) * (OD / 2 - TOOTH_H / 2)
    result = result.faces(">Z").workplane().center(cx, cy).rect(TOOTH_W, TOOTH_H).cutBlind(-H)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_catch_pawl(plan: dict[str, Any], context: dict[str, str]) -> str:
    return '''import cadquery as cq
result = cq.Workplane("XY").box(55, 9, 22)
result = result.faces(">Z").workplane().center(-20, 0).hole(6)
bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.2f},{bb.ylen:.2f},{bb.zlen:.2f}")
'''


def _code_flyweight(plan: dict[str, Any], context: dict[str, str]) -> str:
    base = plan.get("base_shape", {})
    r = float(base.get("diameter", 120.0)) / 2.0
    h = float(base.get("height", 20.0))
    return f'''import cadquery as cq
result = cq.Workplane("XY").circle({r}).extrude({h})
result = result.faces(">Z").workplane().hole(8)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_brake_drum(plan: dict[str, Any], context: dict[str, str]) -> str:
    base = plan.get("base_shape", {})
    od = float(base.get("od", 200.0))
    wall = float(plan.get("wall_mm", 3.0) or 3.0)
    h = float(base.get("height", 50.0))
    inner_r = max(od / 2 - wall, 0.5)
    return f'''import cadquery as cq
outer = cq.Workplane("XY").circle({od / 2}).extrude({h})
result = outer.faces(">Z").workplane().circle({inner_r}).cutThruAll()
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_spool_hub(plan: dict[str, Any], context: dict[str, str]) -> str:
    base = plan.get("base_shape", {})
    dia = float(base.get("diameter", 120.0))
    h = float(base.get("height", 50.0))
    return f'''import cadquery as cq
result = cq.Workplane("XY").circle({dia / 2}).extrude({h})
result = result.faces(">Z").workplane().hole(47.2)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_trip_lever(plan: dict[str, Any], context: dict[str, str]) -> str:
    """Prismatic trip lever — planner uses width/height/depth as L/H/W."""
    base = plan.get("base_shape", {})
    w = float(base.get("width", 60.0))
    h = float(base.get("height", 22.0))
    d = float(base.get("depth", 6.0))
    return f'''import cadquery as cq
result = cq.Workplane("XY").box({w}, {h}, {d})
result = result.faces(">Z").workplane().center(-15, 0).hole(4.2)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_shaft_collar(plan: dict[str, Any], context: dict[str, str]) -> str:
    """Simple annular collar; defaults if plan has no base_shape."""
    base = plan.get("base_shape", {})
    od = float(base.get("od", base.get("diameter", 40.0)))
    h = float(base.get("height", 12.0))
    bore = float(base.get("bore", 20.0))
    return f'''import cadquery as cq
outer = cq.Workplane("XY").circle({od / 2}).extrude({h})
result = outer.faces(">Z").workplane().circle({bore / 2}).cutThruAll()
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.2f}},{{bb.ylen:.2f}},{{bb.zlen:.2f}}")
'''


def _code_from_parsed_spec(spec: dict[str, Any]) -> Optional[str]:
    """Generate CadQuery from goal_parser spec. Returns code string or None if spec too minimal."""
    base = spec.get("base_shape", {})
    if not base:
        return None
    lines = ["import cadquery as cq", ""]
    t = base.get("type", "box")
    if t == "box":
        w, h, d = base.get("width", 100), base.get("height", 100), base.get("depth", 100)
        wall = spec.get("wall_mm")
        if wall and wall > 0:
            lines.append(f"box = cq.Workplane(\"XY\").box({w}, {h}, {d})")
            lines.append(f"inner = cq.Workplane(\"XY\").box({w - 2*wall}, {h - 2*wall}, {d - 2*wall})")
            lines.append("result = box.cut(inner)")
        else:
            lines.append(f"result = cq.Workplane(\"XY\").box({w}, {h}, {d})")
    else:
        dia, height = base.get("diameter", 50), base.get("height", 20)
        wall = spec.get("wall_mm")
        if wall and wall > 0:
            r_outer, r_inner = dia / 2, (dia / 2) - wall
            lines.append(f"outer = cq.Workplane(\"XY\").cylinder({height}, {r_outer})")
            lines.append(f"inner = cq.Workplane(\"XY\").cylinder({height}, {r_inner})")
            lines.append("result = outer.cut(inner)")
        else:
            lines.append(f"result = cq.Workplane(\"XY\").cylinder({height}, {dia/2})")
    # Center hole
    ch = spec.get("center_hole")
    if ch:
        d_hole = ch.get("diameter", 10)
        lines.append(f"result = result.faces(\">Z\").workplane().center(0, 0).hole({d_hole})")
    # Bolt circle
    bc = spec.get("bolt_circle")
    if bc:
        import math
        r = bc["bolt_circle_diameter"] / 2
        n = int(bc.get("count", 4))
        hole_d = bc.get("hole_diameter", 6.5)
        angles = [2 * math.pi * i / n for i in range(n)]
        positions = [(r * math.cos(a), r * math.sin(a)) for a in angles]
        lines.append(f"for x, y in {positions!r}:")
        lines.append(f"    result = result.faces(\">Z\").workplane().center(x, y).hole({hole_d})")
    # Slot (box only)
    slot = spec.get("slot")
    if slot and t == "box":
        sw, sl, sd = slot.get("width", 20), slot.get("length", 40), slot.get("depth", 10)
        lines.append(f"result = result.faces(\">Z\").workplane().center(0, 0).rect({sw}, {sl}).cutBlind(-{sd})")
    lines.append("bb = result.val().BoundingBox()")
    lines.append('print(f"BBOX:{bb.xlen:.2f},{bb.ylen:.2f},{bb.zlen:.2f}")')
    return "\n".join(lines)


def _code_generic(base: dict, hollow: bool, wall_mm: Any, features: list) -> str:
    """Generic code from base_shape + features."""
    t = base.get("type", "box")
    if t == "box":
        w, h, d = base.get("width", 100), base.get("height", 100), base.get("depth", 100)
        if hollow and wall_mm:
            code = f'''import cadquery as cq\nbox = cq.Workplane("XY").box({w}, {h}, {d})\ninner = cq.Workplane("XY").box({w-2*wall_mm}, {h-2*wall_mm}, {d-2*wall_mm})\nresult = box.cut(inner)'''
        else:
            code = f'''import cadquery as cq\nresult = cq.Workplane("XY").box({w}, {h}, {d})'''
    else:
        dia, height = base.get("diameter", 50), base.get("height", 30)
        code = f'''import cadquery as cq\nresult = cq.Workplane("XY").circle({dia/2}).extrude({height})'''
    code += "\nbb = result.val().BoundingBox()\nprint(f\"BBOX:{bb.xlen:.2f},{bb.ylen:.2f},{bb.zlen:.2f}\")"
    return code
