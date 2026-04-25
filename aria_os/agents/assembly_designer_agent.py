"""Assembly Designer agent — turns "planetary 4:1, 3 planets" into a
sized BOM with calculated tooth counts BEFORE any geometry runs.

Without this step, the LLM tends to produce 'plausible-looking'
mechanism BOMs that violate gear math (sun + 2*planet ≠ ring) or
have impossible joint topologies. By computing the BOM upstream and
giving it to the geometry planner as a seed, we get mechanisms that
both look right AND actually mesh.

Pattern:
    user prompt → AssemblyDesignerAgent.design()
                  → returns {components: [...], mates: [...], motion: [...]}
                  → planner emits asmBegin + N×addComponent + N×mate*

The agent recognizes a small library of mechanism families:
    planetary_gearbox  — sun + planets + ring + carrier
    spur_gear_pair     — driver + driven on parallel shafts
    rack_and_pinion    — gear + linear rack
    scotch_yoke        — crank + slot
    four_bar_linkage   — ground + 3 movable links
    serial_arm         — N-DOF chain (RR, RRR, RRRRRR, …)
    parallel_gripper   — body + 2 jaws on prismatic rails

Anything outside the library is delegated to the LLM with a structured
schema so the agent can still produce a usable BOM via the same data
shape (components/mates/motion).

Public API:
    from aria_os.agents.assembly_designer_agent import design_assembly
    spec = design_assembly("planetary 4:1, 3 planets, NEMA17 input")
    # spec.components → [{id, type, params}, ...]
    # spec.mates       → [{kind, parts, ...}, ...]
    # spec.motion      → [{kind, joint, ...}, ...]
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


# --- Output schema -----------------------------------------------------

@dataclass
class AssemblySpec:
    components: list[dict] = field(default_factory=list)
    mates:      list[dict] = field(default_factory=list)
    motion:     list[dict] = field(default_factory=list)
    notes:      list[str]  = field(default_factory=list)

    def to_plan(self) -> list[dict]:
        """Serialize as a native plan: asmBegin + addComponent×N + mate×N."""
        plan: list[dict] = [{"kind": "asmBegin", "params": {},
                              "label": "Begin assembly"}]
        for c in self.components:
            plan.append({"kind": "addComponent", "params": c,
                          "label": f"Add {c.get('id', '?')}"})
        for m in self.mates:
            plan.append({"kind": "mate" + m["kind"].title()
                          .replace("_", ""),
                          "params": m,
                          "label": f"Mate {m.get('parts', [])}"})
        for mo in self.motion:
            plan.append({"kind": "motion" + mo["kind"].title()
                          .replace("_", ""),
                          "params": mo,
                          "label": f"Motion {mo.get('kind')}"})
        return plan


# --- Mechanism family routers -----------------------------------------

def _design_planetary(goal: str, params: dict) -> AssemblySpec:
    """Sun + N planets + ring + carrier. Tooth-count math:
        N_ring = N_sun + 2 * N_planet
        ratio  = 1 + N_ring / N_sun   (carrier output, ring fixed)

    Inputs:
        params['ratio']    — desired reduction ratio (e.g. 4.0)
        params['n_planets'] — usually 3 or 4
        params['module']    — gear module (default 1.5mm)

    Picks N_sun + N_planet such that the math closes; returns a
    component list with calculated geometry (OD, bore, thickness)."""
    ratio = float(params.get("ratio", 4.0))
    n_planets = int(params.get("n_planets", 3))
    module = float(params.get("module", 1.5))
    width = float(params.get("width_mm", 10.0))

    # Iterate N_sun until N_planet is integer + planets fit angularly
    best = None
    for n_sun in range(8, 64):
        n_ring = round(n_sun * (ratio - 1))
        n_planet_x2 = n_ring - n_sun
        if n_planet_x2 % 2 != 0:
            continue
        n_planet = n_planet_x2 // 2
        if n_planet < 8:
            continue
        # Equal-spacing constraint: (N_sun + N_ring) / n_planets must be int
        if (n_sun + n_ring) % n_planets != 0:
            continue
        # Geometric clearance: planet OD < (carrier circumference) / n_planets
        # PCD_carrier = (sun + planet) * module / 2 * 2 = (n_sun + n_planet) * module
        pcd_carrier = (n_sun + n_planet) * module
        planet_arc = pcd_carrier * math.pi / n_planets
        planet_od = (n_planet + 2) * module
        if planet_od >= planet_arc * 0.95:
            continue
        actual_ratio = 1 + n_ring / n_sun
        if best is None or abs(actual_ratio - ratio) < best[0]:
            best = (abs(actual_ratio - ratio), n_sun, n_planet, n_ring,
                     actual_ratio)
    if best is None:
        # Fallback to a textbook 4:1
        n_sun, n_planet, n_ring = 12, 12, 36
        actual_ratio = 4.0
    else:
        _, n_sun, n_planet, n_ring, actual_ratio = best

    spec = AssemblySpec()
    spec.notes.append(
        f"Planetary {actual_ratio:.2f}:1 (target {ratio:.2f}): "
        f"sun={n_sun}T, planet={n_planet}T, ring={n_ring}T, m={module}, "
        f"n_planets={n_planets}")

    # Components
    spec.components.append({
        "id": "sun", "type": "spur_gear",
        "params": {"module": module, "n_teeth": n_sun, "thickness": width,
                    "bore_d": 5.0}})
    spec.components.append({
        "id": "ring", "type": "internal_gear",
        "params": {"module": module, "n_teeth": n_ring, "thickness": width,
                    "outer_d": (n_ring + 4) * module}})
    pcd_carrier = (n_sun + n_planet) * module
    for k in range(n_planets):
        ang = 2 * math.pi * k / n_planets
        spec.components.append({
            "id": f"planet_{k+1}", "type": "spur_gear",
            "params": {"module": module, "n_teeth": n_planet,
                        "thickness": width, "bore_d": 3.0,
                        "offset_xyz": [pcd_carrier / 2 * math.cos(ang),
                                         pcd_carrier / 2 * math.sin(ang),
                                         0]}})
    spec.components.append({
        "id": "carrier", "type": "plate_with_pins",
        "params": {"od": (n_sun + n_planet + 2) * module,
                    "thickness": width / 2,
                    "pin_count": n_planets,
                    "pin_pcd": pcd_carrier}})

    # Mates: sun concentric to ring (Z axis), each planet concentric
    # to its carrier pin. Gear mates connect tooth-meshing pairs.
    spec.mates.append({"kind": "concentric",
                        "parts": ["sun", "ring"], "axis": "Z"})
    for k in range(n_planets):
        spec.mates.append({"kind": "concentric",
                            "parts": [f"planet_{k+1}",
                                       f"carrier.pin_{k+1}"]})
        spec.mates.append({"kind": "gear",
                            "parts": ["sun", f"planet_{k+1}"],
                            "ratio": -n_planet / n_sun})
        spec.mates.append({"kind": "gear",
                            "parts": [f"planet_{k+1}", "ring"],
                            "ratio": -n_ring / n_planet})

    # Motion driver: input on sun
    spec.motion.append({"kind": "revolute", "joint": "sun.axis",
                         "speed_rpm": 1500.0,
                         "output": "carrier", "ratio": actual_ratio})
    return spec


_DOF_PATTERN = re.compile(r"(?P<n>\d+)\s*-?\s*dof", re.IGNORECASE)
_RRR_PATTERN = re.compile(r"\b([RP]{2,8})\b")


def _design_serial_arm(goal: str, params: dict) -> AssemblySpec:
    """N-DOF serial arm (alternating revolute / prismatic joints).

    Accepts:
        params['joints']  — string like "RRRRRR" or "RPRPR" (R=revolute,
                              P=prismatic). Default: 6-DOF RRRRRR.
        params['reach']   — total reach in mm (default 600)
        params['payload'] — kg (default 2.0; sizes link cross-sections)"""
    joints_str = params.get("joints", "")
    if not joints_str:
        m = _RRR_PATTERN.search(goal.upper())
        joints_str = m.group(1) if m else "RRRRRR"
    n = len(joints_str)
    reach = float(params.get("reach", 600.0))
    payload = float(params.get("payload", 2.0))
    link_len = reach / max(1, n - 1)  # last joint = end-effector

    spec = AssemblySpec()
    spec.notes.append(
        f"Serial arm {joints_str}, reach={reach:.0f}mm, "
        f"payload={payload:.1f}kg, link_len={link_len:.0f}mm")

    # Base
    spec.components.append({
        "id": "base", "type": "plate",
        "params": {"width": 200, "depth": 200, "thickness": 10}})

    # Each joint = motor housing + link + (next joint mounted on link end)
    prev = "base"
    for i, jtype in enumerate(joints_str, start=1):
        joint_id = f"j{i}_motor"
        link_id = f"link_{i}"
        spec.components.append({
            "id": joint_id, "type": "motor_housing",
            "params": {"od": 60 - i * 5, "length": 80,
                        "torque_nm": 50 / i,
                        "joint_type": "revolute" if jtype == "R" else "prismatic"}})
        spec.components.append({
            "id": link_id, "type": "tube",
            "params": {"od": 50 - i * 4, "id": 40 - i * 4,
                        "length": link_len}})
        # Mate joint motor to previous link's end
        spec.mates.append({
            "kind": "concentric",
            "parts": [f"{prev}.tip" if prev != "base" else "base.center",
                       f"{joint_id}.base"]})
        spec.mates.append({
            "kind": "concentric",
            "parts": [f"{joint_id}.tip", f"{link_id}.base"]})
        # Joint motion
        if jtype == "R":
            spec.motion.append({
                "kind": "revolute", "joint": f"{joint_id}.axis",
                "range_deg": [-180, 180],
                "speed_rpm": 60.0})
        else:
            spec.motion.append({
                "kind": "prismatic", "joint": f"{joint_id}.axis",
                "range_mm": [0, link_len],
                "speed_mm_s": 100.0})
        prev = link_id

    # End-effector flange
    spec.components.append({
        "id": "ee_flange", "type": "flange",
        "params": {"od": 50, "n_bolts": 4, "bolt_circle_r": 18,
                    "bolt_dia": 4, "thickness": 6}})
    spec.mates.append({"kind": "concentric",
                        "parts": [f"{prev}.tip", "ee_flange.center"]})
    return spec


def _design_scotch_yoke(goal: str, params: dict) -> AssemblySpec:
    """Crank + slotted yoke. Converts rotation to linear motion at
    sinusoidal velocity profile.

    Inputs:
        params['stroke']       — total linear travel in mm
        params['crank_rpm']    — input speed
    """
    stroke = float(params.get("stroke", 50.0))
    rpm = float(params.get("crank_rpm", 1500.0))
    crank_radius = stroke / 2
    spec = AssemblySpec()
    spec.notes.append(
        f"Scotch yoke: stroke={stroke}mm, crank_radius={crank_radius}mm, "
        f"rpm={rpm}")
    spec.components.append({
        "id": "frame", "type": "plate",
        "params": {"width": stroke + 80, "height": 80, "thickness": 10}})
    spec.components.append({
        "id": "crank", "type": "shaft_with_pin",
        "params": {"shaft_od": 8, "pin_radius": crank_radius,
                    "pin_dia": 6, "thickness": 8}})
    spec.components.append({
        "id": "yoke", "type": "slotted_block",
        "params": {"width": 30, "height": 40,
                    "slot_w": 7, "slot_h": 2 * crank_radius + 10,
                    "rod_len": stroke / 2 + 30}})
    spec.mates.append({"kind": "concentric",
                        "parts": ["frame.crank_bore", "crank.shaft"]})
    spec.mates.append({"kind": "slot",
                        "parts": ["yoke.slot", "crank.pin"],
                        "freedom": "Y"})
    spec.mates.append({"kind": "slider",
                        "parts": ["frame.rod_guide", "yoke.rod"],
                        "axis": "X"})
    spec.motion.append({"kind": "revolute",
                         "joint": "crank.shaft", "speed_rpm": rpm})
    return spec


def _design_parallel_gripper(goal: str, params: dict) -> AssemblySpec:
    """Two jaws on prismatic rails. Total travel = grip range.

    Inputs:
        params['travel']    — total opening range in mm
        params['mounting']  — bolt size for the M-mounting flange
    """
    travel = float(params.get("travel", 40.0))
    spec = AssemblySpec()
    spec.notes.append(f"Parallel gripper: total travel = {travel}mm")
    spec.components.append({
        "id": "body", "type": "block",
        "params": {"width": travel + 40, "height": 40, "depth": 30,
                    "rail_y": 20}})
    for side, sign in (("left", -1), ("right", +1)):
        spec.components.append({
            "id": f"jaw_{side}", "type": "block",
            "params": {"width": 12, "height": 50, "depth": 30,
                        "offset_x": sign * travel / 2}})
        spec.mates.append({"kind": "slider",
                            "parts": [f"body.rail_{side}",
                                       f"jaw_{side}.rail_face"],
                            "axis": "X"})
        spec.motion.append({"kind": "prismatic",
                             "joint": f"jaw_{side}.rail",
                             "range_mm": [0, travel / 2]})
    spec.components.append({
        "id": "mounting_flange", "type": "flange",
        "params": {"od": 50, "n_bolts": 4, "bolt_circle_r": 18,
                    "bolt_dia": 4, "thickness": 6}})
    spec.mates.append({"kind": "concentric",
                        "parts": ["body.top", "mounting_flange.center"]})
    return spec


# --- Public API --------------------------------------------------------

_FAMILY_ROUTERS: list[tuple[tuple[str, ...], Any]] = [
    (("planetary", "epicyclic"),                       _design_planetary),
    (("scotch yoke",),                                  _design_scotch_yoke),
    (("parallel gripper", "two jaw gripper",
      "two-jaw gripper"),                                _design_parallel_gripper),
    (("dof robot", "dof arm", "robot arm",
      "rrr", "rrrrrr", "serial arm"),                   _design_serial_arm),
]


def _extract_params(goal: str) -> dict:
    """Pull a few common params from the goal text by regex."""
    g = goal.lower()
    p: dict = {}
    m = re.search(r"(\d+(?:\.\d+)?)\s*:\s*1", g)
    if m:
        p["ratio"] = float(m.group(1))
    m = re.search(r"(\d+)\s*planets", g)
    if m:
        p["n_planets"] = int(m.group(1))
    m = re.search(r"module\s*(\d+(?:\.\d+)?)", g)
    if m:
        p["module"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*stroke", g)
    if m:
        p["stroke"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*travel", g)
    if m:
        p["travel"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*reach", g)
    if m:
        p["reach"] = float(m.group(1))
    m = _DOF_PATTERN.search(goal)
    if m:
        n = int(m.group("n"))
        p.setdefault("joints", "R" * n)
    return p


def design_assembly(goal: str, **overrides) -> AssemblySpec | None:
    """Match the goal to a mechanism family and return the BOM, or
    None if no family matches (caller falls back to LLM)."""
    g = goal.lower()
    params = {**_extract_params(goal), **overrides}
    for keywords, fn in _FAMILY_ROUTERS:
        if any(kw in g for kw in keywords):
            return fn(goal, params)
    return None


__all__ = ["AssemblySpec", "design_assembly"]
