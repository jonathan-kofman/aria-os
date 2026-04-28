r"""sw_matrix_contact_sheet.py - one-page visual summary of every test.

Walks outputs/feature_matrix/<slug>_views/ for every test, picks the most
informative single render (preference: isometric > top > front > section),
and stitches them into a labeled grid PNG. The user opens ONE file and
sees the result of every test in the matrix.

Output: outputs/feature_matrix/contact_sheet.png

Usage:
  python scripts/sw_matrix_contact_sheet.py
  python scripts/sw_matrix_contact_sheet.py --columns 5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "outputs" / "feature_matrix"


def _pick_view(slug_dir: Path) -> Path | None:
    """Pick the most informative single render for a test."""
    if not slug_dir.exists():
        return None
    pngs = sorted(slug_dir.glob("*.png"))
    if not pngs:
        return None
    # Preference order
    for hint in ("isometric", "iso", "top", "front", "side", "section"):
        for p in pngs:
            if hint in p.name.lower():
                return p
    return pngs[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--columns", type=int, default=4,
                    help="Tiles per row")
    ap.add_argument("--tile", type=int, default=320,
                    help="Tile size in pixels")
    args = ap.parse_args()

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("ERROR: pip install pillow", file=sys.stderr)
        sys.exit(1)

    # Load report.json for pass/fail labels
    rep_json = OUT / "report.json"
    statuses: dict[str, str] = {}
    if rep_json.exists():
        for r in json.loads(rep_json.read_text(encoding="utf-8")):
            statuses[r["slug"]] = ("PASS" if r["overall_pass"]
                                    else "FAIL")

    # Find every <slug>_views/ directory
    view_dirs = sorted([p for p in OUT.iterdir()
                        if p.is_dir() and p.name.endswith("_views")])
    if not view_dirs:
        print(f"No <slug>_views/ dirs found in {OUT}")
        sys.exit(2)

    tiles = []
    for vd in view_dirs:
        slug = vd.name.removesuffix("_views")
        view = _pick_view(vd)
        if view is None:
            continue
        try:
            img = Image.open(view).convert("RGB")
            img.thumbnail((args.tile, args.tile))
            tiles.append((slug, statuses.get(slug, "?"), img))
        except Exception as ex:
            print(f"  skip {slug}: {ex}", file=sys.stderr)

    if not tiles:
        print("No tiles rendered.")
        sys.exit(2)

    # Lay out the grid
    cols = max(1, args.columns)
    rows = math.ceil(len(tiles) / cols)
    cell_w = args.tile + 16
    cell_h = args.tile + 48  # extra space for label
    total_w = cell_w * cols + 16
    total_h = cell_h * rows + 16

    sheet = Image.new("RGB", (total_w, total_h), color="#0a0a0f")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
        font_big = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
        font_big = ImageFont.load_default()

    for idx, (slug, status, img) in enumerate(tiles):
        c = idx % cols
        r = idx // cols
        x = 8 + c * cell_w
        y = 8 + r * cell_h
        # Status banner
        color = ("#22cc55" if status == "PASS" else
                 "#cc2244" if status == "FAIL" else "#888")
        draw.rectangle([x, y, x + args.tile, y + 28], fill=color)
        draw.text((x + 8, y + 4), f"{status}  {slug}",
                  fill="#0a0a0f", font=font_big)
        # Image below banner
        sheet.paste(img, (x, y + 32))

    out_path = OUT / "contact_sheet.png"
    sheet.save(out_path, "PNG", optimize=True)
    print(f"Wrote {len(tiles)} tiles to {out_path}")
    print(f"Open: {out_path}")


if __name__ == "__main__":
    main()
