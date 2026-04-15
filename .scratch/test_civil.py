import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
import os
from pathlib import Path
from aria_os.autocad.dxf_exporter import generate_civil_dxf

print("Testing civil plan generation...")

result = generate_civil_dxf(
    'residential road plan 500m long with storm drainage and sidewalks',
    state='national',
    units_type='metric',
    project='Test Road',
    drawn_by='ARIA'
)
print(f"Output: {result}")
if result and os.path.exists(result):
    sz = os.path.getsize(result)
    print(f"Size: {sz:,} bytes  PASS" if sz > 1000 else f"Size: {sz} bytes  WARN: small file")
else:
    print("FAIL: no output file")
print("Done.")
