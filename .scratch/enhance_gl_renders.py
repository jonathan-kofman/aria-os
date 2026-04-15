"""
Post-process all GL renders: Sobel edge outlines + directional specular +
metallic blue-silver tint + improved AO. Overwrites files in-place.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
from pathlib import Path
from PIL import Image, ImageFilter

OUT = Path("outputs/gallery_renders")
BG_THRESH = 35   # pixels darker than this on all channels = background

TARGETS = [
    "gl_nozzle.png",
    "gl_heat_sink.png",
    "gl_l_bracket.png",
    "gl_housing.png",
    "gl_sloper.png",
    "gl_octet.png",
    "gl_flange.png",
    "gl_impeller.png",
]

# Load the iso nozzle from screenshots (used in card 1)
EXTRA = [
    (Path("outputs/screenshots/gl_iso_nozzle.png"), Path("outputs/screenshots/gl_iso_nozzle.png")),
]


def is_model(arr):
    """Return boolean mask: True = model pixel, False = background."""
    gray = arr[:, :, 0].astype(float) * 0.299 + arr[:, :, 1] * 0.587 + arr[:, :, 2] * 0.114
    return gray > BG_THRESH


def sobel_edges(gray_f):
    """2-pass gradient magnitude via PIL."""
    img_g = Image.fromarray(np.clip(gray_f, 0, 255).astype(np.uint8))
    edges = img_g.filter(ImageFilter.FIND_EDGES)
    return np.array(edges, dtype=float) / 255.0


def enhance(src: Path, dst: Path):
    if not src.exists():
        print(f"  SKIP (not found): {src}")
        return

    img = Image.open(src).convert("RGB")
    arr = np.array(img, dtype=float)
    H, W = arr.shape[:2]

    mask = is_model(arr)  # True = model

    # ── 1. Metallic aluminum tint ────────────────────────────────────────────
    # Shift model pixels slightly toward blue-silver (AL 6061 look)
    # Darken red channel a touch, boost blue slightly
    arr[:, :, 0] = np.where(mask, np.clip(arr[:, :, 0] * 0.93, 0, 255), arr[:, :, 0])
    arr[:, :, 2] = np.where(mask, np.clip(arr[:, :, 2] * 1.06 + 4, 0, 255), arr[:, :, 2])

    # ── 2. AO: interior concavity darkening ──────────────────────────────────
    model_img = Image.fromarray((mask * 255).astype(np.uint8))
    # Erode mask to get deep interior pixels only
    interior = np.array(model_img.filter(ImageFilter.MinFilter(size=15)), dtype=bool)
    gray_f = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    raw_edges = sobel_edges(gray_f)
    ao = raw_edges.copy()
    ao[~interior] = 0.0
    ao_soft = np.array(
        Image.fromarray((ao * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius=4)),
        dtype=float,
    ) / 255.0
    for c in range(3):
        arr[:, :, c] = np.clip(arr[:, :, c] * (1.0 - ao_soft * 0.60), 0, 255)

    # ── 3. Edge outline (Sobel-based dark silhouette) ────────────────────────
    gray_f2 = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    edge_raw = sobel_edges(gray_f2)
    # Keep edges only where they straddle model/bg boundary or on model
    # Dilate mask slightly to include boundary
    boundary_mask = np.array(
        Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(size=3)),
        dtype=bool,
    )
    edge_raw[~boundary_mask] = 0.0
    # Boost contrast — exaggerate strong edges, suppress weak
    edge_strong = np.clip((edge_raw - 0.08) / 0.45, 0, 1) ** 0.65
    edge_soft = np.array(
        Image.fromarray((edge_strong * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius=0.6)),
        dtype=float,
    ) / 255.0
    for c in range(3):
        arr[:, :, c] = np.clip(arr[:, :, c] * (1.0 - edge_soft * 0.80), 0, 255)

    # ── 4. Directional specular highlight ────────────────────────────────────
    Y_, X_ = np.ogrid[:H, :W]
    # Primary highlight: upper-left (like studio key light at 10 o'clock)
    light_x, light_y = W * 0.22, H * 0.15
    spec_dist = np.sqrt((X_ - light_x) ** 2 + (Y_ - light_y) ** 2) / (max(W, H) * 0.52)
    spec = np.clip(1.0 - spec_dist, 0, 1) ** 3.5 * 0.32
    spec = np.where(mask, spec, 0.0)
    for c in range(3):
        arr[:, :, c] = np.clip(arr[:, :, c] + spec * 255, 0, 255)

    # ── 5. Vignette ──────────────────────────────────────────────────────────
    cy, cx = H / 2.0, W / 2.0
    dist = np.sqrt((X_ - cx) ** 2 + (Y_ - cy) ** 2)
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    vig = 1.0 - 0.30 * (dist / max_dist) ** 1.9
    for c in range(3):
        arr[:, :, c] = np.clip(arr[:, :, c] * vig, 0, 255)

    # ── 6. Background: radial dark gradient (deep navy -> near-black) ────────
    bg_dark = [8, 11, 15]   # near-black blue-black
    bg_mid_v = [14, 20, 30]  # slightly lighter center
    bg_grad = np.clip(1.0 - dist / (max_dist * 0.7), 0, 1)  # (H, W)
    for c in range(3):
        bg_val = bg_dark[c] + (bg_mid_v[c] - bg_dark[c]) * bg_grad
        arr[:, :, c] = np.where(mask, arr[:, :, c], bg_val)

    result = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    result.save(str(dst))
    sz = dst.stat().st_size
    print(f"  {dst.name}: {sz:,} bytes")


print("Enhancing GL renders...")
for fname in TARGETS:
    p = OUT / fname
    enhance(p, p)

for src, dst in EXTRA:
    enhance(Path(src), Path(dst))

print("Done.")
