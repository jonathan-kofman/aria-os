"""
Pro-grade SDF lattice library — the full TPMS family plus strut lattices
and stochastic beam networks. This is the differentiator nTop / Hyperganic
/ General Lattice all lean on: implicit lattices handle arbitrary cell
counts that B-rep booleans can't touch.

All lattices return f(x, y, z) -> signed distance.
  cell_size: period of the unit cell (mm)
  thickness: wall thickness for TPMS, beam radius for struts (mm)

TPMS family (all with period-agnostic scaling):
  Gyroid           (in base sdf_generator)
  Schwarz-P        (in base sdf_generator)
  Schwarz-D / Diamond (in base sdf_generator)
  Schwarz-W        (alternating faces)           HERE
  IWP              (I-WrappedPackage — highest strength TPMS) HERE
  Neovius          (high symmetry)               HERE
  F-RD             (face-centred-regular-diamond) HERE

Strut lattices (beam networks):
  cubic            (in base — beams along 3 axes)
  BCC              (body-centred cubic — center node + corners)  HERE
  FCC              (face-centred cubic)                          HERE
  Octet-truss      (best stiffness-to-weight; aerospace std)     HERE
  Kagome 2D        (triangular tiling — optimal energy)          HERE
  Honeycomb-2D     (hex tiling, extruded)                        HERE

Stochastic:
  stochastic_beams  (random beam network — mimics open-cell foams) HERE
"""
from __future__ import annotations

import numpy as np

from aria_os.generators.sdf_generator import sdf_capsule, op_union


# ---------------------------------------------------------------------------
# Additional TPMS
# ---------------------------------------------------------------------------

def sdf_schwarz_w(cell_size: float = 10.0, thickness: float = 1.0):
    """Schwarz-W TPMS — alternating-faces variant of Schwarz-P."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = (np.sin(k * x) * np.sin(k * y) * np.sin(k * z)
               + np.cos(k * x) * np.cos(k * y) * np.cos(k * z))
        return np.abs(val) - t2
    return f


def sdf_iwp(cell_size: float = 10.0, thickness: float = 1.0):
    """IWP TPMS (I-Wrapped Package) — mechanically strongest common TPMS
    at matched density. Widely used in aerospace AM."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = (2 * (np.cos(k * x) * np.cos(k * y)
                    + np.cos(k * y) * np.cos(k * z)
                    + np.cos(k * z) * np.cos(k * x))
               - (np.cos(2 * k * x) + np.cos(2 * k * y) + np.cos(2 * k * z)))
        return np.abs(val) - t2
    return f


def sdf_neovius(cell_size: float = 10.0, thickness: float = 1.0):
    """Neovius TPMS — very high surface-to-volume ratio, used for heat
    exchangers and biomedical scaffolds."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = (3 * (np.cos(k * x) + np.cos(k * y) + np.cos(k * z))
               + 4 * np.cos(k * x) * np.cos(k * y) * np.cos(k * z))
        return np.abs(val) - t2
    return f


def sdf_frd(cell_size: float = 10.0, thickness: float = 1.0):
    """F-RD TPMS (Face-Regular-Diamond). Used for acoustic damping."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = (4 * np.cos(k * x) * np.cos(k * y) * np.cos(k * z)
               - (np.cos(2 * k * x) * np.cos(2 * k * y)
                  + np.cos(2 * k * y) * np.cos(2 * k * z)
                  + np.cos(2 * k * x) * np.cos(2 * k * z)))
        return np.abs(val) - t2
    return f


# ---------------------------------------------------------------------------
# Strut lattices
# ---------------------------------------------------------------------------

def _periodic_cell(x, y, z, cell):
    """Return local (mx, my, mz) coords folded into a single cell
    centred on origin, plus the cell indices (ix, iy, iz)."""
    half = cell / 2
    ix = np.floor(x / cell + 0.5)
    iy = np.floor(y / cell + 0.5)
    iz = np.floor(z / cell + 0.5)
    mx = x - ix * cell
    my = y - iy * cell
    mz = z - iz * cell
    return mx, my, mz, ix, iy, iz


