"""
Rotation composition utilities — matrix-based, replaces naive additive-RPY.

Used by hierarchical_assembly.py and any other code that composes rotations
across an assembly tree. Pure-Python, no numpy dependency required (but uses
numpy when available for accuracy).
"""
from __future__ import annotations

import math
from typing import Tuple

Vec3 = Tuple[float, float, float]


def rpy_deg_to_matrix(rx_deg: float, ry_deg: float, rz_deg: float) -> list[list[float]]:
    """Roll-Pitch-Yaw (in degrees) -> 3x3 rotation matrix. ZYX convention.

    World-frame composition: R = Rz @ Ry @ Rx (yaw applied last).
    """
    rx = math.radians(rx_deg)
    ry = math.radians(ry_deg)
    rz = math.radians(rz_deg)

    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    # R = Rz @ Ry @ Rx
    return [
        [cz * cy,  cz * sy * sx - sz * cx,  cz * sy * cx + sz * sx],
        [sz * cy,  sz * sy * sx + cz * cx,  sz * sy * cx - cz * sx],
        [-sy,      cy * sx,                 cy * cx],
    ]


def matrix_to_rpy_deg(R: list[list[float]]) -> Tuple[float, float, float]:
    """3x3 rotation matrix -> RPY (degrees). Inverse of rpy_deg_to_matrix.

    Handles gimbal-lock (when |R[2][0]| ≈ 1, ry = ±90°) by setting roll=0.
    """
    sy = -R[2][0]
    if abs(sy) >= 0.9999:
        # Gimbal lock — pitch is ±90°, can't separate roll/yaw uniquely
        ry = math.copysign(math.pi / 2, sy)
        rx = 0.0
        rz = math.atan2(-R[0][1], R[1][1])
    else:
        ry = math.asin(sy)
        cy = math.cos(ry)
        rx = math.atan2(R[2][1] / cy, R[2][2] / cy)
        rz = math.atan2(R[1][0] / cy, R[0][0] / cy)
    return (math.degrees(rx), math.degrees(ry), math.degrees(rz))


def matmul_3x3(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:
    """3x3 matrix multiplication."""
    return [
        [sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def matvec_3(M: list[list[float]], v: Vec3) -> Vec3:
    """3x3 @ 3-vector."""
    return (
        M[0][0] * v[0] + M[0][1] * v[1] + M[0][2] * v[2],
        M[1][0] * v[0] + M[1][1] * v[1] + M[1][2] * v[2],
        M[2][0] * v[0] + M[2][1] * v[1] + M[2][2] * v[2],
    )


def compose_pose(
    parent_pos: Vec3, parent_rpy_deg: Vec3,
    child_pos: Vec3, child_rpy_deg: Vec3,
) -> Tuple[Vec3, Vec3]:
    """
    Compose child pose into the parent's frame.

    Returns the child's pose expressed in the world frame:
        world_pos  = parent_pos + R(parent_rpy) @ child_pos
        world_R    = R(parent_rpy) @ R(child_rpy)
    """
    R_parent = rpy_deg_to_matrix(*parent_rpy_deg)
    rotated_child_offset = matvec_3(R_parent, child_pos)
    world_pos = (
        parent_pos[0] + rotated_child_offset[0],
        parent_pos[1] + rotated_child_offset[1],
        parent_pos[2] + rotated_child_offset[2],
    )
    R_child = rpy_deg_to_matrix(*child_rpy_deg)
    R_world = matmul_3x3(R_parent, R_child)
    world_rpy = matrix_to_rpy_deg(R_world)
    return world_pos, world_rpy
