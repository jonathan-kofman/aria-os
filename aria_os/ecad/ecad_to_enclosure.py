"""
ECAD-to-Enclosure Bridge — Parse .kicad_pcb files and generate enclosures.

Reads KiCad PCB files (s-expression format), extracts board outline,
mounting holes, and connector positions, then generates parametric
CadQuery enclosures with proper cutouts, standoffs, and ventilation.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aria_os.ecad_to_enclosure")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MountingHole:
    x_mm: float
    y_mm: float
    diameter_mm: float


@dataclass
class ConnectorPosition:
    name: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float  # above board surface
    edge: str = "none"  # top | bottom | left | right | none


@dataclass
class PCBGeometry:
    """Extracted board geometry from .kicad_pcb file."""
    board_outline: list[tuple[float, float]] = field(default_factory=list)
    board_width_mm: float = 0.0
    board_height_mm: float = 0.0
    board_thickness_mm: float = 1.6
    mounting_holes: list[MountingHole] = field(default_factory=list)
    connectors: list[ConnectorPosition] = field(default_factory=list)
    tallest_component_mm: float = 10.0


@dataclass
class EnclosureOptions:
    wall_thickness_mm: float = 2.0
    clearance_mm: float = 1.0
    standoff_height_mm: float = 5.0
    standoff_od_mm: float = 6.0
    standoff_id_mm: float = 2.5  # M2.5 screw
    ventilation: bool = True
    vent_slot_width_mm: float = 1.5
    vent_slot_length_mm: float = 15.0
    closure_type: str = "screw"  # screw | snap_fit
    fillet_radius_mm: float = 2.0
    lid_lip_mm: float = 1.5


@dataclass
class EnclosureResult:
    script: str  # CadQuery Python code
    step_paths: dict = field(default_factory=dict)
    stl_paths: dict = field(default_factory=dict)
    dxf_path: Optional[str] = None
    pcb_geometry: Optional[PCBGeometry] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# KiCad PCB parser
# ---------------------------------------------------------------------------

# S-expression tokenizer
_TOKEN_RE = re.compile(r'"[^"]*"|[()]|[^\s()]+')


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _parse_sexpr(tokens: list[str], idx: int = 0) -> tuple:
    """Parse s-expression tokens into nested lists."""
    result = []
    while idx < len(tokens):
        token = tokens[idx]
        if token == "(":
            child, idx = _parse_sexpr(tokens, idx + 1)
            result.append(child)
        elif token == ")":
            return result, idx + 1
        else:
            # Strip quotes
            if token.startswith('"') and token.endswith('"'):
                token = token[1:-1]
            result.append(token)
            idx += 1
    return result, idx


def _find_nodes(tree: list, name: str) -> list:
    """Find all child nodes with given name."""
    results = []
    for item in tree:
        if isinstance(item, list) and len(item) > 0 and item[0] == name:
            results.append(item)
    return results


def _find_node(tree: list, name: str):
    nodes = _find_nodes(tree, name)
    return nodes[0] if nodes else None


def _get_value(node: list, key: str, default=None):
    """Get value from (key value) pair in a node."""
    sub = _find_node(node, key)
    if sub and len(sub) > 1:
        return sub[1]
    return default


# Connector footprint patterns
_CONNECTOR_PATTERNS = [
    "USB", "JST", "Molex", "BarrelJack", "Barrel_Jack",
    "PinHeader", "Pin_Header", "HDMI", "RJ45", "Ethernet",
    "MicroUSB", "USB_C", "USB-C", "TypeC",
]

# Typical connector heights above board (mm)
_CONNECTOR_HEIGHTS = {
    "USB": 7.0, "USB_C": 3.5, "MicroUSB": 2.5,
    "JST": 8.0, "Molex": 6.0, "BarrelJack": 11.0,
    "Barrel_Jack": 11.0, "PinHeader": 8.5, "Pin_Header": 8.5,
    "HDMI": 6.5, "RJ45": 13.5, "Ethernet": 13.5,
}


def parse_kicad_pcb(pcb_path: str) -> PCBGeometry:
    """Parse a .kicad_pcb file and extract board geometry.

    Extracts:
    - Board outline from Edge.Cuts layer (gr_line, gr_arc)
    - Mounting holes from footprints with "MountingHole" reference
    - Connector positions from footprints with connector-type references
    """
    pcb_path = Path(pcb_path)
    if not pcb_path.exists():
        raise FileNotFoundError(f"PCB file not found: {pcb_path}")

    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    tokens = _tokenize(text)
    tree, _ = _parse_sexpr(tokens)

    # The top-level should be a kicad_pcb node
    pcb_node = tree
    if isinstance(tree, list) and len(tree) == 1 and isinstance(tree[0], list):
        pcb_node = tree[0]

    geometry = PCBGeometry()

    # Extract board outline from gr_line on Edge.Cuts
    outline_points = set()
    for gr_line in _find_nodes(pcb_node, "gr_line"):
        layer = _get_value(gr_line, "layer")
        if layer and "Edge.Cuts" in str(layer):
            start = _find_node(gr_line, "start")
            end = _find_node(gr_line, "end")
            if start and end and len(start) >= 3 and len(end) >= 3:
                try:
                    sx, sy = float(start[1]), float(start[2])
                    ex, ey = float(end[1]), float(end[2])
                    outline_points.add((sx, sy))
                    outline_points.add((ex, ey))
                except (ValueError, IndexError):
                    pass

    if outline_points:
        xs = [p[0] for p in outline_points]
        ys = [p[1] for p in outline_points]
        geometry.board_outline = list(outline_points)
        geometry.board_width_mm = max(xs) - min(xs)
        geometry.board_height_mm = max(ys) - min(ys)

    # Extract footprints
    tallest = 0.0
    for fp in _find_nodes(pcb_node, "footprint"):
        fp_lib = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""

        # Get position
        at_node = _find_node(fp, "at")
        if not at_node or len(at_node) < 3:
            continue
        try:
            fp_x = float(at_node[1])
            fp_y = float(at_node[2])
        except (ValueError, IndexError):
            continue

        # Check for mounting holes
        if "MountingHole" in fp_lib or "mounting" in fp_lib.lower():
            # Find pad diameter
            diameter = 3.2  # default M3
            for pad in _find_nodes(fp, "pad"):
                size_node = _find_node(pad, "size")
                if size_node and len(size_node) >= 2:
                    try:
                        diameter = float(size_node[1])
                    except (ValueError, IndexError):
                        pass
            geometry.mounting_holes.append(MountingHole(
                x_mm=fp_x, y_mm=fp_y, diameter_mm=diameter,
            ))
            continue

        # Check for connectors
        for pattern in _CONNECTOR_PATTERNS:
            if pattern.lower() in fp_lib.lower():
                # Estimate connector dimensions from pads
                conn_w, conn_h = 10.0, _CONNECTOR_HEIGHTS.get(pattern, 8.0)
                tallest = max(tallest, conn_h)

                # Determine which board edge it's near
                edge = _classify_edge(
                    fp_x, fp_y,
                    geometry.board_width_mm, geometry.board_height_mm,
                    outline_points,
                )

                # Get reference designator
                ref = ""
                for prop in _find_nodes(fp, "property"):
                    if len(prop) > 1 and prop[1] == "Reference" and len(prop) > 2:
                        ref = prop[2]

                geometry.connectors.append(ConnectorPosition(
                    name=ref or pattern,
                    x_mm=fp_x, y_mm=fp_y,
                    width_mm=conn_w, height_mm=conn_h,
                    edge=edge,
                ))
                break

    if tallest > 0:
        geometry.tallest_component_mm = tallest

    return geometry


def _classify_edge(x: float, y: float, board_w: float, board_h: float,
                   outline_points: set) -> str:
    """Classify which board edge a component is closest to."""
    if not outline_points or board_w == 0 or board_h == 0:
        return "none"

    xs = [p[0] for p in outline_points]
    ys = [p[1] for p in outline_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    margin = 3.0  # mm from edge to be considered "on edge"

    distances = {
        "left":   abs(x - min_x),
        "right":  abs(x - max_x),
        "top":    abs(y - min_y),
        "bottom": abs(y - max_y),
    }
    closest = min(distances, key=distances.get)
    if distances[closest] <= margin:
        return closest
    return "none"


# ---------------------------------------------------------------------------
# CadQuery enclosure generator
# ---------------------------------------------------------------------------

def generate_enclosure_script(pcb: PCBGeometry,
                               options: Optional[EnclosureOptions] = None) -> str:
    """Generate CadQuery Python code for a two-part enclosure."""
    if options is None:
        options = EnclosureOptions()

    w = options.wall_thickness_mm
    cl = options.clearance_mm
    lip = options.lid_lip_mm

    # Outer dimensions
    outer_w = pcb.board_width_mm + 2 * (cl + w)
    outer_h = pcb.board_height_mm + 2 * (cl + w)
    # Bottom shell height: standoff + board + component clearance + wall
    bottom_h = options.standoff_height_mm + pcb.board_thickness_mm + pcb.tallest_component_mm + w
    # Top shell height (lid)
    top_h = w + lip + 2.0  # small clearance above components

    fillet = min(options.fillet_radius_mm, w * 0.9)

    # Build CadQuery script
    lines = [
        "import cadquery as cq",
        "",
        f"# Auto-generated enclosure for {pcb.board_width_mm:.1f}x{pcb.board_height_mm:.1f}mm PCB",
        f"OUTER_W = {outer_w:.2f}",
        f"OUTER_H = {outer_h:.2f}",
        f"BOTTOM_H = {bottom_h:.2f}",
        f"TOP_H = {top_h:.2f}",
        f"WALL = {w:.2f}",
        f"FILLET = {fillet:.2f}",
        f"STANDOFF_H = {options.standoff_height_mm:.2f}",
        f"STANDOFF_OD = {options.standoff_od_mm:.2f}",
        f"STANDOFF_ID = {options.standoff_id_mm:.2f}",
        f"LIP = {lip:.2f}",
        "",
        "# --- Bottom shell ---",
        "bottom = (",
        "    cq.Workplane('XY')",
        "    .box(OUTER_W, OUTER_H, BOTTOM_H, centered=(True, True, False))",
        f"    .edges('|Z').fillet(FILLET)",
        "    # Hollow out",
        f"    .faces('>Z').shell(-WALL)",
        ")",
        "",
    ]

    # Add standoffs at mounting hole positions
    if pcb.mounting_holes:
        # Calculate board origin offset (center of enclosure = center of board)
        lines.append("# Standoffs at mounting holes")
        for i, hole in enumerate(pcb.mounting_holes):
            # Convert from PCB coordinates to enclosure-centered coordinates
            cx = hole.x_mm - pcb.board_width_mm / 2
            cy = -(hole.y_mm - pcb.board_height_mm / 2)  # KiCad Y is inverted
            lines.extend([
                f"bottom = bottom.union(",
                f"    cq.Workplane('XY').transformed(offset=({cx:.2f}, {cy:.2f}, 0))",
                f"    .circle(STANDOFF_OD/2).extrude(STANDOFF_H)",
                f"    .faces('>Z').hole(STANDOFF_ID)",
                f")",
            ])
        lines.append("")

    # Add connector cutouts
    if pcb.connectors:
        lines.append("# Connector cutouts")
        for conn in pcb.connectors:
            if conn.edge == "none":
                continue
            cx = conn.x_mm - pcb.board_width_mm / 2
            cy = -(conn.y_mm - pcb.board_height_mm / 2)
            cut_z = options.standoff_height_mm + pcb.board_thickness_mm / 2
            cw = conn.width_mm + 1.0  # 0.5mm clearance each side
            ch = conn.height_mm + 1.0

            if conn.edge in ("left", "right"):
                face_sel = "'<X'" if conn.edge == "left" else "'>X'"
                lines.extend([
                    f"bottom = (",
                    f"    bottom.faces({face_sel}).workplane(centerOption='CenterOfBoundBox')",
                    f"    .transformed(offset=(0, {cy:.2f}, {cut_z:.2f}))",
                    f"    .rect({ch:.2f}, {cw:.2f}).cutBlind(-WALL*2)",
                    f")",
                ])
            elif conn.edge in ("top", "bottom"):
                face_sel = "'<Y'" if conn.edge == "bottom" else "'>Y'"
                lines.extend([
                    f"bottom = (",
                    f"    bottom.faces({face_sel}).workplane(centerOption='CenterOfBoundBox')",
                    f"    .transformed(offset=({cx:.2f}, 0, {cut_z:.2f}))",
                    f"    .rect({cw:.2f}, {ch:.2f}).cutBlind(-WALL*2)",
                    f")",
                ])
        lines.append("")

    # Add ventilation slots
    if options.ventilation:
        lines.extend([
            "# Ventilation slots on top face",
            "vent_count = max(1, int(OUTER_W / 5))",
            "vent_spacing = OUTER_W / (vent_count + 1)",
            "for i in range(vent_count):",
            f"    x_pos = -OUTER_W/2 + vent_spacing * (i + 1)",
            "    bottom = (",
            "        bottom.faces('>Z').workplane(centerOption='CenterOfBoundBox')",
            f"        .transformed(offset=(x_pos, 0, 0))",
            f"        .slot2D({options.vent_slot_length_mm}, {options.vent_slot_width_mm}).cutBlind(-WALL)",
            "    )",
            "",
        ])

    # Top shell (lid)
    lines.extend([
        "# --- Top shell (lid) ---",
        "top = (",
        "    cq.Workplane('XY')",
        "    .box(OUTER_W, OUTER_H, TOP_H, centered=(True, True, False))",
        f"    .edges('|Z').fillet(FILLET)",
        f"    .faces('<Z').shell(-WALL)",
        ")",
        "",
        "# Lip for alignment",
        "top = top.union(",
        f"    cq.Workplane('XY').box(OUTER_W - WALL*2 - 0.3, OUTER_H - WALL*2 - 0.3, LIP, centered=(True, True, False))",
        ")",
        "",
        "# Export",
        "result = bottom  # primary result",
        "bb = result.val().BoundingBox()",
        'print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")',
    ])

    return "\n".join(lines)


def generate_enclosure_from_pcb(pcb_path: str,
                                 options: Optional[EnclosureOptions] = None,
                                 output_dir: Optional[str] = None) -> EnclosureResult:
    """Full pipeline: parse PCB -> generate enclosure -> export files."""
    try:
        pcb = parse_kicad_pcb(pcb_path)
    except Exception as exc:
        return EnclosureResult(script="", error=str(exc))

    if pcb.board_width_mm == 0 or pcb.board_height_mm == 0:
        return EnclosureResult(
            script="",
            error="Could not determine board dimensions from PCB file",
            pcb_geometry=pcb,
        )

    script = generate_enclosure_script(pcb, options)

    if output_dir is None:
        output_dir = str(Path(pcb_path).parent / "enclosure")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Write script
    script_file = out_path / "enclosure_cq.py"
    script_file.write_text(script)

    # Try to execute and export
    step_paths = {}
    stl_paths = {}
    try:
        import cadquery as cq

        # Execute in controlled namespace
        ns = {"cq": cq}
        exec(script, ns)

        bottom = ns.get("bottom") or ns.get("result")
        top = ns.get("top")

        if bottom:
            bottom_step = str(out_path / "enclosure_bottom.step")
            bottom_stl = str(out_path / "enclosure_bottom.stl")
            cq.exporters.export(bottom, bottom_step)
            cq.exporters.export(bottom, bottom_stl)
            step_paths["bottom"] = bottom_step
            stl_paths["bottom"] = bottom_stl

        if top:
            top_step = str(out_path / "enclosure_top.step")
            top_stl = str(out_path / "enclosure_top.stl")
            cq.exporters.export(top, top_step)
            cq.exporters.export(top, top_stl)
            step_paths["top"] = top_step
            stl_paths["top"] = top_stl

    except ImportError:
        logger.warning("CadQuery not installed — script saved but not executed")
    except Exception as exc:
        logger.error("Enclosure execution failed: %s", exc)

    return EnclosureResult(
        script=script,
        step_paths=step_paths,
        stl_paths=stl_paths,
        pcb_geometry=pcb,
    )