def sdf_bcc_lattice(cell_size: float = 10.0, beam_radius: float = 1.0):
    """Body-Centred Cubic lattice.
    Struts from each corner of the cube to the center of the cube.
    Good isotropic properties, medium stiffness."""
    c2 = cell_size / 2
    def f(x, y, z):
        mx, my, mz, _, _, _ = _periodic_cell(x, y, z, cell_size)
        # Distance to centerline of each of the 4 body-diagonals (through cell center)
        # Corners at (±c2, ±c2, ±c2); center at (0, 0, 0).
        # 4 struts: (+,+,+)->(-,-,-), (+,+,-)->(-,-,+), (+,-,+)->(-,+,-), (+,-,-)->(-,+,+)
        r = np.inf
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    # line from (sx*c2, sy*c2, sz*c2) -> (0,0,0)
                    # direction (-sx, -sy, -sz); parametric: p(t) = corner + t*dir, t in [0,1]
                    # distance from point P to this line segment
                    ax, ay, az = sx * c2, sy * c2, sz * c2
                    bx, by, bz = 0.0, 0.0, 0.0
                    pa_x, pa_y, pa_z = mx - ax, my - ay, mz - az
                    ba_x, ba_y, ba_z = bx - ax, by - ay, bz - az
                    ba_dot = ba_x ** 2 + ba_y ** 2 + ba_z ** 2
                    t = np.clip(
                        (pa_x * ba_x + pa_y * ba_y + pa_z * ba_z) / ba_dot,
                        0, 1)
                    dx = pa_x - t * ba_x
                    dy = pa_y - t * ba_y
                    dz = pa_z - t * ba_z
                    d = np.sqrt(dx * dx + dy * dy + dz * dz)
                    r = np.minimum(r, d)
        return r - beam_radius
    return f


def sdf_fcc_lattice(cell_size: float = 10.0, beam_radius: float = 1.0):
    """Face-Centred Cubic lattice.
    Struts between nearest face-centred nodes — denser than BCC, higher
    stiffness per density."""
    c2 = cell_size / 2
    # Face-centred nodes (one per face, 6 per cell; periodic neighbours
    # mean every face node connects to 4 neighbours on adjacent faces)
    def f(x, y, z):
        mx, my, mz, _, _, _ = _periodic_cell(x, y, z, cell_size)
        nodes = [
            (c2, 0, 0), (-c2, 0, 0),
            (0, c2, 0), (0, -c2, 0),
            (0, 0, c2), (0, 0, -c2),
        ]
        r = np.inf
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                # Only connect adjacent (orthogonal) face centres — not
                # opposite ones (that would be a tube through the cell).
                ax, ay, az = nodes[i]
                bx, by, bz = nodes[j]
                if ax == -bx and ay == -by and az == -bz:
                    continue
                pa_x, pa_y, pa_z = mx - ax, my - ay, mz - az
                ba_x, ba_y, ba_z = bx - ax, by - ay, bz - az
                ba_dot = ba_x ** 2 + ba_y ** 2 + ba_z ** 2
                t = np.clip(
                    (pa_x * ba_x + pa_y * ba_y + pa_z * ba_z) / ba_dot,
                    0, 1)
                dx = pa_x - t * ba_x
                dy = pa_y - t * ba_y
                dz = pa_z - t * ba_z
                d = np.sqrt(dx * dx + dy * dy + dz * dz)
                r = np.minimum(r, d)
        return r - beam_radius
    return f


