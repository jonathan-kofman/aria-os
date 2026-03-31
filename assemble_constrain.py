"""
assemble_constrain.py
---------------------
Reads an assembly JSON config (same format used by assemble.py / batch.py) and
generates a Fusion 360 Python API script that:
  1. Imports every STEP file as a component with the correct transform.
  2. Creates as-built joints (revolute / rigid / slider / ball) between pairs of
     parts that are spatially close AND match a joint-rule pattern.

Usage
-----
  python assemble_constrain.py assembly_configs/clock_gear_train.json
  python assemble_constrain.py assembly_configs/f1_car_18.json --proximity 80
  python assemble_constrain.py assembly_configs/welding_robot_7dof.json \\
        --out outputs/cad/fusion_scripts/welding_robot_constrained.py

Input JSON format
-----------------
{
  "name": "Assembly Name",
  "parts": [
    {"id": "barrel_arbor", "step": "outputs/cad/step/barrel_arbor.step",
     "pos": [0, 0, 0], "rot": [0, 0, 0]},
    ...
  ]
}

The generated script is pasted into the Fusion 360 Script editor and executed
there — it does NOT run standalone.
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional numpy — fall back to pure Python if not installed
# ---------------------------------------------------------------------------
try:
    import numpy as np
    _NUMPY = True
except ImportError:  # pragma: no cover
    _NUMPY = False


# ---------------------------------------------------------------------------
# Joint classification rules
# (pattern_a, pattern_b, joint_type, direction)
# Patterns are tried against both (a→b) and (b→a) orderings.
# joint_type : "revolute" | "rigid" | "slider" | "ball"
# direction  : "z" | "x" | "y" | None
# ---------------------------------------------------------------------------
JOINT_RULES = [
    # Shaft/arbor → wheel/gear/drum/cap/flywheel
    (r"shaft|arbor|axle|spindle",       r"wheel|gear|drum|cap|flywheel|sprocket",   "revolute", "z"),
    # Arbor → housing/bearing/collar
    (r"shaft|arbor|axle",               r"housing|bearing|collar|bushing|sleeve",   "revolute", "z"),
    # Pinion → wheel/gear (gear mesh)
    (r"pinion|spur|helical",            r"wheel|gear|ring",                          "revolute", "z"),
    # Motor → housing/mount/plate/bracket
    (r"motor|nema|servo|stepper",       r"housing|mount|bracket|plate|frame",        "rigid",    None),
    # Pillar/column/post/standoff → plate/frame/base
    (r"pillar|column|post|rod|standoff",r"plate|frame|base|deck|panel",              "rigid",    None),
    # Suspension arm/wishbone → upright/knuckle/hub
    (r"arm|wishbone|link|rod",          r"upright|knuckle|hub|spindle",              "revolute", "y"),
    # Arm/wishbone → chassis/frame (pivot)
    (r"arm|wishbone|link",              r"chassis|monocoque|frame|tub",              "revolute", "y"),
    # Rail/track → carriage/slider/block
    (r"rail|track|guide",               r"carriage|slider|block|saddle",             "slider",   "z"),
    # Piston → cylinder/bore
    (r"piston|plunger|ram",             r"cylinder|bore|tube",                       "slider",   "z"),
    # Wheel → axle/spindle
    (r"wheel|tire|tyre",                r"axle|spindle|stub",                        "revolute", "z"),
    # Brake → rotor/disc
    (r"brake|calliper|caliper",         r"rotor|disc|disk",                          "revolute", "z"),
    # Wing/aero → chassis/body/endplate
    (r"wing|flap|spoiler|drs",          r"chassis|body|endplate|monocoque",          "rigid",    None),
    # Endplate → wing
    (r"endplate",                       r"wing|plane|flap",                          "rigid",    None),
    # Upright/knuckle → axle/shaft
    (r"upright|knuckle",                r"axle|shaft|spindle",                       "revolute", "z"),
    # Bolt/screw/fastener → anything
    (r"bolt|screw|fastener|nut",        r".*",                                       "rigid",    None),
    # Anything → chassis/monocoque/frame/base/housing (catch-all)
    (r".*",                             r"chassis|monocoque|frame|base|housing",     "rigid",    None),
]

# Direction string → Fusion 360 JointDirections enum member
_DIRECTION_MAP = {
    "z": "adsk.fusion.JointDirections.ZAxisJointDirection",
    "x": "adsk.fusion.JointDirections.XAxisJointDirection",
    "y": "adsk.fusion.JointDirections.YAxisJointDirection",
}


# ---------------------------------------------------------------------------
# Joint classification
# ---------------------------------------------------------------------------
def _classify_joint(id_a: str, id_b: str) -> tuple:
    """Return (joint_type, direction_str_or_None)."""
    for pat_a, pat_b, jtype, jdir in JOINT_RULES:
        if (re.search(pat_a, id_a, re.I) and re.search(pat_b, id_b, re.I)) or \
           (re.search(pat_a, id_b, re.I) and re.search(pat_b, id_a, re.I)):
            return jtype, jdir
    return "rigid", None


# ---------------------------------------------------------------------------
# Proximity detection
# ---------------------------------------------------------------------------
def _distance(a, b) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _distance_np(a, b) -> float:
    import numpy as np  # noqa: F401 – already confirmed available
    return float(np.linalg.norm(np.array(a, dtype=float) - np.array(b, dtype=float)))


def find_mating_pairs(parts: list, proximity_mm: float = 50.0) -> list:
    """
    Return list of (i, j, joint_type, direction) for part pairs within
    proximity_mm of each other.
    """
    dist_fn = _distance_np if _NUMPY else _distance
    n = len(parts)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            d = dist_fn(parts[i]["pos"], parts[j]["pos"])
            if d <= proximity_mm:
                jtype, jdir = _classify_joint(parts[i]["id"], parts[j]["id"])
                pairs.append((i, j, jtype, jdir))
    return pairs


# ---------------------------------------------------------------------------
# Code-generation helpers
# ---------------------------------------------------------------------------
_HEADER_TEMPLATE = '''\
# =============================================================================
# Fusion 360 constrained assembly script
# Generated by assemble_constrain.py
#
# Assembly : {name}
# Parts    : {n_parts}
# Joints   : {n_joints}
# Proximity: {proximity} mm
#
# HOW TO RUN
# ----------
#   1. Open Fusion 360 and create (or open) a design.
#   2. Tools > Scripts and Add-Ins > Scripts > "+" > browse to this file.
#   3. Click "Run".
#
# All STEP paths are embedded as absolute strings resolved at generation time.
# If you move files, update the STEP_PATHS dict below.
# =============================================================================

import adsk.core
import adsk.fusion
import math
import traceback

# ---------------------------------------------------------------------------
# STEP file paths (absolute at generation time)
# ---------------------------------------------------------------------------
STEP_PATHS = {{
{step_paths_block}}}
'''

_RUN_OPEN = '''\

def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        design = app.activeProduct
        root = design.rootComponent
        import_mgr = app.importManager

        occurrences = {}  # part_id -> occurrence
'''

_MAKE_TRANSFORM_FN = '''\
def _make_transform(pos, rot_deg):
    """Build a Matrix3D from pos [mm] and rot [degrees, XYZ Euler]."""
    matrix = adsk.core.Matrix3D.create()
    rx, ry, rz = (math.radians(r) for r in rot_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    r00 = cy * cz;          r01 = cy * sz;          r02 = -sy
    r10 = sx*sy*cz - cx*sz; r11 = sx*sy*sz + cx*cz; r12 = sx * cy
    r20 = cx*sy*cz + sx*sz; r21 = cx*sy*sz - sx*cz; r22 = cx * cy
    # Fusion internal units are cm — convert mm → cm
    tx, ty, tz = pos[0] / 10.0, pos[1] / 10.0, pos[2] / 10.0
    matrix.setWithArray([
        r00, r01, r02, tx,
        r10, r11, r12, ty,
        r20, r21, r22, tz,
        0,   0,   0,   1,
    ])
    return matrix

'''


def _indent(text: str, spaces: int = 8) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def _step_paths_block(parts: list, repo_root: Path) -> str:
    lines = []
    for p in parts:
        step_raw = p.get("step", "")
        # Resolve relative paths against repo root
        step_path = Path(step_raw)
        if not step_path.is_absolute():
            step_path = repo_root / step_path
        # Normalise to forward slashes for readability inside the script
        step_str = step_path.as_posix()
        lines.append(f'    "{p["id"]}": r"{step_str}",')
    return "\n".join(lines) + "\n"


def _import_block(parts: list) -> str:
    lines = []
    for p in parts:
        pid = p["id"]
        pos = p.get("pos", [0, 0, 0])
        rot = p.get("rot", [0, 0, 0])
        lines.append(f"        # --- {pid} ---")
        lines.append(f"        _step = STEP_PATHS['{pid}']")
        lines.append(f"        _opt = import_mgr.createSTEPImportOptions(_step)")
        lines.append(f"        import_mgr.importToTarget(_opt, root)")
        lines.append(f"        _occ = root.occurrences[-1]")
        lines.append(f"        _occ.transform = _make_transform({pos}, {rot})")
        lines.append(f"        occurrences['{pid}'] = _occ")
        lines.append("")
    return "\n".join(lines)


def _direction_code(jdir: str | None) -> str:
    if jdir and jdir in _DIRECTION_MAP:
        return _DIRECTION_MAP[jdir]
    return _DIRECTION_MAP["z"]


def _joint_block(pairs: list, parts: list) -> str:
    if not pairs:
        return "        # No mating pairs found within proximity threshold.\n"

    lines = []
    lines.append("        # ----------------------------------------------------------------")
    lines.append("        # As-built joints")
    lines.append("        # ----------------------------------------------------------------")

    for i, j, jtype, jdir in pairs:
        id_a = parts[i]["id"]
        id_b = parts[j]["id"]
        var_a = f"occurrences['{id_a}']"
        var_b = f"occurrences['{id_b}']"
        label = f"{id_a} <-> {id_b}"

        lines.append(f"")
        lines.append(f"        # {label}  [{jtype}, axis={jdir}]")
        lines.append(f"        _ji = root.asBuiltJoints.createInput({var_a}, {var_b}, None)")

        if jtype == "revolute":
            dir_code = _direction_code(jdir)
            lines.append(f"        _ji.setAsRevoluteJointMotion({dir_code})")
        elif jtype == "slider":
            dir_code = _direction_code(jdir)
            lines.append(f"        _ji.setAsSliderJointMotion({dir_code})")
        elif jtype == "ball":
            lines.append(f"        _ji.setAsBallJointMotion()")
        else:  # rigid (default)
            lines.append(f"        _ji.setAsRigidJointMotion()")

        lines.append(f"        root.asBuiltJoints.add(_ji)")

    return "\n".join(lines) + "\n"


def _success_message(name: str, n_parts: int, n_joints: int) -> str:
    return (
        f"        ui.messageBox(\n"
        f"            'Assembly complete!\\n'\n"
        f"            'Name   : {name}\\n'\n"
        f"            'Parts  : {n_parts}\\n'\n"
        f"            'Joints : {n_joints}\\n'\n"
        f"        )\n"
    )


def _except_block() -> str:
    return (
        "    except:  # noqa: E722\n"
        "        if ui:\n"
        "            ui.messageBox('Error:\\n' + traceback.format_exc())\n"
    )


# ---------------------------------------------------------------------------
# Component registry stub
# ---------------------------------------------------------------------------
def _resolve_step_paths(parts: list, repo_root: Path) -> list:
    """
    Check each part's STEP path and warn (not error) if the file is missing.
    Returns the parts list unchanged — the Fusion script will fail at runtime
    for genuinely missing files, which is expected during design iteration.
    """
    missing = []
    for p in parts:
        step_raw = p.get("step", "")
        step_path = Path(step_raw)
        if not step_path.is_absolute():
            step_path = repo_root / step_path
        if not step_path.exists():
            missing.append(str(step_path))

    if missing:
        print("WARNING: The following STEP files were not found on disk.")
        print("  The generated script will fail at runtime until they are generated.")
        for m in missing:
            print(f"  MISSING: {m}")
        print()

    return parts


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------
def generate_constrained_script(
    config_path: Path,
    proximity_mm: float = 50.0,
    out_path: Path | None = None,
) -> Path:
    """
    Read the assembly JSON at config_path, compute joints, and write the
    Fusion 360 script.  Returns the path of the written script.
    """
    repo_root = Path(__file__).parent.resolve()

    with config_path.open("r", encoding="utf-8") as fh:
        config = json.load(fh)

    name: str = config.get("name", config_path.stem)
    parts: list = config.get("parts", [])

    if not parts:
        print("ERROR: No parts found in assembly config.", file=sys.stderr)
        sys.exit(1)

    # Ensure every part has pos and rot
    for p in parts:
        p.setdefault("pos", [0, 0, 0])
        p.setdefault("rot", [0, 0, 0])

    # Warn about missing STEP files
    _resolve_step_paths(parts, repo_root)

    # Find mating pairs
    pairs = find_mating_pairs(parts, proximity_mm)

    n_parts = len(parts)
    n_joints = len(pairs)

    # Default output path
    if out_path is None:
        safe_name = re.sub(r"[^\w]+", "_", name).strip("_").lower()
        out_path = repo_root / "outputs" / "cad" / "fusion_scripts" / f"{safe_name}_constrained.py"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # Build the script text
    # ----------------------------------------------------------------
    step_block = _step_paths_block(parts, repo_root)

    header = _HEADER_TEMPLATE.format(
        name=name,
        n_parts=n_parts,
        n_joints=n_joints,
        proximity=proximity_mm,
        step_paths_block=step_block,
    )

    body_lines = [
        header,
        _MAKE_TRANSFORM_FN,
        _RUN_OPEN,
        "        # ----------------------------------------------------------------",
        "        # 1. Import STEP components",
        "        # ----------------------------------------------------------------",
        _import_block(parts),
        "        # ----------------------------------------------------------------",
        "        # 2. As-built joints",
        "        # ----------------------------------------------------------------",
        _joint_block(pairs, parts),
        _success_message(name, n_parts, n_joints),
        _except_block(),
    ]

    script_text = "\n".join(body_lines)

    out_path.write_text(script_text, encoding="utf-8")

    # ----------------------------------------------------------------
    # Summary to stdout
    # ----------------------------------------------------------------
    print(f"Assembly   : {name}")
    print(f"Parts      : {n_parts}")
    print(f"Proximity  : {proximity_mm} mm")
    print(f"Joints     : {n_joints}")
    print()
    if pairs:
        col_w = max(len(parts[i]["id"]) + len(parts[j]["id"]) + 5 for i, j, *_ in pairs)
        header_row = f"  {'Part A <-> Part B':<{col_w}}  Type       Axis"
        print(header_row)
        print("  " + "-" * (len(header_row) - 2))
        for i, j, jtype, jdir in pairs:
            pair_label = f"{parts[i]['id']} <-> {parts[j]['id']}"
            axis_str = jdir if jdir else "-"
            print(f"  {pair_label:<{col_w}}  {jtype:<10} {axis_str}")
    else:
        print("  (no mating pairs found — try increasing --proximity)")

    print()
    print(f"Output     : {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="assemble_constrain.py",
        description=(
            "Generate a Fusion 360 Python API script from an assembly JSON config. "
            "The script imports all STEP components and creates as-built joints between "
            "spatially proximate parts using pattern-matched joint rules."
        ),
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to the assembly JSON config file.",
    )
    parser.add_argument(
        "--proximity",
        type=float,
        default=50.0,
        metavar="MM",
        help=(
            "Maximum centre-to-centre distance (mm) between two parts for them to be "
            "considered mating candidates. Default: 50."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Output path for the generated Fusion 360 script. "
            "Default: outputs/cad/fusion_scripts/<assembly_name>_constrained.py"
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    config_path: Path = args.config
    if not config_path.is_absolute():
        config_path = Path(__file__).parent / config_path

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    generate_constrained_script(
        config_path=config_path,
        proximity_mm=args.proximity,
        out_path=args.out,
    )


if __name__ == "__main__":
    main()
