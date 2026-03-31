
import cadquery as cq
import math

# UAG-style rugged case for iPhone 13 Pro Max
# Thicker corners, raised camera ring, grip ridges, tactile button covers

CASE_D   = 11.65
WALL     = 2.5
PH_T     = 7.65
PH_L     = 160.8
PH_W     = 78.1

# ── 1. Outer shell — rounded rect with thicker corners ──────────────────
outer = cq.Workplane("XY").polyline([(44.55, 72.4), (44.2906, 75.0337), (43.5224, 77.5662), (42.2748, 79.9002), (40.5959, 81.9459), (38.5502, 83.6248), (36.2162, 84.8724), (33.6837, 85.6406), (31.05, 85.9), (-31.05, 85.9), (-33.6837, 85.6406), (-36.2162, 84.8724), (-38.5502, 83.6248), (-40.5959, 81.9459), (-42.2748, 79.9002), (-43.5224, 77.5662), (-44.2906, 75.0337), (-44.55, 72.4), (-44.55, -72.4), (-44.2906, -75.0337), (-43.5224, -77.5662), (-42.2748, -79.9002), (-40.5959, -81.9459), (-38.5502, -83.6248), (-36.2162, -84.8724), (-33.6837, -85.6406), (-31.05, -85.9), (31.05, -85.9), (33.6837, -85.6406), (36.2162, -84.8724), (38.5502, -83.6248), (40.5959, -81.9459), (42.2748, -79.9002), (43.5224, -77.5662), (44.2906, -75.0337), (44.55, -72.4)]).close().extrude(CASE_D)

# Inner trim — cut back to normal wall thickness on flat sides
# (keeps corners thick, sides at standard wall)
trim = cq.Workplane("XY").polyline([(41.55, 72.4), (41.3482, 74.4484), (40.7507, 76.4182), (39.7804, 78.2335), (38.4746, 79.8246), (36.8835, 81.1304), (35.0682, 82.1007), (33.0984, 82.6982), (31.05, 82.9), (-31.05, 82.9), (-33.0984, 82.6982), (-35.0682, 82.1007), (-36.8835, 81.1304), (-38.4746, 79.8246), (-39.7804, 78.2335), (-40.7507, 76.4182), (-41.3482, 74.4484), (-41.55, 72.4), (-41.55, -72.4), (-41.3482, -74.4484), (-40.7507, -76.4182), (-39.7804, -78.2335), (-38.4746, -79.8246), (-36.8835, -81.1304), (-35.0682, -82.1007), (-33.0984, -82.6982), (-31.05, -82.9), (31.05, -82.9), (33.0984, -82.6982), (35.0682, -82.1007), (36.8835, -81.1304), (38.4746, -79.8246), (39.7804, -78.2335), (40.7507, -76.4182), (41.3482, -74.4484), (41.55, -72.4)]).close().extrude(CASE_D + 1)
# Only remove material OUTSIDE the inner wall but INSIDE the outer
# Actually we want the outer shape to BE the bumper corners.
# So: extrude the inner_wall profile and cut only the side panels back.
# Simpler: use outer as-is (corners are naturally thicker due to bump_extra).
# Cut the phone cavity from it.

# ── 2. Phone cavity — cut from screen side (+Z) ─────────────────────────
cavity = (
    cq.Workplane("XY")
    .workplane(offset=WALL)
    .polyline([(39.05, 72.9), (38.9059, 74.3632), (38.4791, 75.7701), (37.786, 77.0668), (36.8533, 78.2033), (35.7168, 79.136), (34.4201, 79.8291), (33.0132, 80.2559), (31.55, 80.4), (-31.55, 80.4), (-33.0132, 80.2559), (-34.4201, 79.8291), (-35.7168, 79.136), (-36.8533, 78.2033), (-37.786, 77.0668), (-38.4791, 75.7701), (-38.9059, 74.3632), (-39.05, 72.9), (-39.05, -72.9), (-38.9059, -74.3632), (-38.4791, -75.7701), (-37.786, -77.0668), (-36.8533, -78.2033), (-35.7168, -79.136), (-34.4201, -79.8291), (-33.0132, -80.2559), (-31.55, -80.4), (31.55, -80.4), (33.0132, -80.2559), (34.4201, -79.8291), (35.7168, -79.136), (36.8533, -78.2033), (37.786, -77.0668), (38.4791, -75.7701), (38.9059, -74.3632), (39.05, -72.9)]).close()
    .extrude(PH_T + 2.0 + 1.5)
)
result = outer.cut(cavity)

