"""
Replace white/near-white backgrounds on all GL renders with dark studio background.
Uses PIL flood-fill from image corners.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from PIL import Image, ImageFilter
import numpy as np
from pathlib import Path

DARK_BG = (13, 17, 23)      # #0d1117 — matches gallery dark

def darken_background(path, thresh=20):
    """Flood-fill from all 4 corners to replace white background with dark."""
    img = Image.open(path).convert("RGB")
    # Flood fill from each corner
    from PIL import ImageDraw
    for corner in [(0, 0), (img.width-1, 0), (0, img.height-1), (img.width-1, img.height-1)]:
        pixel = img.getpixel(corner)
        # Only fill if the corner pixel is light (likely background)
        if all(c > 200 for c in pixel):
            ImageDraw.floodfill(img, corner, DARK_BG, thresh=thresh)
    img.save(path)
    print(f"  darkened: {Path(path).name}")

RENDERS = Path("outputs/gallery_renders")
SCREENS = Path("outputs/screenshots")

targets = [
    RENDERS / "gl_nozzle.png",
    RENDERS / "gl_heat_sink.png",
    RENDERS / "gl_l_bracket.png",
    RENDERS / "gl_flange.png",
    RENDERS / "gl_housing.png",
    RENDERS / "gl_sloper.png",
    RENDERS / "gl_octet.png",
    RENDERS / "assembly_v7.png",
    RENDERS / "gl_impeller.png",
    RENDERS / "gl_gear.png",
    SCREENS / "gl_iso_nozzle.png",
]

for t in targets:
    if t.exists():
        darken_background(str(t))
    else:
        print(f"  SKIP (not found): {t.name}")

print("Done.")
