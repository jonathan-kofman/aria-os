# aria_os.visual_qa

Reusable visual verification framework. Takes a feature output
(DXF, STL, JSON, HTTP endpoint) and produces a visual artifact plus
a pass/fail signal with a confidence score.

## Public API

```python
from aria_os.visual_qa import render_dxf, render_stl, verify_sheet_metal_dxf
```

### `render_dxf(dxf_path, png_out, layers=None) -> dict`

Parses a DXF with `ezdxf`, draws each entity on a matplotlib `Agg`
figure, saves a PNG and returns `{ok, png_path, bbox, layer_counts,
entity_total}`. Headless-Linux safe.

### `render_stl(stl_path, out_dir, goal="...") -> dict`

Thin wrapper around `aria_os.visual_verifier._render_views`. Returns
`{ok, png_paths, view_labels, bbox, triangle_count}`.

### `verify_sheet_metal_dxf(dxf_path, expected_bbox_mm=None, expected_holes=0, bbox_tolerance=0.05) -> dict`

Deterministic checks for sheet-metal flat patterns emitted by
`aria_os.sheet_metal_unfold`. Checks: file readable, OUTLINE layer
populated, HOLES count matches, OUTLINE bbox within tolerance of
`expected_bbox_mm`. Returns
`{passed, confidence, checks, bbox, layer_counts}`.

## CLI

```bash
python -m aria_os.visual_qa render-dxf part.dxf part.png
python -m aria_os.visual_qa render-dxf part.dxf part.png --layers OUTLINE,BEND
python -m aria_os.visual_qa render-stl part.stl ./preview_dir --goal "housing"
python -m aria_os.visual_qa verify-sheet-metal part.dxf --expected-bbox 120x80 --expected-holes 4
```

All subcommands emit JSON to stdout and exit 0 on success, 1 on
failure.

## Contract

Every function in this package follows the never-raise rule: a
bad input or missing file returns a dict with `ok: False` (or
`passed: False`) and an `error` key. Callers can rely on a stable
dict shape without wrapping in `try/except`.
