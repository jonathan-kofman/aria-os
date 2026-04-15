import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from aria_os.scan_pipeline import run_scan_pipeline

result = run_scan_pipeline(
    'outputs/cad/stl/aria_housing.stl',
    material='aluminium_6061',
    tags=['housing', 'test'],
)

print(f"Part ID:    {result.id}")
print(f"Dimensions: {result.bounding_box.x} x {result.bounding_box.y} x {result.bounding_box.z} mm")
print(f"Volume:     {result.volume_mm3} mm3")
print(f"Topology:   {result.topology}")
print(f"Confidence: {result.confidence:.0%}")
print(f"Primitives:")
for p in result.primitives_summary:
    print(f"  {p}")
