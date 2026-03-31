
import cadquery as cq

WIDTH_MM      = 80.0
HEIGHT_MM     = 50.0
THICKNESS_MM  = 6.0
ROLLER_D_MM   = 30.0
BORE_MM       = 8.0
ROPE_W_MM     = 12.0
ARM_W_MM      = 14.399999999999999
N_MOUNT       = 2
MOUNT_D_MM    = 6.0

# --- Base mounting plate ---
base_plate = cq.Workplane("XY").box(WIDTH_MM, ARM_W_MM, THICKNESS_MM)

# --- Two vertical bracket arms ---
arm_gap = ROPE_W_MM + 2.0  # gap between inner faces = rope slot
arm_h   = HEIGHT_MM - THICKNESS_MM
left_arm = (cq.Workplane("XY")
    .workplane(offset=THICKNESS_MM)
    .center(0, -(arm_gap / 2.0 + ARM_W_MM / 2.0))
    .box(ARM_W_MM, ARM_W_MM, arm_h, centered=False)
    .translate((-ARM_W_MM / 2.0, 0, 0)))
right_arm = (cq.Workplane("XY")
    .workplane(offset=THICKNESS_MM)
    .center(0, (arm_gap / 2.0 - ARM_W_MM / 2.0))
    .box(ARM_W_MM, ARM_W_MM, arm_h, centered=False)
    .translate((-ARM_W_MM / 2.0, 0, 0)))

result = base_plate.union(left_arm).union(right_arm)

# --- Roller (cylinder between arms) ---
roller_z = THICKNESS_MM + arm_h * 0.65  # roller sits at 65% of arm height
roller_y_start = -(arm_gap / 2.0 + ARM_W_MM)
roller_y_len   = arm_gap + 2 * ARM_W_MM
roller = (cq.Workplane("XZ")
    .workplane(offset=roller_y_start)
    .center(roller_z, 0)
    .circle(ROLLER_D_MM / 2.0)
    .extrude(roller_y_len))
# Roller bore (axle hole through roller + arms)
axle_hole = (cq.Workplane("XZ")
    .workplane(offset=roller_y_start - 1.0)
    .center(roller_z, 0)
    .circle(BORE_MM / 2.0)
    .extrude(roller_y_len + 2.0))
result = result.union(roller).cut(axle_hole)

# --- Rope slot (cut gap between arms through the roller zone) ---
slot_cut = (cq.Workplane("XY")
    .workplane(offset=THICKNESS_MM - 0.5)
    .center(0, 0)
    .rect(WIDTH_MM + 2, ROPE_W_MM)
    .extrude(arm_h + 1.0))
# Only cut where there's no arm or roller — handled by union order above

# --- Mounting holes in base plate ---
if N_MOUNT > 0:
    margin = WIDTH_MM * 0.15
    x_start = -(WIDTH_MM / 2.0 - margin)
    x_end   = (WIDTH_MM / 2.0 - margin)
    if N_MOUNT == 1:
        pts = [(0, 0)]
    else:
        pts = [(round(x_start + (x_end - x_start) * i / (N_MOUNT - 1), 3), 0)
               for i in range(N_MOUNT)]
    mount_holes = (cq.Workplane("XY").workplane(offset=-1.0)
                   .pushPoints(pts)
                   .circle(MOUNT_D_MM / 2.0).extrude(THICKNESS_MM + 2.0))
    result = result.cut(mount_holes)

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_rope_guide.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_rope_guide.stl"
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
