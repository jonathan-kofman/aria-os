"""
Direct test of the climbing sloper template: generate → export STEP/STL → verify geometry → render.
Bypasses agent loop entirely to isolate the template quality.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')

from pathlib import Path
import cadquery as cq
import trimesh

from aria_os.generators.cadquery_generator import _cq_climbing_sloper

PARAMS = {'width_mm': 130, 'depth_mm': 90, 'height_mm': 45}
OUT_DIR = Path('outputs/cad')
STEP_OUT = OUT_DIR / 'step/template_climbing_sloper.step'
STL_OUT  = OUT_DIR / 'stl/template_climbing_sloper.stl'

print("=== Generating sloper template ===")
code = _cq_climbing_sloper(PARAMS)
print(f"Code length: {len(code)} chars")

ns = {}
exec(code, ns)
result = ns.get('result')
if result is None:
    print("ERROR: result is None")
    sys.exit(1)

print("\n=== Exporting STEP + STL ===")
cq.exporters.export(result, str(STEP_OUT))
cq.exporters.export(result, str(STL_OUT))
print(f"STEP: {STEP_OUT.stat().st_size / 1024:.1f} KB")
print(f"STL : {STL_OUT.stat().st_size / 1024:.1f} KB")

print("\n=== Mesh validation ===")
m = trimesh.load(str(STL_OUT))
print(f"Watertight: {m.is_watertight}")
print(f"Faces: {len(m.faces):,}")
print(f"Volume: {m.volume:,.0f} mm³")
extents = m.bounding_box.extents
print(f"BBOX: {extents[0]:.1f} x {extents[1]:.1f} x {extents[2]:.1f} mm")

print("\n=== Rendering verification images ===")
from aria_os.visual_verifier import _render_views
goal = 'asymmetric freeform climbing sloper hold 130mm wide 90mm deep 45mm tall'
out_dir = Path('outputs/screenshots')
try:
    paths, labels = _render_views(str(STL_OUT), goal, str(out_dir))
    for p, l in zip(paths, labels):
        print(f"  {l}: {Path(p).name}")
except Exception as e:
    print(f"  render error: {e}")

print("\nDone.")
