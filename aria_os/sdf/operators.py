"""
Advanced SDF operators beyond basic booleans — the bridge between
geometry-as-math and geometry-as-design. These are what make SDF more
expressive than B-rep for certain classes of parts.
"""
from __future__ import annotations

import numpy as np


def op_displace(a, displacement_fn, amplitude: float = 0.5):
    """Perturb a surface by adding a scalar displacement function.
    displacement_fn: f(x,y,z) -> scalar (e.g. a noise field).
    Used for surface texturing, roughness, camouflage patterns.
    """
    def f(x, y, z):
        return a(x, y, z) + amplitude * displacement_fn(x, y, z)
    return f


def op_morph(a, b, t: float = 0.5):
    """Linear interpolation between two SDFs. t=0 -> a, t=1 -> b.
    Great for keyframed geometry or FGM transitions."""
    t = float(np.clip(t, 0.0, 1.0))
    def f(x, y, z):
        return (1.0 - t) * a(x, y, z) + t * b(x, y, z)
    return f


def op_round_sdf(a, radius: float = 0.1):
    """Round off all edges by subtracting `radius` from the SDF.
    (Identical to offset() mathematically but intent-named.)"""
    def f(x, y, z):
        return a(x, y, z) - radius
    return f


def op_chamfer_sdf(a, b, chamfer: float = 0.1):
    """Two-SDF chamfered intersection. Produces a 45-degree bevel where
    the two SDFs meet instead of a sharp corner."""
    def f(x, y, z):
        da, db = a(x, y, z), b(x, y, z)
        return np.maximum(np.maximum(da, db),
                          (da + db + chamfer) * np.sqrt(0.5))
    return f


# ---------------------------------------------------------------------------
# Text engraving — raster a string into a 2D distance field, then extrude
# ---------------------------------------------------------------------------

def _text_bitmap(text: str, size_px: int = 48) -> np.ndarray | None:
    """Render a string to a 2D bool bitmap via PIL. Returns None if PIL
    isn't available (degrades gracefully)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    try:
        font = ImageFont.truetype("arial.ttf", size_px)
    except OSError:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", size_px)
        except OSError:
            font = ImageFont.load_default()
    # Measure
    bbox = font.getbbox(text) if hasattr(font, "getbbox") else (0, 0, len(text) * size_px, size_px)
    w = max(bbox[2] - bbox[0], 1)
    h = max(bbox[3] - bbox[1], 1)
    img = Image.new("L", (w + 4, h + 4), 0)
    draw = ImageDraw.Draw(img)
    draw.text((2 - bbox[0], 2 - bbox[1]), text, fill=255, font=font)
    arr = np.array(img) > 128
    return arr


def op_engrave_text(a, text: str,
                    center: tuple = (0, 0, 0),
                    height_mm: float = 5.0,
                    depth: float = 0.3,
                    axis: str = "z"):
    """Engrave text into a surface of another SDF. The text becomes a
    planar cutter that digs `depth` mm into the surface normal to `axis`.

    Uses PIL for glyph rasterization. Skips silently (returns `a`) if PIL
    isn't available.

    The bitmap is sampled as a 2D SDF via distance transform for smooth
    edges even at coarse voxel resolutions.
    """
    bmp = _text_bitmap(text, size_px=max(16, int(height_mm * 12)))
    if bmp is None:
        return a  # degrade gracefully
    try:
        from scipy.ndimage import distance_transform_edt
    except Exception:
        return a
    inside = distance_transform_edt(bmp) - distance_transform_edt(~bmp)
    # Normalise to mm using height_mm spanning the bitmap height
    px_per_mm = bmp.shape[0] / height_mm
    sdf2d = -inside / px_per_mm  # inside glyph = negative
    # Position mapping
    cx, cy, cz = center
    h, w = bmp.shape
    hm_w = w / px_per_mm
    hm_h = height_mm
    half_w = hm_w / 2
    half_h = hm_h / 2

    def sample_text(x_mm, y_mm):
        u = (x_mm + half_w) / hm_w * (w - 1)
        v = (half_h - y_mm) / hm_h * (h - 1)
        u_i = np.clip(u.astype(int), 0, w - 1)
        v_i = np.clip(v.astype(int), 0, h - 1)
        return sdf2d[v_i, u_i]

    def f(x, y, z):
        base = a(x, y, z)
        if axis == "z":
            tx, ty, tz = x - cx, y - cy, z - cz
            d_plane = np.abs(tz) - depth / 2
            d_text2d = sample_text(tx, ty)
        elif axis == "y":
            tx, ty, tz = x - cx, z - cz, y - cy
            d_plane = np.abs(tz) - depth / 2
            d_text2d = sample_text(tx, ty)
        else:
            tx, ty, tz = y - cy, z - cz, x - cx
            d_plane = np.abs(tz) - depth / 2
            d_text2d = sample_text(tx, ty)
        d_text_3d = np.maximum(d_text2d, d_plane)
        # Subtract text volume from base
        return np.maximum(base, -d_text_3d)

    return f
