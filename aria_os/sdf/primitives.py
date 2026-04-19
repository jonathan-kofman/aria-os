"""
Pro-grade SDF primitives + transforms — additions beyond the base set
in aria_os/generators/sdf_generator.py.

Convention (same as base module):
  Each primitive returns a function f(x,y,z) -> signed distance where
    < 0 inside, > 0 outside, = 0 on surface.
  x, y, z are numpy arrays (grid-sampled via SDFScene.evaluate) or scalars.
"""
from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# Additional primitives
# ---------------------------------------------------------------------------

def sdf_ellipsoid(center: tuple = (0, 0, 0),
                  radii: tuple = (1.0, 1.0, 1.0)):
    """Anisotropic sphere. Note: the SDF returned is an approximation
    (normalized to semi-axis lengths) — for tight field usage prefer the
    exact ellipsoid SDF in graphics literature; for meshing this is fine."""
    cx, cy, cz = center
    rx, ry, rz = radii
    def f(x, y, z):
        k0 = np.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
                     + ((z - cz) / rz) ** 2)
        k1 = np.sqrt(((x - cx) / (rx * rx)) ** 2
                     + ((y - cy) / (ry * ry)) ** 2
                     + ((z - cz) / (rz * rz)) ** 2)
        # k1 == 0 at the center — clamp to avoid divide-by-zero
        return k0 * (k0 - 1.0) / np.maximum(k1, 1e-9)
    return f


def sdf_rounded_box(center: tuple = (0, 0, 0),
                    size: tuple = (1, 1, 1), radius: float = 0.1):
    """Box with rounded edges. radius must be <= min(size)/2."""
    cx, cy, cz = center
    sx = size[0] / 2 - radius
    sy = size[1] / 2 - radius
    sz = size[2] / 2 - radius
    def f(x, y, z):
        dx = np.abs(x - cx) - sx
        dy = np.abs(y - cy) - sy
        dz = np.abs(z - cz) - sz
        outside = np.sqrt(
            np.maximum(dx, 0) ** 2 + np.maximum(dy, 0) ** 2
            + np.maximum(dz, 0) ** 2)
        inside = np.minimum(np.maximum(dx, np.maximum(dy, dz)), 0)
        return outside + inside - radius
    return f


def sdf_chamfered_box(center: tuple = (0, 0, 0),
                      size: tuple = (1, 1, 1), chamfer: float = 0.1):
    """Box with chamfered edges (45-degree flat bevels). Use over
    rounded_box when you want CNC-friendly geometry."""
    cx, cy, cz = center
    sx, sy, sz = size[0] / 2, size[1] / 2, size[2] / 2
    def f(x, y, z):
        dx = np.abs(x - cx) - sx
        dy = np.abs(y - cy) - sy
        dz = np.abs(z - cz) - sz
        outside = np.sqrt(
            np.maximum(dx, 0) ** 2 + np.maximum(dy, 0) ** 2
            + np.maximum(dz, 0) ** 2)
        inside = np.minimum(np.maximum(dx, np.maximum(dy, dz)), 0)
        box_d = outside + inside
        # Chamfer planes: max(|dx|+|dy|, |dy|+|dz|, |dx|+|dz|) - diag
        bevel = np.maximum(np.maximum(np.abs(dx) + np.abs(dy),
                                      np.abs(dy) + np.abs(dz)),
                           np.abs(dx) + np.abs(dz)) - chamfer
        return np.maximum(box_d, bevel)
    return f


def sdf_hexagonal_prism(center: tuple = (0, 0, 0),
                        apothem: float = 1.0, height: float = 1.0,
                        axis: str = "z"):
    """Regular hexagonal prism. apothem = inradius of hex (face-to-face / 2)."""
    cx, cy, cz = center
    k0, k1, k2 = -np.sqrt(3) / 2, 0.5, np.sqrt(3) / 3
    h2 = height / 2
    def f(x, y, z):
        if axis == "z":
            px, py, pz = x - cx, y - cy, z - cz
            ax_abs, pz_abs = np.abs(px), pz
        elif axis == "y":
            px, py, pz = x - cx, z - cz, y - cy
            ax_abs, pz_abs = np.abs(px), pz
        else:
            px, py, pz = y - cy, z - cz, x - cx
            ax_abs, pz_abs = np.abs(px), pz
        py_abs = np.abs(py)
        # Project onto hex
        dot = 2 * np.minimum(k0 * ax_abs + k1 * py_abs, 0.0)
        nx = ax_abs - dot * k0
        ny = py_abs - dot * k1
        # Clamp to segment
        clamp_x = np.clip(nx, -k2 * apothem, k2 * apothem)
        d2 = np.hypot(nx - clamp_x, ny - apothem)
        radial = d2 * np.sign(ny - apothem)
        h_d = np.abs(pz_abs) - h2
        return np.maximum(radial, h_d)
    return f


