
import cadquery as cq, math

OD_MM        = 180.0
WIDTH_MM     = 40.0
SHAFT_D_MM   = 20.0
WALL_MM      = 8.0
HUB_OD_MM    = 50.0
HUB_H_MM     = 12.0
N_GROOVES    = 8
BOLT_D_MM    = 6.0

# --- Outer drum shell ---
outer = cq.Workplane("XY").circle(OD_MM / 2.0).extrude(WIDTH_MM)
inner_void = (cq.Workplane("XY").workplane(offset=WALL_MM)
              .circle(OD_MM / 2.0 - WALL_MM).extrude(WIDTH_MM - WALL_MM + 1.0))
drum = outer.cut(inner_void)

# --- Hub with bore ---
hub = (cq.Workplane("XY")
       .circle(HUB_OD_MM / 2.0)
       .circle(SHAFT_D_MM / 2.0)
       .extrude(HUB_H_MM))
# Web plate connecting hub to drum
web = (cq.Workplane("XY")
       .circle(OD_MM / 2.0 - WALL_MM)
       .circle(HUB_OD_MM / 2.0)
       .extrude(WALL_MM))
result = drum.union(hub).union(web)

# --- Bore through ---
bore_cyl = (cq.Workplane("XY").workplane(offset=-1.0)
            .circle(SHAFT_D_MM / 2.0).extrude(WIDTH_MM + 2.0))
result = result.cut(bore_cyl)

# --- Hub bolt holes ---
bolt_pts = [(17.5, 0.0), (0.0, 17.5), (-17.5, 0.0), (-0.0, -17.5)]
if len(bolt_pts) > 0:
    bolt_holes = (cq.Workplane("XY").workplane(offset=-1.0)
                  .pushPoints(bolt_pts)
                  .circle(BOLT_D_MM / 2.0).extrude(HUB_H_MM + 2.0))
    result = result.cut(bolt_holes)

# --- Friction grooves (circumferential score lines on inner drum surface) ---
if N_GROOVES > 0:
    groove_d = max(0.5, WALL_MM * 0.08)
    groove_w = 1.5
    for gi in range(N_GROOVES):
        z_g = WALL_MM + (WIDTH_MM - WALL_MM) * (gi + 0.5) / N_GROOVES
        try:
            groove = (cq.Workplane("XY").workplane(offset=z_g - groove_w / 2)
                      .circle(OD_MM / 2.0 - WALL_MM + groove_d)
                      .circle(OD_MM / 2.0 - WALL_MM)
                      .extrude(groove_w))
            result = result.cut(groove)
        except Exception:
            pass

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_brake_drum.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_brake_drum.stl"
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
