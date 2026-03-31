
import cadquery as cq

DRUM_OD_MM    = 150.0
DRUM_W_MM     = 50.0
FLANGE_OD_MM  = 172.5
FLANGE_TH_MM  = 6.0
HUB_OD_MM     = 25.0
GROOVE_D_MM   = 3.0
GROOVE_W_MM   = 30.0
N_GROOVES     = 1

# Drum core
drum = cq.Workplane("XY").circle(DRUM_OD_MM / 2.0).extrude(DRUM_W_MM)

# Flanges (bottom + top)
fl_b = cq.Workplane("XY").circle(FLANGE_OD_MM / 2.0).extrude(FLANGE_TH_MM)
fl_t = (cq.Workplane("XY").workplane(offset=DRUM_W_MM - FLANGE_TH_MM)
        .circle(FLANGE_OD_MM / 2.0).extrude(FLANGE_TH_MM))

# Hub bore (through)
hub_bore = (cq.Workplane("XY").workplane(offset=-1.0)
            .circle(HUB_OD_MM / 2.0).extrude(DRUM_W_MM + 2.0))

result = drum.union(fl_b).union(fl_t).cut(hub_bore)

# Rope guide channel(s) — helical groove(s) cut into drum OD surface
# Approximated as circumferential V-grooves evenly spaced along drum width
groove_r_outer = DRUM_OD_MM / 2.0 + 0.1   # cut slightly past OD
groove_r_inner = DRUM_OD_MM / 2.0 - GROOVE_D_MM
if N_GROOVES > 0 and GROOVE_D_MM > 0.5:
    spacing = (DRUM_W_MM - 2 * FLANGE_TH_MM) / max(N_GROOVES, 1)
    for gi in range(N_GROOVES):
        z_center = FLANGE_TH_MM + spacing * (gi + 0.5)
        groove_ring = (
            cq.Workplane("XY")
            .workplane(offset=z_center - GROOVE_W_MM / 2.0)
            .circle(groove_r_outer)
            .circle(groove_r_inner)
            .extrude(GROOVE_W_MM)
        )
        result = result.cut(groove_ring)

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_spool.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_spool.stl"
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
