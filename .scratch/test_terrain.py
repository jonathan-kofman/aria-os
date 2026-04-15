import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from pathlib import Path
from aria_os.generators.terrain_generator import generate_terrain, parse_terrain_params

# Test 1: parse
print("=== Parse test ===")
p = parse_terrain_params("mountain terrain 5km x 5km with 200m peak contour interval 20m")
print(f"  type={p.terrain_type}  width={p.width_m}  height={p.height_m}  peak={p.peak_elevation_m}m  contour={p.contour_interval_m}m")

p2 = parse_terrain_params("rolling hills 2km wide roughness 0.8")
print(f"  type={p2.terrain_type}  width={p2.width_m}  roughness={p2.roughness}")

# Test 2: generate
print("\n=== Generate test ===")
out = Path("outputs/terrain")
result = generate_terrain("mountain terrain 3km x 3km with 150m peak", output_dir=str(out))
print(f"  terrain_type : {result.get('terrain_type')}")
print(f"  size         : {result.get('width_m')}m x {result.get('height_m')}m")
print(f"  peak         : {result.get('peak_elevation_m')}m")
print(f"  n_contours   : {result.get('n_contours')}")
print(f"  resolution   : {result.get('grid_resolution')}")
dxf = result.get('dxf_path')
stl = result.get('stl_path')
if dxf:
    sz = Path(dxf).stat().st_size if Path(dxf).is_file() else 0
    print(f"  DXF          : {dxf}  ({sz//1024} KB)")
if stl:
    sz = Path(stl).stat().st_size if Path(stl).is_file() else 0
    print(f"  STL          : {stl}  ({sz//1024} KB)")
print("Done.")
