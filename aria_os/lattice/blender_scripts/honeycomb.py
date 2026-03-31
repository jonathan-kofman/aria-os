import bpy
import bmesh
import math
import json
import sys
from pathlib import Path


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def create_hex_void(bm, cx, cy, r_inner, depth):
    """Cut a hexagonal void at position cx, cy."""
    verts_bot = []
    verts_top = []
    for i in range(6):
        a = math.radians(60 * i + 30)
        x = cx + r_inner * math.cos(a)
        y = cy + r_inner * math.sin(a)
        verts_bot.append(bm.verts.new((x, y, 0.0)))
        verts_top.append(bm.verts.new((x, y, depth)))

    bm.verts.ensure_lookup_table()

    bm.faces.new(verts_bot)
    bm.faces.new(list(reversed(verts_top)))

    for i in range(6):
        bm.faces.new(
            [
                verts_bot[i],
                verts_bot[(i + 1) % 6],
                verts_top[(i + 1) % 6],
                verts_top[i],
            ]
        )


def run(params_path: str):
    from mathutils import Matrix

    with open(params_path, encoding="utf-8") as f:
        p = json.load(f)

    clear_scene()

    W = float(p["width"])
    H = float(p["height"])
    D = float(p["depth"])
    cell = float(p["cell_size"])
    wall = float(p["strut_diameter"])
    frame = float(p["frame_thickness"])
    form = p.get("form", "volumetric")
    skin = float(p.get("skin_thickness", 2.0))

    r_outer = cell / 2.0
    r_inner = r_outer - wall
    hex_w = math.sqrt(3.0) * r_outer
    hex_h = 2.0 * r_outer
    col_step = hex_w
    row_step = hex_h * 0.75

    if form == "skin_core":
        inner_W = W - 2.0 * skin
        inner_H = H - 2.0 * skin
        core_D = D - 2.0 * skin
    else:
        inner_W = W - 2.0 * frame
        inner_H = H - 2.0 * frame
        core_D = D

    cols = int(inner_W / col_step) + 2
    rows = int(inner_H / row_step) + 2

    # Build solid panel as a scaled cube
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(
        bm,
        vec=(W / 2.0, H / 2.0, D / 2.0),
        space=Matrix.Identity(4),
        verts=bm.verts,
    )
    mesh = bpy.data.meshes.new("panel")
    bm.to_mesh(mesh)
    bm.free()

    panel = bpy.data.objects.new("panel", mesh)
    bpy.context.collection.objects.link(panel)
    bpy.context.view_layer.objects.active = panel

    # Build one combined hex cutter mesh
    bm_cut = bmesh.new()

    for row in range(rows):
        for col in range(cols):
            x = -inner_W / 2.0 + col * col_step
            if row % 2 == 1:
                x += col_step / 2.0
            y = -inner_H / 2.0 + row * row_step

            if r_inner <= 0.0:
                continue

            bot = []
            top = []
            for i in range(6):
                a = math.radians(60 * i + 30)
                px = x + r_inner * math.cos(a)
                py = y + r_inner * math.sin(a)
                bot.append(bm_cut.verts.new((px, py, -1.0)))
                top.append(bm_cut.verts.new((px, py, D + 1.0)))

            bm_cut.verts.ensure_lookup_table()

            bm_cut.faces.new(list(reversed(bot)))
            bm_cut.faces.new(top)
            for i in range(6):
                bm_cut.faces.new(
                    [
                        bot[i],
                        top[i],
                        top[(i + 1) % 6],
                        bot[(i + 1) % 6],
                    ]
                )

    bmesh.ops.recalc_face_normals(bm_cut, faces=bm_cut.faces)

    cut_mesh = bpy.data.meshes.new("cutter")
    bm_cut.to_mesh(cut_mesh)
    bm_cut.free()

    cutter = bpy.data.objects.new("cutter", cut_mesh)
    bpy.context.collection.objects.link(cutter)

    mod = panel.modifiers.new(name="hexcut", type="BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.object = cutter
    mod.solver = "MANIFOLD"

    bpy.context.view_layer.objects.active = panel
    bpy.ops.object.modifier_apply(modifier="hexcut")

    bpy.data.objects.remove(cutter, do_unlink=True)

    # Export
    bpy.ops.object.select_all(action="DESELECT")
    panel.select_set(True)
    panel.scale = (0.001, 0.001, 0.001)
    bpy.ops.object.transform_apply(scale=True)

    output_stl = p["output_stl"]
    Path(output_stl).parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.stl_export(
        filepath=output_stl,
        export_selected_objects=True,
        global_scale=1000.0,
        apply_modifiers=True,
        ascii_format=False,
        use_scene_unit=False,
    )

    print(f"SUCCESS: Exported honeycomb to {output_stl}")


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

