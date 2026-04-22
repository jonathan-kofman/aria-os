"""Assembly planner — streams multi-component + mate ops into the
active Fusion design. Every Design IS an assembly (root + occurrences),
so no document switching is needed.

MVP: placeholder components + rigid joints between them. Full impl
would let the caller reference parts from the library or emit
per-component sketch/extrude ops to build each occurrence in-place.
"""
from __future__ import annotations


def plan_simple_assembly(spec: dict) -> list[dict]:
    """A two-component assembly with a rigid joint — smoke-test of the
    bridge plumbing. Equivalent to `plan_flange` on the mechanical side.
    """
    plan: list[dict] = [
        {"kind": "asmBegin", "params": {},
         "label": "Reset assembly registry"},
        {"kind": "addComponent",
         "params": {"name": "Base", "alias": "base",
                    "x_mm": 0, "y_mm": 0, "z_mm": 0},
         "label": "Add component: Base"},
        {"kind": "addComponent",
         "params": {"name": "Housing", "alias": "housing",
                    "x_mm": 60, "y_mm": 0, "z_mm": 0},
         "label": "Add component: Housing (+60mm X)"},
        {"kind": "joint",
         "params": {"component1": "base", "component2": "housing",
                    "joint_type": "rigid", "alias": "base_housing_weld"},
         "label": "Rigid joint: Base ↔ Housing"},
    ]
    return plan