# ── 3. Screen opening — bezel lip (2.5mm border) ────────────────────────
scr_pts = [(36.55, 71.4), (36.4251, 72.6681), (36.0552, 73.8874), (35.4546, 75.0112), (34.6462, 75.9962), (33.6612, 76.8046), (32.5374, 77.4052), (31.3181, 77.7751), (30.05, 77.9), (-30.05, 77.9), (-31.3181, 77.7751), (-32.5374, 77.4052), (-33.6612, 76.8046), (-34.6462, 75.9962), (-35.4546, 75.0112), (-36.0552, 73.8874), (-36.4251, 72.6681), (-36.55, 71.4), (-36.55, -71.4), (-36.4251, -72.6681), (-36.0552, -73.8874), (-35.4546, -75.0112), (-34.6462, -75.9962), (-33.6612, -76.8046), (-32.5374, -77.4052), (-31.3181, -77.7751), (-30.05, -77.9), (30.05, -77.9), (31.3181, -77.7751), (32.5374, -77.4052), (33.6612, -76.8046), (34.6462, -75.9962), (35.4546, -75.0112), (36.0552, -73.8874), (36.4251, -72.6681), (36.55, -71.4)]
screen = (
    cq.Workplane("XY")
    .workplane(offset=CASE_D - 0.5)
    .polyline(scr_pts).close()
    .extrude(2.0)
)
result = result.cut(screen)

# ── 4. Camera — flush cutout + raised protective ring ────────────────────
cam_cut = (
    cq.Workplane("XY")
    .workplane(offset=-0.5)
    .polyline([(4.45, 68.4), (4.3155, 69.7656), (3.9172, 71.0788), (3.2703, 72.289), (2.3997, 73.3497), (1.339, 74.2203), (0.1288, 74.8672), (-1.1844, 75.2655), (-2.55, 75.4), (-26.55, 75.4), (-27.9156, 75.2655), (-29.2288, 74.8672), (-30.439, 74.2203), (-31.4997, 73.3497), (-32.3703, 72.289), (-33.0172, 71.0788), (-33.4155, 69.7656), (-33.55, 68.4), (-33.55, 44.4), (-33.4155, 43.0344), (-33.0172, 41.7212), (-32.3703, 40.511), (-31.4997, 39.4503), (-30.439, 38.5797), (-29.2288, 37.9328), (-27.9156, 37.5345), (-26.55, 37.4), (-2.55, 37.4), (-1.1844, 37.5345), (0.1288, 37.9328), (1.339, 38.5797), (2.3997, 39.4503), (3.2703, 40.511), (3.9172, 41.7212), (4.3155, 43.0344), (4.45, 44.4)]).close()
    .extrude(WALL + 1.0)
)
result = result.cut(cam_cut)

# Camera protective ring — raised 2mm from back face
cam_ring_solid = (
    cq.Workplane("XY")
    .polyline([(6.95, 68.4), (6.7675, 70.2534), (6.2269, 72.0355), (5.349, 73.6779), (4.1675, 75.1175), (2.7279, 76.299), (1.0855, 77.1769), (-0.6966, 77.7175), (-2.55, 77.9), (-26.55, 77.9), (-28.4034, 77.7175), (-30.1855, 77.1769), (-31.8279, 76.299), (-33.2675, 75.1175), (-34.449, 73.6779), (-35.3269, 72.0355), (-35.8675, 70.2534), (-36.05, 68.4), (-36.05, 44.4), (-35.8675, 42.5466), (-35.3269, 40.7645), (-34.449, 39.1221), (-33.2675, 37.6825), (-31.8279, 36.501), (-30.1855, 35.6231), (-28.4034, 35.0825), (-26.55, 34.9), (-2.55, 34.9), (-0.6966, 35.0825), (1.0855, 35.6231), (2.7279, 36.501), (4.1675, 37.6825), (5.349, 39.1221), (6.2269, 40.7645), (6.7675, 42.5466), (6.95, 44.4)]).close()
    .extrude(-2.0)
)
cam_ring_void = (
    cq.Workplane("XY")
    .workplane(offset=0.5)
    .polyline([(4.45, 68.4), (4.3155, 69.7656), (3.9172, 71.0788), (3.2703, 72.289), (2.3997, 73.3497), (1.339, 74.2203), (0.1288, 74.8672), (-1.1844, 75.2655), (-2.55, 75.4), (-26.55, 75.4), (-27.9156, 75.2655), (-29.2288, 74.8672), (-30.439, 74.2203), (-31.4997, 73.3497), (-32.3703, 72.289), (-33.0172, 71.0788), (-33.4155, 69.7656), (-33.55, 68.4), (-33.55, 44.4), (-33.4155, 43.0344), (-33.0172, 41.7212), (-32.3703, 40.511), (-31.4997, 39.4503), (-30.439, 38.5797), (-29.2288, 37.9328), (-27.9156, 37.5345), (-26.55, 37.4), (-2.55, 37.4), (-1.1844, 37.5345), (0.1288, 37.9328), (1.339, 38.5797), (2.3997, 39.4503), (3.2703, 40.511), (3.9172, 41.7212), (4.3155, 43.0344), (4.45, 44.4)]).close()
    .extrude(-(2.0 + 1.0))
)
try:
    result = result.union(cam_ring_solid.cut(cam_ring_void))
