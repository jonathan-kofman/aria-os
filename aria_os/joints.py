"""
Kinematic joint definitions for robotic and mechanism assemblies.

A joint specifies how two parts (parent and child) are constrained in
motion: revolute (rotates about an axis), prismatic (slides along an axis),
cylindrical (both), spherical (ball joint), fixed (rigid).

Joints are the layer above mating constraints. Mating aligns parts
geometrically; joints say which degrees of freedom remain free.

JSON format embedded in assembly config:

    "joints": [
      {"id": "j1", "type": "revolute", "parent": "base", "child": "shoulder",
       "axis": [0, 0, 1], "origin": [0, 0, 50],
       "range_deg": [-180, 180], "actuator": "NEMA23-56mm-8mm"}
    ]

Supports export to URDF (ROS) for downstream motion planning, reach analysis,
and dynamics simulation via Pinocchio / MoveIt.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_JOINT_TYPES = {
    "fixed": "fixed",
    "revolute": "revolute",
    "continuous": "continuous",   # revolute with no limits
    "prismatic": "prismatic",
    "cylindrical": "cylindrical", # URDF doesn't have this natively; model as revolute + prismatic
    "spherical": "floating",      # URDF floating is 6-DOF; spherical is 3 rotational
    "planar": "planar",
}


@dataclass
class Joint:
    """A kinematic joint between two parts."""
    id: str
    type: str                       # see _JOINT_TYPES
    parent: str                     # parent part id (typically upstream in chain)
    child: str                      # child part id

    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    origin_rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)  # roll, pitch, yaw (rad)

    range_deg: tuple[float, float] | None = None   # for revolute/prismatic: (lower, upper)
    max_effort_nm: float | None = None             # torque limit (for dynamics)
    max_velocity_rad_s: float | None = None        # speed limit
    actuator: str | None = None                    # catalog designation of driving motor

    def validate(self) -> None:
        if self.type not in _JOINT_TYPES:
            raise ValueError(
                f"Joint '{self.id}' has unknown type '{self.type}'. "
                f"Valid: {list(_JOINT_TYPES)}"
            )
        if not self.parent:
            raise ValueError(f"Joint '{self.id}' missing parent")
        if not self.child:
            raise ValueError(f"Joint '{self.id}' missing child")
        if self.parent == self.child:
            raise ValueError(f"Joint '{self.id}' parent == child ('{self.parent}')")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type,
            "parent": self.parent, "child": self.child,
            "axis": list(self.axis), "origin": list(self.origin),
            "origin_rpy": list(self.origin_rpy),
            "range_deg": list(self.range_deg) if self.range_deg else None,
            "max_effort_nm": self.max_effort_nm,
            "max_velocity_rad_s": self.max_velocity_rad_s,
            "actuator": self.actuator,
        }


@dataclass
class KinematicChain:
    """A full robot / mechanism — links + joints."""
    name: str
    link_ids: list[str] = field(default_factory=list)
    joints: list[Joint] = field(default_factory=list)

    def add_joint(self, joint: Joint) -> None:
        joint.validate()
        self.joints.append(joint)

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors: list[str] = []
        # Check no joint references unknown links
        known = set(self.link_ids)
        for j in self.joints:
            if j.parent not in known and j.parent != "world":
                errors.append(f"Joint '{j.id}' parent '{j.parent}' not in links")
            if j.child not in known:
                errors.append(f"Joint '{j.id}' child '{j.child}' not in links")
        # Check each link (except root) has at most one parent joint
        parent_counts: dict[str, int] = {}
        for j in self.joints:
            parent_counts[j.child] = parent_counts.get(j.child, 0) + 1
        for link, n in parent_counts.items():
            if n > 1:
                errors.append(f"Link '{link}' has {n} parent joints (max 1 in a tree)")
        # Check there's a single root
        parented_links = set(j.child for j in self.joints)
        roots = [l for l in self.link_ids if l not in parented_links]
        if len(roots) > 1:
            errors.append(f"Multiple root links (no parent joint): {roots}")
        return errors


# ---------------------------------------------------------------------------
# URDF export
# ---------------------------------------------------------------------------

def joints_from_config(config: dict[str, Any]) -> list[Joint]:
    """Parse the 'joints' list from an assembly config into Joint objects."""
    raw = config.get("joints", [])
    joints: list[Joint] = []
    for j in raw:
        joint = Joint(
            id=j["id"],
            type=j["type"],
            parent=j["parent"],
            child=j["child"],
            axis=tuple(j.get("axis", [0, 0, 1])),
            origin=tuple(j.get("origin", [0, 0, 0])),
            origin_rpy=tuple(j.get("origin_rpy", [0, 0, 0])),
            range_deg=tuple(j["range_deg"]) if j.get("range_deg") else None,
            max_effort_nm=j.get("max_effort_nm"),
            max_velocity_rad_s=j.get("max_velocity_rad_s"),
            actuator=j.get("actuator"),
        )
        joint.validate()
        joints.append(joint)
    return joints


def export_urdf(
    chain: KinematicChain,
    *,
    link_stl_paths: dict[str, str] | None = None,
    output_path: str | Path,
) -> str:
    """
    Export a KinematicChain to URDF for ROS / MoveIt / Pinocchio.

    link_stl_paths: optional dict mapping link_id -> STL file path for visual
    meshes. If omitted, links are rendered as unit-box placeholders.
    """
    link_stl_paths = link_stl_paths or {}
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    robot = ET.Element("robot", {"name": chain.name})

    # Links
    for link_id in chain.link_ids:
        link = ET.SubElement(robot, "link", {"name": link_id})
        visual = ET.SubElement(link, "visual")
        ET.SubElement(visual, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
        geom = ET.SubElement(visual, "geometry")
        stl = link_stl_paths.get(link_id)
        if stl:
            ET.SubElement(geom, "mesh", {"filename": stl})
        else:
            ET.SubElement(geom, "box", {"size": "0.05 0.05 0.05"})

    # Joints
    for j in chain.joints:
        # Convert URDF-unsupported types
        urdf_type = _JOINT_TYPES.get(j.type, "fixed")
        joint_el = ET.SubElement(robot, "joint",
                                  {"name": j.id, "type": urdf_type})
        ET.SubElement(joint_el, "parent", {"link": j.parent})
        ET.SubElement(joint_el, "child", {"link": j.child})
        ET.SubElement(joint_el, "origin", {
            "xyz": f"{j.origin[0]/1000} {j.origin[1]/1000} {j.origin[2]/1000}",  # mm -> m
            "rpy": f"{j.origin_rpy[0]} {j.origin_rpy[1]} {j.origin_rpy[2]}",
        })
        ET.SubElement(joint_el, "axis",
                      {"xyz": f"{j.axis[0]} {j.axis[1]} {j.axis[2]}"})
        # URDF spec: <limit> is REQUIRED for revolute and prismatic joints,
        # and effort + velocity are REQUIRED attributes when <limit> is present.
        # For revolute joints without explicit range, fall back to ±π. For
        # prismatic, fall back to ±1m. ROS parsers reject joints missing these.
        if urdf_type in ("revolute", "prismatic"):
            if j.range_deg:
                if urdf_type == "revolute":
                    lo = math.radians(j.range_deg[0])
                    hi = math.radians(j.range_deg[1])
                else:  # prismatic — range_deg is treated as mm
                    lo = j.range_deg[0] / 1000
                    hi = j.range_deg[1] / 1000
            else:
                if urdf_type == "revolute":
                    lo, hi = -math.pi, math.pi
                else:
                    lo, hi = -1.0, 1.0
            # effort and velocity are MANDATORY in URDF — provide sane defaults
            effort = j.max_effort_nm if j.max_effort_nm is not None else 10.0
            velocity = j.max_velocity_rad_s if j.max_velocity_rad_s is not None else 1.0
            ET.SubElement(joint_el, "limit", {
                "lower": f"{lo}",
                "upper": f"{hi}",
                "effort": f"{effort}",
                "velocity": f"{velocity}",
            })

    tree = ET.ElementTree(robot)
    ET.indent(tree, space="  ")
    tree.write(str(out), encoding="utf-8", xml_declaration=True)
    return str(out)