def sdf_triangular_prism(center: tuple = (0, 0, 0),
                         width: float = 1.0, height: float = 1.0,
                         depth: float = 1.0):
    """Isoceles triangular prism extruded along Z. Triangle apex at +Y."""
    cx, cy, cz = center
    w2, h2, d2 = width / 2, height / 2, depth / 2
    def f(x, y, z):
        px = np.abs(x - cx) - w2
        py = y - cy - h2
        # Two-edge tri (apex up)
        # slope k = w2/h (half-width / half-height)
        k = w2 / max(h2, 1e-9)
        q = np.maximum(px + k * py, py)
        radial = np.where(q < 0, -np.maximum(px, py), np.hypot(px, py))
        d_h = np.abs(z - cz) - d2
        return np.maximum(radial, d_h)
    return f


def sdf_pyramid(base_center: tuple = (0, 0, 0),
                base_size: float = 1.0, height: float = 1.0):
    """Square-base pyramid with apex at +Z. base_center is the BOTTOM."""
    cx, cy, cz = base_center
    h = height
    m2 = h ** 2 + 0.25
    def f(x, y, z):
        px = np.abs(x - cx)
        py = z - cz
        pz = np.abs(y - cy)
        px, pz = np.where(pz > px, pz, px), np.where(pz > px, px, pz)
        px -= base_size / 2
        pz -= base_size / 2
        q_x = pz
        q_y = h * py - 0.5 * px
        q_z = h * px + 0.5 * py
        s = np.maximum(-q_x, 0.0)
        t = np.clip((q_y - 0.5 * pz) / (m2 + 0.25), 0.0, 1.0)
        a = m2 * (q_x + s) ** 2 + q_y ** 2
        b = m2 * (q_x + 0.5 * t) ** 2 + (q_y - m2 * t) ** 2
        d2 = np.where(
            np.minimum(q_y, -q_x * m2 - q_y * 0.5) > 0.0,
            0.0, np.minimum(a, b))
        return np.sqrt((d2 + q_z ** 2) / m2) * np.sign(
            np.maximum(q_z, -py))
    return f


def sdf_plane(origin: tuple = (0, 0, 0), normal: tuple = (0, 0, 1)):
    """Infinite plane. Signed distance is positive on the normal side."""
    ox, oy, oz = origin
    nx, ny, nz = normal
    mag = np.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / mag, ny / mag, nz / mag
    def f(x, y, z):
        return (x - ox) * nx + (y - oy) * ny + (z - oz) * nz
    return f


def sdf_half_space(origin: tuple = (0, 0, 0),
                   normal: tuple = (0, 0, 1)):
    """Half-space — identical to plane but intent-named for booleans
    (intersect sphere with half-space = hemisphere)."""
    return sdf_plane(origin, normal)


def sdf_line_segment(a: tuple = (0, 0, 0), b: tuple = (1, 0, 0),
                     radius: float = 0.1):
    """Round-ended line (same as capsule, kept as intent-named alias)."""
    ax, ay, az = a
    bx, by, bz = b
    def f(x, y, z):
        pa_x, pa_y, pa_z = x - ax, y - ay, z - az
        ba_x, ba_y, ba_z = bx - ax, by - ay, bz - az
        ba_dot = ba_x ** 2 + ba_y ** 2 + ba_z ** 2
        t = np.clip(
            (pa_x * ba_x + pa_y * ba_y + pa_z * ba_z) / (ba_dot + 1e-12),
            0, 1)
        dx = pa_x - t * ba_x
        dy = pa_y - t * ba_y
        dz = pa_z - t * ba_z
        return np.sqrt(dx ** 2 + dy ** 2 + dz ** 2) - radius
    return f


def sdf_extrude_2d(profile_2d, height: float, axis: str = "z"):
    """Turn a 2D SDF (f(x,y) -> d) into a 3D extrusion along axis.

    profile_2d: callable (x_array, y_array) -> distance_array
    height: total extrusion thickness, centred at origin on the axis.
    """
    h2 = height / 2
    def f(x, y, z):
        if axis == "z":
            d2d = profile_2d(x, y)
            d_h = np.abs(z) - h2
        elif axis == "y":
            d2d = profile_2d(x, z)
            d_h = np.abs(y) - h2
        else:  # x
            d2d = profile_2d(y, z)
            d_h = np.abs(x) - h2
        # Clamp to avoid outside-outside errors
        outside = np.sqrt(np.maximum(d2d, 0) ** 2 + np.maximum(d_h, 0) ** 2)
        inside = np.minimum(np.maximum(d2d, d_h), 0)
        return outside + inside
    return f


def sdf_revolve_profile(profile_2d,
                        center: tuple = (0, 0, 0),
                        axis: str = "z"):
    """Revolve a 2D SDF around an axis. profile_2d takes (radial, axial)
    coords. axis: the axis of rotation; the other two become radial."""
    cx, cy, cz = center
    def f(x, y, z):
        if axis == "z":
            r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
            return profile_2d(r, z - cz)
        elif axis == "y":
            r = np.sqrt((x - cx) ** 2 + (z - cz) ** 2)
            return profile_2d(r, y - cy)
        else:
            r = np.sqrt((y - cy) ** 2 + (z - cz) ** 2)
            return profile_2d(r, x - cx)
    return f