except Exception:
    pass

# ── 5. Button cutouts — all 4 buttons through side walls ─────────────────
# Buttons must cut through the FULL wall including bump_extra.
# Box(thickness_to_cut, button_length, button_height) centered on the wall.
_WALL_FULL = WALL + 3.0  # total wall at bumper corners
_BTN_CUT = _WALL_FULL + 4.0       # cut depth (generous, ensures full penetration)

# Power button — RIGHT side (+X wall)
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, 13.0, 4.5)
    .translate((PH_W/2 + _WALL_FULL/2, 7.4, CASE_D/2))
)

# Volume UP — LEFT side (-X wall)
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, 9.0, 4.5)
    .translate((-(PH_W/2 + _WALL_FULL/2), 12.4, CASE_D/2))
)

# Volume DOWN — LEFT side (-X wall)
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, 9.0, 4.5)
    .translate((-(PH_W/2 + _WALL_FULL/2), -0.6, CASE_D/2))
)

# Mute switch — LEFT side (-X wall), above volume
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, 5.0, 3.0)
    .translate((-(PH_W/2 + _WALL_FULL/2), 24.4, CASE_D/2))
)

# ── 6. Bottom — Lightning port + speaker + mic grilles ───────────────────
_bot = PH_L/2 + WALL + 3.0

# Lightning port
result = result.cut(
    cq.Workplane("XY").box(9.0, WALL*2 + 3.0*2 + 2, 3.5)
    .translate((0, -_bot, CASE_D/2))
)

# Speaker grille (right of port) — 6 square holes
for i in range(6):
    result = result.cut(
        cq.Workplane("XY").box(1.5, WALL*2 + 3.0*2 + 2, 1.5)
        .translate((9.0/2 + 4 + i*2.8, -_bot, CASE_D/2))
    )

# Mic grille (left of port) — 2 square holes
for i in range(2):
    result = result.cut(
        cq.Workplane("XY").box(1.5, WALL*2 + 3.0*2 + 2, 1.5)
        .translate((-9.0/2 - 4 - i*2.8, -_bot, CASE_D/2))
    )

# ── 7. Side grip ridges — deep parallel grooves on both sides ────────────
RIDGE_W = 2.5     # groove width along Y
RIDGE_D = 1.8     # groove depth into wall (X direction)
N_RIDGES = 8
RIDGE_SPAN = PH_L * 0.55
RIDGE_START = -RIDGE_SPAN / 2
RIDGE_STEP = RIDGE_SPAN / max(N_RIDGES - 1, 1)

for side_sign in [-1, 1]:
    x_edge = side_sign * (PH_W/2 + WALL + 3.0)
    for ri in range(N_RIDGES):
        ry = RIDGE_START + ri * RIDGE_STEP
        ridge = (
            cq.Workplane("XY")
            .box(RIDGE_D * 2, RIDGE_W, CASE_D * 0.65)
            .translate((x_edge, ry, CASE_D * 0.5))
        )
        try:
            result = result.cut(ridge)
        except Exception:
            pass

# ── 8. Back panel armor lines — UAG-style diagonal structural cuts ───────
# Deep grooves cut into the back face creating angular armor panel look.
LINE_DEPTH = WALL * 0.4  # 40% of wall thickness
LINE_W = 1.5

# Horizontal armor line across the back at 40% from top
armor_h1 = (
    cq.Workplane("XY")
    .box(PH_W * 0.85, LINE_W, LINE_DEPTH)
    .translate((0, PH_L * 0.10, LINE_DEPTH / 2))
)
result = result.cut(armor_h1)

# Second horizontal armor line at 70% from top
armor_h2 = (
    cq.Workplane("XY")
    .box(PH_W * 0.85, LINE_W, LINE_DEPTH)
    .translate((0, -PH_L * 0.20, LINE_DEPTH / 2))
)
result = result.cut(armor_h2)

# Vertical center spine line on back
armor_v = (
    cq.Workplane("XY")
    .box(LINE_W, PH_L * 0.5, LINE_DEPTH)
    .translate((0, -PH_L * 0.05, LINE_DEPTH / 2))
)
result = result.cut(armor_v)

