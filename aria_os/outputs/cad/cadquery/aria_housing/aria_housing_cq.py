
import cadquery as cq

WIDTH_MM  = 200.0
HEIGHT_MM = 150.0
DEPTH_MM  = 100.0
WALL_MM   = 5.0
BOSS_OD   = 12.5
MOUNT_D   = 5.0

# --- Outer box ---
outer = cq.Workplane("XY").box(WIDTH_MM, DEPTH_MM, HEIGHT_MM)

# --- Shell out interior (open top face for lid) ---
result = outer.shell(-WALL_MM)

# --- Mounting bosses at 4 corners (inside, at bottom) ---
mount_pts = [(63.64, 28.284), (-63.64, 28.284), (-63.64, -28.284), (63.64, -28.284)]
if len(mount_pts) > 0:
    for px, py in mount_pts:
        boss = (cq.Workplane("XY")
                .workplane(offset=-HEIGHT_MM / 2.0)
                .center(px, py)
                .circle(BOSS_OD / 2.0)
                .extrude(HEIGHT_MM - WALL_MM))
        boss_hole = (cq.Workplane("XY")
                     .workplane(offset=-HEIGHT_MM / 2.0 - 1.0)
                     .center(px, py)
                     .circle(MOUNT_D / 2.0)
                     .extrude(HEIGHT_MM + 2.0))
        result = result.union(boss).cut(boss_hole)

# --- Lid screw bosses on rim (top face corners) ---
lid_pts = [(WIDTH_MM / 2 - WALL_MM * 1.5, DEPTH_MM / 2 - WALL_MM * 1.5),
           (-WIDTH_MM / 2 + WALL_MM * 1.5, DEPTH_MM / 2 - WALL_MM * 1.5),
           (-WIDTH_MM / 2 + WALL_MM * 1.5, -DEPTH_MM / 2 + WALL_MM * 1.5),
           (WIDTH_MM / 2 - WALL_MM * 1.5, -DEPTH_MM / 2 + WALL_MM * 1.5)]
for lx, ly in lid_pts:
    lid_hole = (cq.Workplane("XY")
                .workplane(offset=HEIGHT_MM / 2.0 - WALL_MM - 1.0)
                .center(lx, ly)
                .circle(MOUNT_D * 0.4)
                .extrude(WALL_MM + 2.0))
    result = result.cut(lid_hole)

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_housing.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_housing.stl"
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