# ---------------------------------------------------------------------------
# Additional transforms (full 3-axis rotation + mirror + taper + symmetry)
# ---------------------------------------------------------------------------

def op_rotate_x(a, angle_deg: float):
    angle = np.radians(angle_deg)
    c, s = np.cos(angle), np.sin(angle)
    def f(x, y, z):
        return a(x, y * c + z * s, -y * s + z * c)
    return f


def op_rotate_y(a, angle_deg: float):
    angle = np.radians(angle_deg)
    c, s = np.cos(angle), np.sin(angle)
    def f(x, y, z):
        return a(x * c - z * s, y, x * s + z * c)
    return f


def op_rotate_axis_angle(a, axis: tuple, angle_deg: float):
    """Rodrigues rotation — arbitrary axis. axis must be unit-length or
    will be normalized."""
    ax_x, ax_y, ax_z = axis
    mag = np.sqrt(ax_x * ax_x + ax_y * ax_y + ax_z * ax_z)
    ax_x, ax_y, ax_z = ax_x / mag, ax_y / mag, ax_z / mag
    angle = np.radians(angle_deg)
    c, s = np.cos(angle), np.sin(angle)
    C = 1.0 - c
    # Rotation matrix coefficients
    m00 = c + ax_x * ax_x * C
    m01 = ax_x * ax_y * C - ax_z * s
    m02 = ax_x * ax_z * C + ax_y * s
    m10 = ax_y * ax_x * C + ax_z * s
    m11 = c + ax_y * ax_y * C
    m12 = ax_y * ax_z * C - ax_x * s
    m20 = ax_z * ax_x * C - ax_y * s
    m21 = ax_z * ax_y * C + ax_x * s
    m22 = c + ax_z * ax_z * C

    def f(x, y, z):
        # Inverse rotation for SDF querying
        nx = m00 * x + m10 * y + m20 * z
        ny = m01 * x + m11 * y + m21 * z
        nz = m02 * x + m12 * y + m22 * z
        return a(nx, ny, nz)
    return f


def op_rotate_euler(a, rx_deg: float = 0.0, ry_deg: float = 0.0,
                    rz_deg: float = 0.0):
    """ZYX intrinsic Euler rotation."""
    rot = a
    if rx_deg:
        rot = op_rotate_x(rot, rx_deg)
    if ry_deg:
        rot = op_rotate_y(rot, ry_deg)
    if rz_deg:
        from aria_os.generators.sdf_generator import op_rotate_z
        rot = op_rotate_z(rot, rz_deg)
    return rot


def op_mirror(a, plane: str = "x", offset: float = 0.0):
    """Mirror around a plane. plane in {'x','y','z','xy','yz','xz'}."""
    def f(x, y, z):
        if plane == "x":
            return a(-np.abs(x - offset) + offset, y, z)
        if plane == "y":
            return a(x, -np.abs(y - offset) + offset, z)
        if plane == "z":
            return a(x, y, -np.abs(z - offset) + offset)
        if plane == "xy":
            return a(-np.abs(x), -np.abs(y), z)
        if plane == "yz":
            return a(x, -np.abs(y), -np.abs(z))
        if plane == "xz":
            return a(-np.abs(x), y, -np.abs(z))
        return a(x, y, z)
    return f


def op_taper(a, axis: str = "z", k: float = 0.1):
    """Linear taper along an axis. k > 0 shrinks positive side; k < 0 flares.
    Useful for conical extrusions or draft angles."""
    def f(x, y, z):
        if axis == "z":
            scale = np.maximum(1e-6, 1.0 + k * z)
            return a(x / scale, y / scale, z) * scale
        if axis == "y":
            scale = np.maximum(1e-6, 1.0 + k * y)
            return a(x / scale, y, z / scale) * scale
        scale = np.maximum(1e-6, 1.0 + k * x)
        return a(x, y / scale, z / scale) * scale
    return f


def op_axial_symmetry(a, n: int = 6, axis: str = "z"):
    """Rotate-and-union copies around an axis (flower / star / fan).
    n = number of copies. Uses a "fold" trick: project every query point
    into the first sector and evaluate once. Fast and exact.
    """
    angle_step = 2 * np.pi / n
    half = angle_step / 2

    def f(x, y, z):
        if axis == "z":
            r = np.sqrt(x * x + y * y)
            theta = np.arctan2(y, x)
            folded = ((theta + half) % angle_step) - half
            fx = r * np.cos(folded)
            fy = r * np.sin(folded)
            return a(fx, fy, z)
        if axis == "y":
            r = np.sqrt(x * x + z * z)
            theta = np.arctan2(z, x)
            folded = ((theta + half) % angle_step) - half
            fx = r * np.cos(folded)
            fz = r * np.sin(folded)
            return a(fx, y, fz)
        r = np.sqrt(y * y + z * z)
        theta = np.arctan2(z, y)
        folded = ((theta + half) % angle_step) - half
        fy = r * np.cos(folded)
        fz = r * np.sin(folded)
        return a(x, fy, fz)
    return f