def sdf_octet_truss(cell_size: float = 10.0, beam_radius: float = 1.0):
    """Octet-truss — combines FCC face-diagonal struts with an internal
    octahedron. Highest known isotropic stiffness-to-weight ratio;
    used in aerospace (e.g. SpaceX grid fins, HRL metallic microlattice).
    """
    c2 = cell_size / 2
    # Nodes: 8 cell corners + 6 face centres
    corners = [
        (sx * c2, sy * c2, sz * c2)
        for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    faces = [
        (c2, 0, 0), (-c2, 0, 0),
        (0, c2, 0), (0, -c2, 0),
        (0, 0, c2), (0, 0, -c2)]
    # Struts: every corner connects to 3 nearest face centres
    struts = []
    for cx, cy, cz in corners:
        for fx, fy, fz in faces:
            # connect when corner and face share two matching zero axes
            same = sum(1 for ax, bx in ((cx, fx), (cy, fy), (cz, fz))
                       if np.sign(ax) == np.sign(bx) or bx == 0)
            if same >= 2:
                struts.append(((cx, cy, cz), (fx, fy, fz)))
    # Plus octahedron edges between face centres
    for i in range(len(faces)):
        for j in range(i + 1, len(faces)):
            fa, fb = faces[i], faces[j]
            if not (fa[0] == -fb[0] and fa[1] == -fb[1] and fa[2] == -fb[2]):
                struts.append((fa, fb))

    def f(x, y, z):
        mx, my, mz, _, _, _ = _periodic_cell(x, y, z, cell_size)
        r = np.inf
        for (ax, ay, az), (bx, by, bz) in struts:
            pa_x, pa_y, pa_z = mx - ax, my - ay, mz - az
            ba_x, ba_y, ba_z = bx - ax, by - ay, bz - az
            ba_dot = ba_x ** 2 + ba_y ** 2 + ba_z ** 2
            if ba_dot < 1e-12:
                continue
            t = np.clip(
                (pa_x * ba_x + pa_y * ba_y + pa_z * ba_z) / ba_dot, 0, 1)
            dx = pa_x - t * ba_x
            dy = pa_y - t * ba_y
            dz = pa_z - t * ba_z
            d = np.sqrt(dx * dx + dy * dy + dz * dz)
            r = np.minimum(r, d)
        return r - beam_radius
    return f


def sdf_kagome_lattice(cell_size: float = 10.0, beam_radius: float = 0.5,
                       thickness: float = 5.0):
    """2D Kagome tiling extruded along Z. Triangular net of struts —
    known for optimal in-plane stiffness."""
    a = cell_size
    h = a * np.sqrt(3) / 2
    thk_h = thickness / 2

    def f(x, y, z):
        # Fold into Kagome unit cell
        y_mod = np.mod(y + h * 0.5, h)
        x_mod = np.mod(x + a * 0.25, a * 0.5)
        # 6 lines per unit cell — approximate via 3 axis-aligned beams
        # (horizontal at y_mod=h/2, and two diagonals at ±60°).
        d_horiz = np.abs(y_mod - h * 0.5) - beam_radius
        # Diagonal beams: rotate query by ±60°
        cos60, sin60 = 0.5, np.sqrt(3) / 2
        u1 = x_mod * cos60 + y_mod * sin60
        v1 = -x_mod * sin60 + y_mod * cos60
        d_diag1 = np.abs(np.mod(v1, h) - h * 0.5) - beam_radius
        u2 = x_mod * cos60 - y_mod * sin60
        v2 = x_mod * sin60 + y_mod * cos60
        d_diag2 = np.abs(np.mod(v2, h) - h * 0.5) - beam_radius
        d_2d = np.minimum(d_horiz, np.minimum(d_diag1, d_diag2))
        d_z = np.abs(z) - thk_h
        outside = np.sqrt(np.maximum(d_2d, 0) ** 2 + np.maximum(d_z, 0) ** 2)
        inside = np.minimum(np.maximum(d_2d, d_z), 0)
        return outside + inside
    return f


def sdf_honeycomb_2d(cell_size: float = 10.0, wall_thickness: float = 1.0,
                     thickness: float = 5.0):
    """Regular hexagonal honeycomb, extruded along Z. The classic
    aerospace lightweighting structure."""
    a = cell_size
    h = a * np.sqrt(3) / 2
    thk_h = thickness / 2

    def f(x, y, z):
        # Hex tiling with offset every row
        row = np.floor(y / h)
        col_offset = (np.mod(row, 2) * 0.5) * a
        x_mod = np.mod(x + col_offset + a * 0.5, a) - a * 0.5
        y_mod = np.mod(y + h * 0.5, h) - h * 0.5
        # Distance to the walls: hex consists of 6 planes offset by apothem
        apothem = a / 2.0
        d_hex = np.maximum(
            np.abs(x_mod) - apothem + wall_thickness,
            np.maximum(
                np.abs(y_mod * np.cos(np.pi / 6) + x_mod * np.sin(np.pi / 6))
                - apothem + wall_thickness,
                np.abs(y_mod * np.cos(np.pi / 6) - x_mod * np.sin(np.pi / 6))
                - apothem + wall_thickness))
        d_z = np.abs(z) - thk_h
        outside = np.sqrt(np.maximum(d_hex, 0) ** 2 + np.maximum(d_z, 0) ** 2)
        inside = np.minimum(np.maximum(d_hex, d_z), 0)
        return outside + inside
    return f


# ---------------------------------------------------------------------------
# Stochastic — beam network from a random seed (open-cell foam imitation)
# ---------------------------------------------------------------------------

def sdf_stochastic_beams(bounds: tuple = ((-10, -10, -10), (10, 10, 10)),
                         n_beams: int = 32, beam_radius: float = 0.5,
                         seed: int = 0):
    """Random beam network inside a bounding box. Mimics open-cell metal
    foam. Seed for reproducibility."""
    rng = np.random.default_rng(seed)
    (x0, y0, z0), (x1, y1, z1) = bounds
    pts = rng.uniform(
        low=[x0, y0, z0], high=[x1, y1, z1], size=(n_beams * 2, 3))
    segments = [(tuple(pts[2 * i]), tuple(pts[2 * i + 1]))
                for i in range(n_beams)]

    def f(x, y, z):
        r = np.inf
        for (ax, ay, az), (bx, by, bz) in segments:
            pa_x, pa_y, pa_z = x - ax, y - ay, z - az
            ba_x, ba_y, ba_z = bx - ax, by - ay, bz - az
            ba_dot = ba_x ** 2 + ba_y ** 2 + ba_z ** 2
            if ba_dot < 1e-12:
                continue
            t = np.clip(
                (pa_x * ba_x + pa_y * ba_y + pa_z * ba_z) / ba_dot, 0, 1)
            dx = pa_x - t * ba_x
            dy = pa_y - t * ba_y
            dz = pa_z - t * ba_z
            d = np.sqrt(dx * dx + dy * dy + dz * dz)
            r = np.minimum(r, d)
        return r - beam_radius
    return f
