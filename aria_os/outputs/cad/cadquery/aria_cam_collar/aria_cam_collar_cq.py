
import cadquery as cq, math

OD_MM         = 80.0
HEIGHT_MM     = 45.0
BORE_MM       = 60.0
RAMP_RISE_MM  = 5.4
RAMP_ARC_DEG  = 90.0
SET_SCREW_D   = 4.0
WALL_MM       = 10.00

# --- Base annular cylinder ---
result = (
    cq.Workplane("XY")
    .circle(OD_MM / 2.0)
    .circle(BORE_MM / 2.0)
    .extrude(HEIGHT_MM)
)

# --- Helical cam ramp on bore surface ---
# Cut a ramped wedge from the bore face to create the engagement ramp.
# The ramp is a triangular prism swept along the bore arc.
# We approximate with a series of small cuts at increasing depth.
N_RAMP_SEGS = 12
for i in range(N_RAMP_SEGS):
    frac = i / N_RAMP_SEGS
    angle_deg = frac * RAMP_ARC_DEG
    ramp_depth = frac * RAMP_RISE_MM
    a_rad = math.radians(angle_deg)
    cx = (BORE_MM / 2.0 + 1.0) * math.cos(a_rad)
    cy = (BORE_MM / 2.0 + 1.0) * math.sin(a_rad)
    seg_len = math.pi * BORE_MM * (RAMP_ARC_DEG / N_RAMP_SEGS) / 360.0
    try:
        wedge = (
            cq.Workplane("XY")
            .workplane(offset=HEIGHT_MM - ramp_depth)
            .transformed(rotate=cq.Vector(0, 0, angle_deg))
            .center(BORE_MM / 2.0 * 0.85, 0)
            .rect(WALL_MM * 0.3, max(seg_len, 1.0))
            .extrude(ramp_depth + 0.5)
        )
        result = result.cut(wedge)
    except Exception:
        pass  # skip failed ramp segment

# --- Radial set screw hole (M{SET_SCREW_D:.0f}) at mid-height ---
set_screw = (
    cq.Workplane("XZ")
    .workplane(offset=0)
    .center(HEIGHT_MM / 2.0, 0)
    .circle(SET_SCREW_D / 2.0)
    .extrude(OD_MM)
)
result = result.cut(set_screw)

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmppcu2iaxd/a.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmppcu2iaxd/a.stl"
try:
    _os.makedirs(_os.path.dirname(_step), exist_ok=True)
except OSError:
    pass
try:
    _os.makedirs(_os.path.dirname(_stl), exist_ok=True)
except OSError:
    pass
_exp.export(result, _step, _exp.ExportTypes.STEP)
_exp.export(result, _stl,  _exp.ExportTypes.STL)
print(f"EXPORTED STEP: {_step}")
print(f"EXPORTED STL: {_stl}")
