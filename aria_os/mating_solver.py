from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

import json

from .assembler import AssemblyPart


@dataclass
class MatingConstraint:
    type: str        # "coaxial" | "face_contact" | "bolt_pattern"
    part_a: str      # part name in assembly
    part_b: str      # part name in assembly
    params: Dict[str, Any]


class MatingSolver:
    """Adjust part positions based on simple mating constraints."""

    def __init__(self, repo_root: Optional[Path] = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = Path(repo_root)

    def _load_meta_for_part(self, part: AssemblyPart) -> Dict[str, Any]:
        p = Path(part.step_path)
        # STEP: outputs/cad/step/name.step -> meta: outputs/cad/meta/name.json
        meta_path = p.parent.parent / "meta" / (p.stem + ".json")
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def solve(
        self,
        parts: List[AssemblyPart],
        constraints: List[MatingConstraint],
        context: Dict[str, Any],
    ) -> List[AssemblyPart]:
        parts_by_name: Dict[str, AssemblyPart] = {p.name: AssemblyPart(**vars(p)) for p in parts}

        for c in constraints:
            a = parts_by_name.get(c.part_a)
            b = parts_by_name.get(c.part_b)
            if not a or not b:
                continue
            if c.type == "coaxial":
                axis = c.params.get("axis", "Z").upper()
                if axis == "Z":
                    b.position = (a.position[0], a.position[1], b.position[2])
            elif c.type == "face_contact":
                meta_a = self._load_meta_for_part(a)
                bbox_a = (meta_a.get("bbox_mm") or {})
                dz_a = float(bbox_a.get("z", 0.0))
                # Only support ">Z" on A and "<Z" on B along Z
                b.position = (b.position[0], b.position[1], a.position[2] + dz_a)
            elif c.type == "bolt_pattern":
                # Verify diameters roughly match; then align rotation about Z
                d_a = float(c.params.get("bolt_circle_a", 0.0))
                d_b = float(c.params.get("bolt_circle_b", 0.0))
                if abs(d_a - d_b) <= 1.0:
                    b.rotation = (b.rotation[0], b.rotation[1], a.rotation[2])
            elif c.type == "shaft_into_bore":
                # Insert shaft part (B) into bore of part (A). Coaxial + set Z to insertion_depth.
                b.position = (a.position[0], a.position[1],
                              a.position[2] - float(c.params.get("insertion_depth_mm", 0.0)))
            elif c.type == "gear_mesh":
                # Position B tangent to A's gear outer circle. Center distance = (OD_a + OD_b) / 2.
                od_a = float(c.params.get("od_a_mm", 0.0))
                od_b = float(c.params.get("od_b_mm", 0.0))
                axis_angle_deg = float(c.params.get("axis_angle_deg", 0.0))  # angle in XY plane
                center_dist = (od_a + od_b) / 2
                import math as _math
                theta = _math.radians(axis_angle_deg)
                b.position = (a.position[0] + center_dist * _math.cos(theta),
                              a.position[1] + center_dist * _math.sin(theta),
                              a.position[2])
            elif c.type == "dowel_locate":
                # Position B so its dowel hole centers coincide with A's.
                offset = c.params.get("dowel_offset", [0, 0, 0])
                b.position = (a.position[0] + float(offset[0]),
                              a.position[1] + float(offset[1]),
                              a.position[2] + float(offset[2]))
                # Dowel locates prevent rotation — B inherits A's rotation
                b.rotation = a.rotation
            elif c.type == "slot_engage":
                # B slides along slot of A. Initial position at slot start.
                slot_start = c.params.get("slot_start", [0, 0, 0])
                b.position = (a.position[0] + float(slot_start[0]),
                              a.position[1] + float(slot_start[1]),
                              a.position[2] + float(slot_start[2]))

        return list(parts_by_name.values())

