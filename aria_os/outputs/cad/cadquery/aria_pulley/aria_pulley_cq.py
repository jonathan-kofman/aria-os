
import cadquery as cq, math

OD_MM          = 80.0
GROOVE_DEPTH   = 5.0
WIDTH_MM       = 20.0
BORE_MM        = 10.0
GROOVE_ANGLE   = 38.0
N_GROOVES      = 1
HUB_OD_MM     = 24.0

# --- Main pulley body ---
outer = cq.Workplane("XY").circle(OD_MM / 2.0).extrude(WIDTH_MM)

# --- V-groove(s): revolved trapezoidal profile cut from OD ---
# V-groove profile: two angled cuts converging at groove bottom radius
groove_r_bottom = OD_MM / 2.0 - GROOVE_DEPTH
groove_half_angle = math.radians(GROOVE_ANGLE / 2.0)
groove_top_width = 2.0 * GROOVE_DEPTH * math.tan(groove_half_angle)

groove_spacing = WIDTH_MM / max(N_GROOVES, 1)
for gi in range(N_GROOVES):
    z_center = groove_spacing * (gi + 0.5)
    # Triangular groove profile revolved around Z axis
    # Points: outer-left, bottom-center, outer-right (in XZ plane at Y=0)
    half_w = groove_top_width / 2.0
    try:
        groove_cut = (
            cq.Workplane("XZ")
            .workplane(offset=0)
            .moveTo(OD_MM / 2.0 + 0.5, z_center - half_w)
            .lineTo(groove_r_bottom,     z_center)
            .lineTo(OD_MM / 2.0 + 0.5, z_center + half_w)
            .close()
            .revolve(360, (0, 0, 0), (0, 0, 1))
        )
        outer = outer.cut(groove_cut)
    except Exception:
        # Fallback: simple annular groove
        groove_void = (
            cq.Workplane("XY")
            .workplane(offset=z_center - half_w)
            .circle(OD_MM / 2.0 + 0.1)
            .circle(groove_r_bottom)
            .extrude(groove_top_width)
        )
        outer = outer.cut(groove_void)

# --- Bore ---
bore_cyl = (
    cq.Workplane("XY")
    .workplane(offset=-1.0)
    .circle(BORE_MM / 2.0)
    .extrude(WIDTH_MM + 2.0)
)
result = outer.cut(bore_cyl)

# --- Keyway on bore (standard rectangular) ---
keyway_w = max(BORE_MM * 0.25, 2.0)
keyway_d = keyway_w * 0.6
keyway = (cq.Workplane("XY").workplane(offset=-0.5)
          .center(0, BORE_MM / 2.0 + keyway_d / 2.0 - 0.5)
          .rect(keyway_w, keyway_d)
          .extrude(WIDTH_MM + 1.0))
result = result.cut(keyway)

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_pulley.step"
_stl  = "C:/Users/jonko/AppData/Local/Temp/tmpemz7wo79/aria_pulley.stl"
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
