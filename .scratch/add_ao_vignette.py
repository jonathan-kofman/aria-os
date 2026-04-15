"""
Add ambient-occlusion edge darkening + radial vignette to all GL renders.
Dramatically improves perceived depth without re-rendering anything.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
from PIL import Image, ImageFilter
from pathlib import Path

GALLERY = Path("outputs/gallery_renders")
SCREENS = Path("outputs/screenshots")

# All GL renders that appear in gallery card previews
TARGETS = [
    GALLERY / "gl_nozzle.png",
    GALLERY / "gl_heat_sink.png",
    GALLERY / "gl_l_bracket.png",
    GALLERY / "gl_housing.png",
    GALLERY / "gl_sloper.png",
    GALLERY / "gl_octet.png",
    GALLERY / "gl_flange.png",
    GALLERY / "assembly_v7.png",
    GALLERY / "gl_impeller.png",
    SCREENS / "gl_iso_nozzle.png",
]

BG_THRESHOLD = 45   # pixels with all channels < this are background


def process(path, ao_strength=0.65, vignette_strength=0.28, blur_r=5):
    img = Image.open(path).convert("RGB")
    arr = np.array(img, dtype=float)
    h, w = arr.shape[:2]

    # ── 1. Ambient Occlusion (interior edge darkening) ──────────────────
    is_bg = (arr[:, :, 0] < BG_THRESHOLD) & \
            (arr[:, :, 1] < BG_THRESHOLD) & \
            (arr[:, :, 2] < BG_THRESHOLD)

    # Model mask: erode to get interior-only region (drops boundary 6px ring)
    model_mask_img = Image.fromarray((~is_bg * 255).astype(np.uint8))
    interior_img = model_mask_img.filter(ImageFilter.MinFilter(size=13))
    interior = np.array(interior_img) > 127

    # Edge detection on grayscale
    gray = Image.fromarray(arr.astype(np.uint8)).convert("L")
    edges_img = gray.filter(ImageFilter.FIND_EDGES)
    edges = np.array(edges_img, dtype=float) / 255.0

    # Keep only interior edges (removes bright boundary artifact)
    edges[~interior] = 0.0

    # Soft blur for realistic AO falloff
    ao_img = Image.fromarray((edges * 255).astype(np.uint8))
    ao_soft = np.array(ao_img.filter(ImageFilter.GaussianBlur(radius=blur_r)),
                       dtype=float) / 255.0

    # Apply AO darkening
    for c in range(3):
        arr[:, :, c] = arr[:, :, c] * (1.0 - ao_soft * ao_strength)

    # ── 2. Radial Vignette (corners darker, center pops) ────────────────
    cy, cx = h / 2.0, w / 2.0
    Y, X = np.ogrid[:h, :w]
    dist = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    vig = 1.0 - vignette_strength * (dist / max_dist) ** 1.8

    for c in range(3):
        arr[:, :, c] *= vig

    # ── 3. Slight contrast boost (make model pop off dark bg) ───────────
    # Boost model pixels only — stretch histogram slightly
    model_pixels = ~is_bg
    if model_pixels.any():
        for c in range(3):
            ch = arr[:, :, c]
            mn = ch[model_pixels].min()
            mx = ch[model_pixels].max()
            if mx > mn:
                ch_norm = (ch - mn) / (mx - mn)
                # S-curve contrast
                ch_norm = np.where(model_pixels, ch_norm ** 0.88 * (mx - mn) + mn, ch)
                arr[:, :, c] = np.where(model_pixels, ch_norm, arr[:, :, c])

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path)
    print(f"  processed: {path.name}")


for t in TARGETS:
    if t.exists():
        process(t)
    else:
        print(f"  SKIP (not found): {t.name}")

print("Done.")
