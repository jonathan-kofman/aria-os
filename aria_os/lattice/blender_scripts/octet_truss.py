import bpy
import bmesh
import math
import json
import sys
from pathlib import Path


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def make_cylinder_between(bm, p1, p2, radius, n=12):
    """
    Add a cylinder between two 3D points to an existing bmesh.
    """
    import mathutils

    v1 = mathutils.Vector(p1)
    v2 = mathutils.Vector(p2)
    vec = v2 - v1
    length = vec.length
    if length < 0.001:
        return

    vec_n = vec.normalized()
    if abs(vec_n.x) < 0.9:
        perp = vec_n.cross(mathutils.Vector((1.0, 0.0, 0.0)))
    else:
        perp = vec_n.cross(mathutils.Vector((0.0, 1.0, 0.0)))
    perp = perp.normalized()
    perp2 = vec_n.cross(perp)

    bot_verts = []
    top_verts = []
    for i in range(n):
        angle = 2.0 * math.pi * i / n
        offset = (perp * math.cos(angle) + perp2 * math.sin(angle)) * radius
        bot_verts.append(bm.verts.new((v1 + offset).to_tuple()))
        top_verts.append(bm.verts.new((v2 + offset).to_tuple()))

    bm.verts.ensure_lookup_table()

    bm.faces.new(list(reversed(bot_verts)))
    bm.faces.new(top_verts)
    for i in range(n):
        bm.faces.new(
            [
                bot_verts[i],
                bot_verts[(i + 1) % n],
                top_verts[(i + 1) % n],
                top_verts[i],
            ]
        )


def run(params_path: str):
    with open(params_path, encoding="utf-8") as f:
        p = json.load(f)

    clear_scene()

    W = float(p["width"])
    H = float(p["height"])
    D = float(p["depth"])
    cell = float(p["cell_size"])
    strut_r = float(p["strut_diameter"]) / 2.0
    node_r = strut_r * 1.5

    cols = max(1, int(W / cell))
    rows = max(1, int(H / cell))
    layers = max(1, int(D / cell))

    bm = bmesh.new()
    total_struts = 0

    for ci in range(cols):
        for ri in range(rows):
            for li in range(layers):
                ox = ci * cell
                oy = ri * cell
                oz = li * cell
                s = cell
                h = cell / 2.0

                corners = [
                    (ox, oy, oz),
                    (ox + s, oy, oz),
                    (ox, oy + s, oz),
                    (ox + s, oy + s, oz),
                    (ox, oy, oz + s),
                    (ox + s, oy, oz + s),
                    (ox, oy + s, oz + s),
                    (ox + s, oy + s, oz + s),
                ]
                faces_c = [
                    (ox + h, oy + h, oz),
                    (ox + h, oy + h, oz + s),
                    (ox + h, oy, oz + h),
                    (ox + h, oy + s, oz + h),
                    (ox, oy + h, oz + h),
                    (ox + s, oy + h, oz + h),
                ]

                connectivity = [
                    (0, [0, 1, 2, 3]),
                    (1, [4, 5, 6, 7]),
                    (2, [0, 1, 4, 5]),
                    (3, [2, 3, 6, 7]),
                    (4, [0, 2, 4, 6]),
                    (5, [1, 3, 5, 7]),
                ]

                for face_idx, corner_indices in connectivity:
                    for ci2 in corner_indices:
                        make_cylinder_between(
                            bm,
                            faces_c[face_idx],
                            corners[ci2],
                            strut_r,
                            n=8,
                        )
                        total_struts += 1

                for corner in corners:
                    for axis_offset in [
                        ((node_r, 0.0, 0.0), (-node_r, 0.0, 0.0)),
                        ((0.0, node_r, 0.0), (0.0, -node_r, 0.0)),
                        ((0.0, 0.0, node_r), (0.0, 0.0, -node_r)),
                    ]:
                        p1 = tuple(corner[i] + axis_offset[0][i] for i in range(3))
                        p2 = tuple(corner[i] + axis_offset[1][i] for i in range(3))
                        make_cylinder_between(bm, p1, p2, node_r, n=8)

    print(f"Built {total_struts} struts")

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.001)

    mesh = bpy.data.meshes.new("octet_truss")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new("octet_truss", mesh)
    bpy.context.collection.objects.link(obj)

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.remove_doubles(threshold=0.01)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")

    obj.scale = (0.001, 0.001, 0.001)
    bpy.ops.object.transform_apply(scale=True)

    output_stl = p["output_stl"]
    Path(output_stl).parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.wm.stl_export(
        filepath=output_stl,
        export_selected_objects=True,
        global_scale=1000.0,
        apply_modifiers=True,
        ascii_format=False,
        use_scene_unit=False,
    )

    print(f"SUCCESS: Exported octet truss to {output_stl}")
    print(f"  Grid: {cols}x{rows}x{layers} cells")
    print(f"  Total struts: {total_struts}")


if __name__ == "__main__":
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

