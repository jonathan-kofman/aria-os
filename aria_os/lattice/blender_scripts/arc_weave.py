import bpy
import bmesh
import math
import json
import sys
from pathlib import Path


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def create_tube_along_arc(
    cx,
    cy,
    start_angle,
    end_angle,
    arc_radius,
    tube_radius,
    center_z,
    n_arc=24,
    n_tube=8,
):
    """
    Create a tube (pipe) swept along a quarter-circle arc.
    Centerline follows the arc; cross-section is a circle.
    """
    bm = bmesh.new()
    angle_span = end_angle - start_angle

    all_rings: list[list[bmesh.types.BMVert]] = []

    for i in range(n_arc + 1):
        t = i / n_arc
        a_center = math.radians(start_angle + t * angle_span)

        px = cx + arc_radius * math.cos(a_center)
        py = cy + arc_radius * math.sin(a_center)

        tx = -math.sin(a_center)
        ty = math.cos(a_center)
        tz = 0.0

        nx, ny, nz = 0.0, 0.0, 1.0

        bx = ty * nz - tz * ny
        by = tz * nx - tx * nz
        bz = tx * ny - ty * nx

        ring: list[bmesh.types.BMVert] = []
        for j in range(n_tube):
            angle_tube = 2.0 * math.pi * j / n_tube
            ox = (nx * math.cos(angle_tube) + bx * math.sin(angle_tube)) * tube_radius
            oy = (ny * math.cos(angle_tube) + by * math.sin(angle_tube)) * tube_radius
            oz = (nz * math.cos(angle_tube) + bz * math.sin(angle_tube)) * tube_radius

            v = bm.verts.new((px + ox, py + oy, center_z + oz))
            ring.append(v)

        all_rings.append(ring)

    bm.verts.ensure_lookup_table()

    for i in range(n_arc):
        ring_a = all_rings[i]
        ring_b = all_rings[i + 1]
        for j in range(n_tube):
            j_next = (j + 1) % n_tube
            bm.faces.new(
                [
                    ring_a[j],
                    ring_a[j_next],
                    ring_b[j_next],
                    ring_b[j],
                ]
            )

    bm.faces.new(all_rings[0])
    bm.faces.new(list(reversed(all_rings[-1])))

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def run(params_path):
    with open(params_path, encoding="utf-8") as f:
        p = json.load(f)

    clear_scene()

    W = p["width"]
    H = p["height"]
    D = p["depth"]
    cell = p["cell_size"]
    strut_d = p["strut_diameter"]
    frame = p["frame_thickness"]
    form = p["form"]
    skin = p.get("skin_thickness", 2.0)
    interlaced = bool(p.get("interlaced", False))
    weave_offset_req = float(p.get("weave_offset_mm", 0.0))

    tube_r = strut_d / 2.0
    arc_r = cell / 2.0

    if form == "skin_core":
        inner_W = W - 2.0 * skin
        inner_H = H - 2.0 * skin
    else:
        inner_W = W - 2.0 * frame
        inner_H = H - 2.0 * frame

    cols = max(1, int(inner_W / cell))
    rows = max(1, int(inner_H / cell))
    cell_w = inner_W / cols
    cell_h = inner_H / rows
    cell_actual = min(cell_w, cell_h)
    arc_r = cell_actual / 2.0
    z_mid = D / 2.0
    max_safe_offset = max(0.0, (D / 2.0) - (tube_r * 1.05))
    if interlaced:
        weave_offset = weave_offset_req if weave_offset_req > 0.0 else min(cell_actual * 0.12, max_safe_offset)
        weave_offset = min(weave_offset, max_safe_offset)
    else:
        weave_offset = 0.0

    all_objects = []

    for col in range(cols):
        for row in range(rows):
            ox = -inner_W / 2.0 + col * cell_actual
            oy = -inner_H / 2.0 + row * cell_actual

            configs = [
                (ox, oy, 0.0, 90.0),
                (ox + cell_actual, oy, 90.0, 180.0),
                (ox, oy + cell_actual, 270.0, 360.0),
                (ox + cell_actual, oy + cell_actual, 180.0, 270.0),
            ]

            for cx, cy, sa, ea in configs:
                family = 0 if sa in (0.0, 180.0) else 1
                if interlaced:
                    parity = (col + row) % 2
                    sign = 1.0 if ((family == 0) ^ (parity == 1)) else -1.0
                    center_z = z_mid + sign * weave_offset
                else:
                    center_z = z_mid
                bm = create_tube_along_arc(
                    cx,
                    cy,
                    sa,
                    ea,
                    arc_r,
                    tube_r,
                    center_z=center_z,
                    n_arc=20,
                    n_tube=6,
                )
                mesh_name = f"arc_{col}_{row}_{sa}"
                mesh = bpy.data.meshes.new(mesh_name)
                bm.to_mesh(mesh)
                bm.free()
                obj = bpy.data.objects.new(mesh_name, mesh)
                bpy.context.collection.objects.link(obj)
                all_objects.append(obj)

    if not all_objects:
        print("ERROR: No arc objects created")
        return

    hw = W / 2.0
    hh = H / 2.0
    iw = inner_W / 2.0
    ih = inner_H / 2.0

    border_pieces = [
        (-hw, hw, -hh, -ih),
        (-hw, hw, ih, hh),
        (-hw, -iw, -ih, ih),
        (iw, hw, -ih, ih),
    ]

    for x1, x2, y1, y2 in border_pieces:
        bm_b = bmesh.new()
        verts = [
            bm_b.verts.new((x1, y1, 0.0)),
            bm_b.verts.new((x2, y1, 0.0)),
            bm_b.verts.new((x2, y2, 0.0)),
            bm_b.verts.new((x1, y2, 0.0)),
            bm_b.verts.new((x1, y1, D)),
            bm_b.verts.new((x2, y1, D)),
            bm_b.verts.new((x2, y2, D)),
            bm_b.verts.new((x1, y2, D)),
        ]
        faces = [
            [0, 1, 2, 3],
            [7, 6, 5, 4],
            [0, 4, 5, 1],
            [1, 5, 6, 2],
            [2, 6, 7, 3],
            [3, 7, 4, 0],
        ]
        for f in faces:
            bm_b.faces.new([verts[i] for i in f])
        bmesh.ops.recalc_face_normals(bm_b, faces=bm_b.faces)
        border_mesh = bpy.data.meshes.new("border")
        bm_b.to_mesh(border_mesh)
        bm_b.free()
        border_obj = bpy.data.objects.new("border", border_mesh)
        bpy.context.collection.objects.link(border_obj)
        all_objects.append(border_obj)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in all_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = all_objects[0]
    bpy.ops.object.join()
    final_obj = bpy.context.object

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.remove_doubles(threshold=0.001)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")

    final_obj.scale = (0.001, 0.001, 0.001)
    bpy.ops.object.transform_apply(scale=True)

    output_stl = p["output_stl"]
    Path(output_stl).parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.object.select_all(action="DESELECT")
    final_obj.select_set(True)
    bpy.ops.wm.stl_export(
        filepath=output_stl,
        export_selected_objects=True,
        global_scale=1000.0,
        apply_modifiers=True,
        ascii_format=False,
        use_scene_unit=False,
    )

    cell_count = cols * rows
    print(f"SUCCESS: Exported arc weave to {output_stl}")
    print(f"  Grid: {cols}x{rows} = {cell_count} cells")
    print(f"  Arcs: {cell_count * 4} tubes")
    if interlaced:
        print(f"  Interlaced: ON (weave_offset={weave_offset:.3f}mm)")


argv = sys.argv
try:
    if "--" in argv:
        run(argv[argv.index("--") + 1])
    else:
        print("ERROR: No params file provided")
    print("SCRIPT_COMPLETE")
except Exception as e:
    import traceback

    print(f"SCRIPT_ERROR: {e}")
    traceback.print_exc()

