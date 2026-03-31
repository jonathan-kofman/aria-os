
import cadquery as cq, math

LENGTH_MM        = 60.0
WIDTH_MM         = 12.0
THICKNESS_MM     = 6.0
PIVOT_HOLE_D_MM  = 6.0
TIP_DEPTH_MM     = 3.0
TIP_ANGLE_DEG    = 30.0
SPRING_D_MM      = 3.0

# --- Main body: tapered pawl shape ---
# Wider at pivot end, narrowing toward tip for engagement
body = cq.Workplane("XY").box(LENGTH_MM, WIDTH_MM, THICKNESS_MM)

# --- Tapered tip: angled engagement face at free end ---
# Cut a wedge from the top-right corner to create the tooth engagement surface.
# Drive face (steep) engages the ratchet tooth; back face (shallow) rides over.
tip_x = LENGTH_MM / 2.0
tip_cut_len = TIP_DEPTH_MM / math.tan(math.radians(TIP_ANGLE_DEG)) if TIP_ANGLE_DEG > 0 else TIP_DEPTH_MM
wedge = (
    cq.Workplane("XY")
    .workplane(offset=-0.5)
    .moveTo(tip_x - tip_cut_len, WIDTH_MM / 2.0 + 0.1)
    .lineTo(tip_x + 0.1,         WIDTH_MM / 2.0 + 0.1)
    .lineTo(tip_x + 0.1,         WIDTH_MM / 2.0 - TIP_DEPTH_MM)
    .close()
    .extrude(THICKNESS_MM + 1.0)
)
result = body.cut(wedge)

# --- Pivot bore at left end ---
pivot = (cq.Workplane("XY").workplane(offset=-1.0)
         .center(-LENGTH_MM / 2.0 + WIDTH_MM / 2.0, 0)
         .circle(PIVOT_HOLE_D_MM / 2.0).extrude(THICKNESS_MM + 2.0))
result = result.cut(pivot)

# --- Spring return hole (smaller, between pivot and midpoint) ---
spring_pos_x = -LENGTH_MM / 2.0 + WIDTH_MM * 1.5
spring = (cq.Workplane("XY").workplane(offset=-1.0)
          .center(spring_pos_x, 0)
          .circle(SPRING_D_MM / 2.0).extrude(THICKNESS_MM + 2.0))
result = result.cut(spring)

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_catch_pawl.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_catch_pawl.stl"
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