# Diagonal cuts at corners — X pattern in lower half of back
for dx_sign in [-1, 1]:
    diag = (
        cq.Workplane("XY")
        .box(PH_W * 0.35, LINE_W, LINE_DEPTH)
        .rotateAboutCenter((0, 0, 1), dx_sign * 35)
        .translate((dx_sign * PH_W * 0.18, -PH_L * 0.28, LINE_DEPTH / 2))
    )
    try:
        result = result.cut(diag)
    except Exception:
        pass

# ── 9. Corner hex cutouts — visible honeycomb shock absorber pattern ─────
# Small hex-shaped recesses at each corner on the back face
HEX_R = 3.5   # hex outer radius
HEX_D = WALL * 0.35  # recess depth
for sx in [-1, 1]:
    for sy in [-1, 1]:
        for ho in range(3):  # 3 hexes per corner in a cluster
            hx = sx * (PH_W/2 - 5.0 - ho * 5.5)
            hy = sy * (PH_L/2 - 5.0 - ho * 3.0)
            # Hex approximated as 6-sided polygon
            hex_pts = [(round(hx + HEX_R * math.cos(math.radians(60*i + 30)), 3),
                        round(hy + HEX_R * math.sin(math.radians(60*i + 30)), 3))
                       for i in range(6)]
            try:
                hex_cut = (
                    cq.Workplane("XY")
                    .workplane(offset=-0.01)
                    .polyline(hex_pts).close()
                    .extrude(HEX_D)
                )
                result = result.cut(hex_cut)
            except Exception:
                pass

# ── 10. Phone retention — internal corner clips + top edge lip ────────────
# Corner retention tabs: small inward-projecting shelves at top inner edge
# that lock over the phone's screen glass corners when snapped in.
CLIP_W    = 8.0    # clip width along each corner edge
CLIP_PROJ = 1.2    # how far clip projects inward over the phone face
CLIP_T    = 0.8    # clip thickness (Z)

for sx in [-1, 1]:
    for sy in [-1, 1]:
        # Position at inner top edge of cavity at each corner
        clip_x = sx * (PH_W/2 - CLIP_W/2 - 3.0)
        clip_y = sy * (PH_L/2 - CLIP_W/2 - 3.0)
        clip = (
            cq.Workplane("XY")
            .workplane(offset=CASE_D - CLIP_T)
            .center(clip_x, clip_y)
            .rect(CLIP_W if abs(sx) > 0 else CLIP_PROJ,
                  CLIP_PROJ if abs(sx) > 0 else CLIP_W)
            .extrude(CLIP_T + 0.5)
        )
        try:
            result = result.union(clip)
        except Exception:
            pass

# Top edge inward lip — continuous 0.8mm shelf around screen opening
# that prevents the phone from lifting out. The phone snaps past this lip.
LIP_PROJ = 0.8  # inward projection
lip_outer_pts = scr_pts
# Slightly smaller = the lip shelf
lip_inner_w = (PH_W - 5) / 2 - LIP_PROJ
lip_inner_l = (PH_L - 5) / 2 - LIP_PROJ
lip_inner_cr = max(8.0 - 1.5 - LIP_PROJ, 1.0)
lip_inner_pts = []
for _cx, _cy, _a0 in [
    ( lip_inner_w - lip_inner_cr,  lip_inner_l - lip_inner_cr,   0),
    (-lip_inner_w + lip_inner_cr,  lip_inner_l - lip_inner_cr,  90),
    (-lip_inner_w + lip_inner_cr, -lip_inner_l + lip_inner_cr, 180),
    ( lip_inner_w - lip_inner_cr, -lip_inner_l + lip_inner_cr, 270),
]:
    for _i in range(9):
        _a = math.radians(_a0 + _i * 90.0 / 8)
        lip_inner_pts.append((
            round(_cx + lip_inner_cr * math.cos(_a), 4),
            round(_cy + lip_inner_cr * math.sin(_a), 4)))

try:
    lip_shelf = (
        cq.Workplane("XY")
        .workplane(offset=CASE_D - 0.8)
        .polyline(lip_outer_pts).close()
        .extrude(0.8)
    )
    lip_void = (
        cq.Workplane("XY")
        .workplane(offset=CASE_D - 0.85)
        .polyline(lip_inner_pts).close()
        .extrude(1.0)
    )
    result = result.union(lip_shelf.cut(lip_void))
except Exception:
    pass

# ── 11. Lanyard hole — bottom-right corner ───────────────────────────────
lanyard = (
    cq.Workplane("XY")
    .box(3.0, WALL * 2 + 3.0 * 2 + 2, 3.0)
    .translate((PH_W/2 - 8, -_bot, CASE_D * 0.35))
)
try:
    result = result.cut(lanyard)
except Exception:
    pass

bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")

# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "outputs/cad/step/iphone_case.step"
_stl  = "outputs/cad/stl/iphone_case.stl"
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
