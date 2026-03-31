"""
Assemble multiple STEP parts with position/rotation and export as a single STEP/STL.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict


@dataclass
class AssemblyPart:
    step_path: str
    position: tuple  # (x, y, z) in mm
    rotation: tuple   # (rx, ry, rz) in degrees
    name: str


class Assembler:
    """Position multiple parts and export as one STEP and one STL."""

    def __init__(self, repo_root: Optional[Path] = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = Path(repo_root)
        self.step_dir = self.repo_root / "outputs" / "cad" / "step"
        self.stl_dir = self.repo_root / "outputs" / "cad" / "stl"
        self.step_dir.mkdir(parents=True, exist_ok=True)
        self.stl_dir.mkdir(parents=True, exist_ok=True)

    def assemble(self, parts: List[AssemblyPart], name: str, constraints: Optional[List[dict]] = None, context: Optional[dict] = None) -> str:
        """
        parts: list of AssemblyPart(step_path, position, rotation, name)
        name: assembly name for output file
        Returns: path to assembly STEP file.
        """
        import cadquery as cq
        from cadquery import Assembly
        from .mating_solver import MatingSolver, MatingConstraint
        from .context_loader import load_context

        # Apply mating constraints if provided
        if constraints:
            if context is None:
                context = load_context(self.repo_root)
            solver = MatingSolver(repo_root=self.repo_root)
            mc = [MatingConstraint(type=c.get("type", ""), part_a=c.get("part_a", ""), part_b=c.get("part_b", ""), params=c.get("params", {})) for c in constraints]
            parts = solver.solve(parts, mc, context)

        assy = Assembly(None, name=name)
        for part in parts:
            step_path = Path(part.step_path)
            if not step_path.exists():
                raise FileNotFoundError(f"STEP not found: {step_path}")
            shape = cq.importers.importStep(str(step_path))
            # Workplane or compound from importStep; Assembly.add accepts Shape or Workplane
            if hasattr(shape, "val") and shape.val() is not None:
                wp = shape
            else:
                wp = cq.Workplane("XY").add(shape)
            # Location: position (x,y,z) + rotation (rx, ry, rz) in degrees. Order: translate then rotate Z, Y, X.
            pos = cq.Vector(part.position[0], part.position[1], part.position[2])
            rx, ry, rz = part.rotation[0], part.rotation[1], part.rotation[2]
            loc = cq.Location(pos)
            if rz != 0:
                loc = loc * cq.Location(cq.Vector(0, 0, 0), cq.Vector(0, 0, 1), rz)
            if ry != 0:
                loc = loc * cq.Location(cq.Vector(0, 0, 0), cq.Vector(0, 1, 0), ry)
            if rx != 0:
                loc = loc * cq.Location(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), rx)
            assy.add(wp, loc=loc, name=part.name)

        step_path = self.step_dir / f"{name}.step"
        stl_path = self.stl_dir / f"{name}.stl"
        assy.export(str(step_path), exportType="STEP")
        assy.export(str(stl_path), exportType="STL")
        return str(step_path)


# ---------------------------------------------------------------------------
# Dependency resolver
# ---------------------------------------------------------------------------

def resolve_depends_on(config_parts: List[Dict]) -> List[Dict]:
    """
    Resolve ``depends_on`` references in an assembly config parts list.

    For each part that has a ``depends_on`` key, its final ``pos`` is computed
    as::

        pos = resolved_pos_of_parent + offset

    where ``offset`` defaults to ``[0, 0, 0]`` if not supplied.

    Chains are supported (A depends on B which depends on C).
    Cycles raise ``ValueError``.

    Parameters
    ----------
    config_parts : list[dict]
        The raw ``"parts"`` list from an assembly JSON config.  Each dict is
        mutated in-place with the resolved ``"pos"`` value and the
        ``"depends_on"`` / ``"offset"`` keys are left intact for traceability.

    Returns
    -------
    list[dict]
        The same list, with all ``pos`` values resolved.

    Raises
    ------
    ValueError
        If a ``depends_on`` reference points to an unknown part id, or if a
        dependency cycle is detected.
    """
    # Index by id for fast lookup
    by_id: Dict[str, Dict] = {}
    for part in config_parts:
        part_id = part.get("id")
        if part_id is None:
            continue
        by_id[part_id] = part

    # Resolved cache: id → [x, y, z]
    resolved: Dict[str, List[float]] = {}

    def _resolve(part_id: str, visiting: set) -> List[float]:
        """Recursively resolve the world-space pos for *part_id*."""
        if part_id in resolved:
            return resolved[part_id]

        if part_id in visiting:
            cycle = " → ".join(sorted(visiting)) + f" → {part_id}"
            raise ValueError(
                f"[assembler] Dependency cycle detected involving '{part_id}': {cycle}"
            )

        part = by_id.get(part_id)
        if part is None:
            raise ValueError(
                f"[assembler] depends_on references unknown part id '{part_id}'"
            )

        depends_on = part.get("depends_on")
        if depends_on is None:
            # Base case: no dependency — take pos as-is
            pos = [float(v) for v in part.get("pos", [0.0, 0.0, 0.0])]
            resolved[part_id] = pos
            return pos

        # Validate parent exists before recursing
        if depends_on not in by_id:
            raise ValueError(
                f"[assembler] Part '{part_id}' depends_on unknown id '{depends_on}'"
            )

        visiting.add(part_id)
        parent_pos = _resolve(depends_on, visiting)
        visiting.discard(part_id)

        offset_raw = part.get("offset", [0.0, 0.0, 0.0])
        offset = [float(v) for v in offset_raw]

        pos = [parent_pos[i] + offset[i] for i in range(3)]
        part["pos"] = pos          # mutate in-place so AssemblyPart builder sees it
        resolved[part_id] = pos
        return pos

    for part in config_parts:
        part_id = part.get("id")
        if part_id is None:
            continue
        if part.get("depends_on") is not None:
            _resolve(part_id, set())

    return config_parts
