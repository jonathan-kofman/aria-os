"""
aria_os/visual_verifier.py

Visual verification of generated CAD parts using vision AI.

Renders 3 views of the STL (top, front, isometric) via matplotlib (headless),
then sends them to a vision LLM with a feature checklist derived from the
goal string and spec dict.  Returns a structured verification result.

Priority: Gemini 2.5 Flash → Groq llama-4-scout → Ollama gemma4:e4b → Anthropic Claude → skip.

Dependencies: trimesh, matplotlib (both already in requirements_aria_os.txt).
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Rendering helpers (matplotlib Agg — works headless on Windows)
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert goal text to a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] if slug else "part"


def _append_section_view(
    mesh: "trimesh.Trimesh",
    slug: str,
    out_dir: "Path",
    short_goal: str,
    paths: list,
    labels: list,
) -> None:
    """Render a matplotlib cross-section and append it to paths/labels in-place.

    Uses mesh.section() (no shapely required) to compute the cross-section at
    Y=centroid, then fills closed polygon loops: outer boundary = solid, inner
    loops = void.  This reveals hollow interiors, bore diameter, wall thickness,
    and internal features that orthographic projections collapse away.
    """
    import numpy as np
    import warnings
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            section = mesh.section(
                plane_origin=mesh.centroid,
                plane_normal=[0, 1, 0],   # cut along Y, view XZ profile
            )
        if section is None:
            return

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            # to_2D is the modern API; to_planar is the old one — try both
            try:
                sec2d, _xform = section.to_2D()
            except AttributeError:
                sec2d, _xform = section.to_planar()

        # Collect closed polygon loops from section entities
        polys: list = []
        for entity in sec2d.entities:
            pts = sec2d.vertices[entity.points]
            if len(pts) < 3:
                continue
            if not np.allclose(pts[0], pts[-1], atol=1e-4):
                pts = np.vstack([pts, pts[0]])
            polys.append(pts)

        if not polys:
            return

        # Sort by enclosed area: outer boundary is largest
        def _area(pts: np.ndarray) -> float:
            x, y = pts[:-1, 0], pts[:-1, 1]
            return abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))) / 2

        polys_sorted = sorted(polys, key=_area, reverse=True)

        extents = np.ptp(mesh.bounds, axis=0)
        cs_dim = f"X: {extents[0]:.1f}mm  |  Z: {extents[2]:.1f}mm"

        fig_cs, ax_cs = plt.subplots(figsize=(8, 6))
        fig_cs.patch.set_facecolor("#111111")
        ax_cs.set_facecolor("#111111")

        for i, poly in enumerate(polys_sorted):
            # Outer boundary = solid material; inner loops = holes/voids
            fill_c = "#4a6080" if i == 0 else "#111111"
            ax_cs.fill(poly[:, 0], poly[:, 1],
                       color=fill_c, zorder=i + 2, alpha=0.95)
            ax_cs.plot(poly[:, 0], poly[:, 1],
                       color="#88aacc", lw=0.9, zorder=len(polys_sorted) + 2)

        ax_cs.autoscale()
        ax_cs.set_aspect("equal")
        ax_cs.set_title(
            f"Cross-section (cut at Y={mesh.centroid[1]:.1f}mm) — {short_goal}\n[{cs_dim}]",
            color="white", fontsize=8,
        )
        ax_cs.set_xlabel("X (mm)", color="#aaaaaa")
        ax_cs.set_ylabel("Z (mm)", color="#aaaaaa")
        ax_cs.tick_params(colors="#888888")
        for sp in ax_cs.spines.values():
            sp.set_edgecolor("#333333")

        cs_path = str(out_dir / f"verify_{slug}_section.png")
        plt.savefig(cs_path, dpi=150, bbox_inches="tight",
                    facecolor=fig_cs.get_facecolor())
        plt.close(fig_cs)
        paths.append(cs_path)
        labels.append(
            "Cross-section (part cut at Y=centroid plane — XZ profile showing "
            "hollow interior, bore diameter, wall thickness, and internal features)"
        )
        print(f"[VISUAL] cross-section view saved: {cs_path}")
    except Exception as _exc:
        print(f"[VISUAL] cross-section view failed: {_exc}")


def _render_views(stl_path: str, goal: str, out_dir: Path) -> tuple[list[str], list[str]]:
    """Render orthographic views of an STL using trimesh's GL renderer (proper depth sorting).

    Falls back to matplotlib wireframe if GL is unavailable (always the case on headless Windows).

    Returns (paths, view_labels) — one label per path describing exactly what each image shows.
    The labels are passed to the vision prompt so the model cannot hallucinate extra views.
    """
    import numpy as np
    import trimesh

    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

    # Smooth face normals for scan/marching-cubes meshes — noisy normals make
    # shading appear salt-and-pepper dark. Vertex normals are area-weighted
    # averages across adjacent faces, giving much cleaner shading.
    try:
        if hasattr(mesh, "vertex_normals") and mesh.vertex_normals is not None:
            # Replace face normals with per-face average of vertex normals
            # (effectively smooth shading from the existing connectivity)
            vn = mesh.vertex_normals[mesh.faces]  # (F, 3, 3)
            smooth_fn = vn.mean(axis=1)           # (F, 3)
            norms = np.linalg.norm(smooth_fn, axis=1, keepdims=True)
            mask = norms[:, 0] > 1e-8
            smooth_fn[mask] /= norms[mask]
            smooth_fn[~mask] = mesh.face_normals[~mask]
            mesh._cache.clear()
            mesh.face_normals = smooth_fn
    except Exception:
        pass  # keep raw face normals on failure

    slug = _slugify(goal)
    short_goal = goal[:50]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    labels: list[str] = []

    # Use max bounding-box extent (not sphere diagonal) for distance — avoids
    # GL far-plane clipping on large meshes where mesh.scale >> largest face.
    extents = np.ptp(mesh.bounds, axis=0)           # [dx, dy, dz]
    distance = float(extents.max()) * 2.5           # camera 2.5× largest dim away
    center = mesh.centroid

    _GL_VIEW_LABELS = {
        "top":   "Top projection (XY plane — orthographic, looking straight down from above)",
        "front": "Front projection (XZ plane — orthographic, looking from front)",
        "iso":   "Isometric 3D view (perspective, looking from front-right-top corner)",
    }

    # Camera angles: (name, angles_tuple) for scene.set_camera
    views = [
        ("top", (np.pi, 0, 0)),
        ("front", (np.pi / 2, 0, 0)),
        ("iso", (np.pi / 3, 0, np.pi / 4)),
    ]

    # Set aluminum/steel material so GL renders are light silver-gray, not dark charcoal.
    # STL meshes have no embedded color; trimesh default is a very dark gray (~#646464).
    # We want ~#AFB9C8: light matte aluminum — matches SolidWorks/Fusion default appearance.
    try:
        alum = np.array([175, 183, 198, 255], dtype=np.uint8)
        mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)
    except Exception:
        pass

    # Try GL renderer first (proper shaded 3D render)
    gl_works = False
    for name, angles in views:
        view_path = str(out_dir / f"verify_{slug}_{name}.png")
        try:
            scene = mesh.scene()
            scene.set_camera(angles=angles, distance=distance, center=center)
            # visible=True required for pyglet-based rendering on Windows
            data = scene.save_image(resolution=(800, 600), visible=True)
            if data and len(data) > 1000:
                with open(view_path, "wb") as f:
                    f.write(data)
                paths.append(view_path)
                labels.append(_GL_VIEW_LABELS[name])
                gl_works = True
            else:
                break
        except Exception:
            break

    if gl_works and len(paths) >= 3:
        # GL rendered top/front/iso — still add a matplotlib cross-section
        # (slice_mesh_plane needs shapely which is often absent; mesh.section() always works)
        _append_section_view(mesh, slug, out_dir, short_goal, paths, labels)
        return paths, labels

    # Fallback: matplotlib solid-shaded projection views (headless, no OpenGL)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection

    paths = []
    labels = []

    # Pre-compute mesh arrays once
    face_verts   = mesh.vertices[mesh.faces]   # (F, 3, 3)
    face_normals = mesh.face_normals            # (F, 3)

    # Subsample very dense meshes to keep renders fast
    MAX_FACES = 60_000
    if len(face_verts) > MAX_FACES:
        idx = np.linspace(0, len(face_verts) - 1, MAX_FACES, dtype=int)
        face_verts   = face_verts[idx]
        face_normals = face_normals[idx]

    # (name, depth_axis, proj_axes, cam_dir, key_light_dir, xlabel, ylabel, label)
    # cam_dir: unit vector from object toward camera; used for back-face culling.
    # dot(face_normal, cam_dir) < 0  => back-face, skip it.
    view_defs = [
        ("top",   2, [0, 1], [0,  0,  1], [0.4,  0.4,  1.0],  "X (mm)", "Y (mm)",
         "Top projection (XY plane — solid shaded, looking straight down from above)"),
        ("front", 1, [0, 2], [0,  1,  0], [0.4,  1.0,  0.6],  "X (mm)", "Z (mm)",
         "Front projection (XZ plane — solid shaded, looking from front)"),
        ("side",  0, [1, 2], [1,  0,  0], [1.0,  0.3,  0.6],  "Y (mm)", "Z (mm)",
         "Side projection (YZ plane — solid shaded, looking from right side)"),
    ]

    AMBIENT       = 0.22
    FILL_STRENGTH = 0.18

    # Pre-compute per-axis bounds for dimension labels embedded in each render title.
    # Vision models read title text reliably — this tells them the actual mm size of
    # each projected dimension so they can compare to spec rather than guessing.
    _ax_label = {0: "X", 1: "Y", 2: "Z"}
    _bds  = {i: (float(face_verts[:, :, i].min()), float(face_verts[:, :, i].max()))
             for i in range(3)}
    _dims = {i: _bds[i][1] - _bds[i][0] for i in range(3)}

    for name, depth_ax, proj_ax, cam_dir, light_dir, xl, yl, view_label in view_defs:
        view_path = str(out_dir / f"verify_{slug}_{name}.png")

        # Back-face culling: skip faces pointing away from camera.
        # dot(face_normal, cam_dir) > 0 => front-facing (visible).
        # This prevents back-faces from incorrectly painting over front-faces
        # on concave geometry (bores, pockets, hollow parts).
        cam_v = np.asarray(cam_dir, dtype=float)
        facing = face_normals @ cam_v  # (F,) — positive = front-facing
        fv_vis = face_verts[facing > -0.05]   # small threshold, keep near-90° faces
        fn_vis = face_normals[facing > -0.05]

        # Painter's algorithm: sort back-to-front along depth axis (ascending = far first)
        depths = fv_vis[:, :, depth_ax].mean(axis=1)
        order  = np.argsort(depths)   # ascending: far faces first, close faces last (on top)
        fv     = fv_vis[order]
        fn     = fn_vis[order]

        # Two-light Phong-style diffuse
        L1 = np.asarray(light_dir, dtype=float)
        L1 /= np.linalg.norm(L1)
        L2 = np.array([-L1[0] * 0.6, -L1[1] * 0.6, L1[2] * 0.5])
        n2 = np.linalg.norm(L2)
        if n2 > 1e-6:
            L2 /= n2

        d1 = np.clip(fn @ L1, 0, 1) * 0.65
        d2 = np.clip(fn @ L2, 0, 1) * FILL_STRENGTH
        intensity = AMBIENT + d1 + d2  # (F,) in ~[0, 1]

        # Steel-blue palette
        r = np.clip(intensity * 0.55, 0, 1)
        g = np.clip(intensity * 0.65, 0, 1)
        b = np.clip(intensity * 0.82, 0, 1)
        face_colors = np.column_stack([r, g, b, np.ones(len(intensity))])

        # 2-D polygon coordinates
        a0, a1 = proj_ax
        polys_2d = fv[:, :, [a0, a1]]  # (F, 3, 2)

        fig, ax = plt.subplots(figsize=(8, 6))
        fig.patch.set_facecolor("#111111")
        ax.set_facecolor("#111111")

        # Solid faces
        pc = PolyCollection(polys_2d, facecolors=face_colors,
                            edgecolors="none", linewidths=0)
        ax.add_collection(pc)

        # Thin silhouette edges for feature clarity
        sil_c     = np.clip(face_colors[:, :3] * 1.3, 0, 1)
        sil_alpha = np.where(intensity > 0.4, 0.25, 0.0)
        pc_e = PolyCollection(polys_2d, facecolors="none",
                              edgecolors=np.column_stack([sil_c, sil_alpha]),
                              linewidths=0.2)
        ax.add_collection(pc_e)

        ax.autoscale()
        ax.set_aspect("equal")
        # Embed actual mm dimensions in title — vision model reads this to check proportions
        dim_str = f"{_ax_label[a0]}: {_dims[a0]:.1f}mm  |  {_ax_label[a1]}: {_dims[a1]:.1f}mm"
        ax.set_title(f"{name.title()} — {short_goal}\n[{dim_str}]",
                     color="white", fontsize=8)
        ax.set_xlabel(xl, color="#aaaaaa")
        ax.set_ylabel(yl, color="#aaaaaa")
        ax.tick_params(colors="#888888")
        for spine in ax.spines.values():
            spine.set_edgecolor("#333333")

        plt.savefig(view_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        paths.append(view_path)
        labels.append(view_label)

    # Isometric view — standard Rx(-35.26deg) @ Ry(45deg) rotation.
    # Orthographic projections collapse depth so a hollow shell looks solid, a bore
    # looks like a flat disc, etc. Isometric reveals all three faces simultaneously
    # and makes 3D structure legible to vision models.
    try:
        iso_path = str(out_dir / f"verify_{slug}_iso.png")
        cy, sy = np.cos(np.radians(45)),    np.sin(np.radians(45))
        cx, sx = np.cos(np.radians(-35.264)), np.sin(np.radians(-35.264))
        Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
        R  = Rx @ Ry

        pts_rot = face_verts.reshape(-1, 3) @ R.T
        fv_iso  = pts_rot.reshape(-1, 3, 3)
        fn_iso  = face_normals @ R.T

        dep_iso = fv_iso[:, :, 2].mean(axis=1)
        ord_iso = np.argsort(-dep_iso)
        fv_iso  = fv_iso[ord_iso]
        fn_iso  = fn_iso[ord_iso]

        L_i  = np.array([0.35, 0.80, 0.55]); L_i  /= np.linalg.norm(L_i)
        L2_i = np.array([-0.50, 0.30, 0.45]); L2_i /= np.linalg.norm(L2_i)
        d1_i  = np.clip(fn_iso @ L_i,  0, 1) * 0.65
        d2_i  = np.clip(fn_iso @ L2_i, 0, 1) * FILL_STRENGTH
        int_i = AMBIENT + d1_i + d2_i

        ri = np.clip(int_i * 0.55, 0, 1)
        gi = np.clip(int_i * 0.65, 0, 1)
        bi = np.clip(int_i * 0.82, 0, 1)
        fc_i   = np.column_stack([ri, gi, bi, np.ones(len(int_i))])
        sil_ci = np.clip(fc_i[:, :3] * 1.3, 0, 1)
        sil_ai = np.where(int_i > 0.4, 0.25, 0.0)

        fig_i, ax_i = plt.subplots(figsize=(8, 6))
        fig_i.patch.set_facecolor("#111111")
        ax_i.set_facecolor("#111111")
        ax_i.add_collection(PolyCollection(fv_iso[:, :, :2], facecolors=fc_i,
                                           edgecolors="none", linewidths=0))
        ax_i.add_collection(PolyCollection(fv_iso[:, :, :2], facecolors="none",
                                           edgecolors=np.column_stack([sil_ci, sil_ai]),
                                           linewidths=0.2))
        ax_i.autoscale()
        ax_i.set_aspect("equal")
        full_dim = f"X:{_dims[0]:.1f} x Y:{_dims[1]:.1f} x Z:{_dims[2]:.1f} mm"
        ax_i.set_title(f"Isometric — {short_goal}\n[{full_dim}]", color="white", fontsize=8)
        ax_i.set_xlabel("ISO-X", color="#aaaaaa")
        ax_i.set_ylabel("ISO-Y", color="#aaaaaa")
        ax_i.tick_params(colors="#888888")
        for sp in ax_i.spines.values():
            sp.set_edgecolor("#333333")
        plt.savefig(iso_path, dpi=150, bbox_inches="tight",
                    facecolor=fig_i.get_facecolor())
        plt.close(fig_i)
        paths.append(iso_path)
        labels.append(
            "Isometric 3D view (Rx(-35deg) @ Ry(45deg) projection — all three faces "
            "visible simultaneously, revealing depth, internal features, and true 3D shape)"
        )
    except Exception as _iso_exc:
        print(f"[VISUAL] isometric view failed: {_iso_exc}")

    # Cross-section view (shared helper — works in both GL and matplotlib paths)
    _append_section_view(mesh, slug, out_dir, short_goal, paths, labels)

    return paths, labels


# ---------------------------------------------------------------------------
# Feature checklist builder
# ---------------------------------------------------------------------------

# keyword -> (description pattern, view hint)
_FEATURE_KEYWORDS: list[tuple[str, str, str]] = [
    ("bore",         "large center hole / bore opening visible",              "top projection"),
    ("hole",         "circular hole(s) visible",                              "top projection"),
    ("fin",          "parallel fin-like protrusions visible",                 "front or side projection"),
    ("heat sink",    "heat-sink body with parallel fins visible",             "front or side projection"),
    ("l-bracket",    "L-shaped profile visible (two plates at ~90 degrees)",  "front projection"),
    ("l bracket",    "L-shaped profile visible (two plates at ~90 degrees)",  "front projection"),
    ("angle bracket","angled profile visible",                                "front projection"),
    ("shell",        "visible wall thickness indicating hollow/shell body",   "front or side projection"),
    ("hollow",       "visible wall thickness indicating hollow interior",     "front or side projection"),
    ("sweep",        "curved swept profile visible",                          "top or front projection"),
    ("curve",        "curved geometry visible",                               "top or front projection"),
    ("bend",         "bent/curved profile visible",                           "front projection"),
    ("thread",       "surface texture or helical thread pattern visible",     "front or side projection"),
    ("knurl",        "knurled surface texture visible",                       "front or side projection"),
    ("slot",         "rectangular slot or cutout visible",                    "top projection"),
    ("groove",       "groove/channel cut into surface visible",               "front projection"),
    ("prong",        "protruding prong/tab features visible",                 "front or side projection"),
    ("clip",         "clip/snap feature visible",                             "front or side projection"),
    ("tab",          "protruding tab feature visible",                        "front or side projection"),
    ("flange",       "flange rim/lip visible around body",                   "front projection"),
    ("rib",          "reinforcing rib(s) visible",                            "front or side projection"),
    ("chamfer",      "chamfered edge(s) visible",                             "front or side projection"),
    ("fillet",       "rounded fillet edge(s) visible",                        "front or side projection"),
    ("gear",         "gear teeth visible around circumference",               "top projection"),
    ("teeth",        "tooth features visible",                                "top projection"),
    ("ratchet",      "asymmetric ratchet teeth visible",                      "top projection"),
    ("keyway",       "keyway slot visible in bore/shaft",                     "top projection"),
    ("spline",       "spline features visible",                               "top projection"),
    ("mount",        "mounting features (holes/tabs/flanges) visible",        "top or front projection"),
    ("nozzle",       "convergent-divergent nozzle profile visible",           "front projection"),
    ("impeller",     "curved vane/blade features visible radiating from hub", "top projection"),
    ("blade",        "blade/airfoil cross-section profile visible",           "front projection"),
    ("vane",         "vane features visible radiating from center",           "top projection"),
    # ARIA-specific parts
    ("spool",        "flanged drum body visible with hub bore and two end flanges (front projection)", "front or side projection"),
    ("drum",         "cylindrical drum body visible",                          "front projection"),
    ("pulley",       "grooved sheave rim visible with central bore (top and front projection)", "top projection"),
    ("cam collar",   "collar body visible with cross-hole or flat (top projection)", "top projection"),
    ("brake drum",   "cylindrical drum with inner cavity visible (cross-section)", "cross-section"),
    ("cam",          "cam profile / eccentric shape visible",                  "top projection"),
    ("pawl",         "asymmetric lever/tooth profile visible",                 "front or side projection"),
    ("catch",        "catch / latch body visible",                             "front projection"),
    ("rope guide",   "guide channel / groove visible for rope path",           "front or side projection"),
    ("spacer",       "cylindrical spacer with center bore visible",            "top projection"),
    ("standoff",     "cylindrical standoff body with threaded bore visible",   "front or side projection"),
    ("hex standoff", "hexagonal cross-section visible",                        "top projection"),
    ("hex",          "hexagonal cross-section visible",                        "top projection"),
    # Structural shapes
    ("u-channel",    "U-shaped channel profile visible (two flanges and a web)", "front projection"),
    ("u channel",    "U-shaped channel profile visible",                       "front projection"),
    ("gusset",       "triangular gusset / corner brace profile visible",       "front projection"),
    ("weld",         "weld bead / fillet weld profile visible",                "front or side projection"),
    ("coupling",     "shaft coupling body with two bores visible",             "front or side projection"),
    ("clamp",        "clamp body with split and fastener holes visible",       "top projection"),
    # Gear types
    ("involute",     "involute tooth profile visible around circumference",    "top projection"),
    ("sprocket",     "sprocket teeth visible around circumference",            "top projection"),
    ("pinion",       "small gear/pinion teeth visible",                        "top projection"),
    # Electronics / enclosures
    ("enclosure",    "hollow box / enclosure visible with walls and opening",  "front or side projection"),
    ("phone case",   "rectangular body with screen opening and side cutouts",  "top projection"),
    ("phone",        "rectangular slab with cutouts visible",                  "top projection"),
    ("lid",          "flat lid panel visible, possibly with tabs or screws",   "top projection"),
    # Aerospace / propulsion
    ("lre",          "rocket nozzle convergent-divergent profile visible",     "front projection"),
    ("rocket",       "nozzle bell / throat profile visible",                   "front projection"),
    ("turbine",      "turbine blade cascade or disc visible",                  "front or side projection"),
    ("propeller",    "twisted blade profile visible",                          "front or side projection"),
    ("fan",          "fan blades visible radiating from hub",                  "top projection"),
    # Lattice / infill
    ("lattice",      "internal lattice / open-cell structure visible",         "cross-section"),
    ("gyroid",       "gyroid / TPMS surface pattern visible",                  "cross-section"),
    # Connectors / fasteners
    ("snap",         "snap / click-fit hook feature visible",                  "front or side projection"),
    ("snap hook",    "cantilever hook / barb feature visible",                 "front or side projection"),
    ("spring clip",  "thin spring-clip leaf visible",                          "front or side projection"),
    ("hinge",        "hinge knuckle / barrel visible",                         "front or side projection"),
    # Civil / structural
    ("bracket",      "bracket body visible with mounting surfaces",            "front projection"),
    ("angle",        "angled/corner geometry visible",                         "front projection"),
    ("plate",        "flat plate visible",                                     "top projection"),
]


def _build_checklist(goal: str, spec: dict) -> list[str]:
    """Build a visual feature checklist from goal text and extracted spec."""
    checks: list[str] = []
    goal_lower = goal.lower()

    # Keyword-based checks
    for keyword, description, view in _FEATURE_KEYWORDS:
        if keyword in goal_lower:
            checks.append(f"{description} (check {view})")

    # Repeated feature pattern: "4x holes", "8 fins", "6 holes", "3 prongs"
    # Requires the count to be directly followed by the feature (with optional x/X)
    # Excludes patterns like "M3" where the digit is part of a metric size
    nx_pattern = re.findall(r"(?<![mM])(\d+)\s*[xX×]\s*(hole|fin|bolt|prong|tab|slot|groove|rib|blade|vane|teeth|tooth|spoke|arm|leg|pin|screw)s?", goal_lower)
    # Also match "N features" without x separator: "8 fins", "24 teeth"
    # Allow optional adjectives between count and feature: "8 parallel fins"
    # Exclude metric sizes: "M5" → skip (the (?<![mM]) prevents "M5 bolt" matching)
    nx_pattern += re.findall(r"(?<![mM])(\d+)\s+(?:\w+\s+)?(hole|fin|bolt|prong|tab|slot|groove|rib|blade|vane|teeth|tooth|spoke|arm|leg|pin|screw)s?", goal_lower)
    seen_nx: set[str] = set()
    # Skip regex bolt/teeth counts when spec has exact values (spec is authoritative)
    _spec_overrides = set()
    if spec.get("n_bolts"):
        _spec_overrides.add("bolt")
        _spec_overrides.add("hole")
    if spec.get("n_teeth"):
        _spec_overrides.add("teeth")
        _spec_overrides.add("tooth")

    for count, feature in nx_pattern:
        if feature in _spec_overrides:
            continue  # spec has the correct count, skip regex guess
        key = f"{count}_{feature}"
        if key not in seen_nx:
            seen_nx.add(key)
            checks.append(f"{count} distinct {feature} features visible")

    # Spec-driven checks (authoritative counts from structured extraction)
    if spec.get("n_teeth"):
        checks.append(f"approximately {spec['n_teeth']} teeth visible around circumference (top projection)")
    if spec.get("n_bolts"):
        # Circular bolt pattern only applies to circular parts (flanges, discs, hubs).
        # Brackets, plates, and housings use linear/grid patterns — don't assert circular.
        _circular_keywords = ("flange", "pipe", "disc", "disk", "wheel", "hub", "pulley",
                               "ring", "annular", "pcd", "bolt circle", "bolt_circle")
        _is_circular_part = any(kw in goal_lower for kw in _circular_keywords)
        if _is_circular_part:
            checks.append(f"{spec['n_bolts']} bolt holes visible in a circular/PCD pattern (top projection)")
        else:
            checks.append(f"{spec['n_bolts']} bolt holes visible (top projection)")
    if spec.get("bore_mm") or spec.get("id_mm"):
        bore = spec.get("bore_mm") or spec.get("id_mm")
        checks.append(f"center bore (~{bore}mm) visible as a large circular opening (top projection)")
    if spec.get("wall_mm"):
        checks.append(f"wall thickness visible (part should appear hollow/shelled, not solid)")
    if spec.get("od_mm") and spec.get("bore_mm"):
        checks.append("part appears as a ring/annular shape (top projection)")

    # Blade/vane checks — spec-authoritative, more specific than keyword regex
    if spec.get("n_blades"):
        n_bl = int(spec["n_blades"])
        checks.append(
            f"exactly {n_bl} distinct blade/vane features visible radiating from center "
            f"(count carefully in top projection — must be {n_bl}, not fewer)"
        )
        sweep = str(spec.get("blade_sweep", "")).lower()
        if "backward" in sweep:
            checks.append(
                f"blades are swept backward (trailing edge leads outward, tip trails hub) — "
                f"visible as angled shapes in top projection"
            )
        elif "forward" in sweep:
            checks.append(
                f"blades are swept forward (leading edge leads outward) — "
                f"visible as forward-angled shapes in top projection"
            )
        elif "radial" in sweep:
            checks.append("blades are radial (straight, no sweep angle) in top projection")

    if spec.get("n_fins"):
        n_fi = int(spec["n_fins"])
        checks.append(f"exactly {n_fi} parallel fin features visible (count in front or side projection)")

    if spec.get("n_spokes"):
        n_sp = int(spec["n_spokes"])
        checks.append(f"exactly {n_sp} spoke/arm features visible radiating from hub (top projection)")

    # Angle check
    angle_match = re.search(r"(\d+)\s*degrees?", goal_lower)
    if angle_match:
        checks.append(f"angled surface at approximately {angle_match.group(1)} degrees visible")

    # If no checks found, add generic shape checks
    if not checks:
        checks.append("overall shape appears reasonable for the described part")
        checks.append("no obvious defects (missing features, floating geometry, zero-thickness walls)")

    return checks


# ---------------------------------------------------------------------------
# Vision API call
# ---------------------------------------------------------------------------

def _encode_image(path: str) -> str:
    """Read an image file and return its base64-encoded content."""
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def _build_vision_prompt(goal: str, checks: list[str], view_labels: list[str],
                         spec: dict | None = None) -> str:
    """Build the vision verification prompt anchored to the exact images being sent.

    view_labels must match the actual images 1:1 — this prevents vision models from
    hallucinating view types (e.g., claiming to see an 'isometric view' that wasn't rendered).
    """
    spec = spec or {}
    checklist_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(checks))
    image_list = "\n".join(f"  Image {i+1}: {label}" for i, label in enumerate(view_labels))

    # Build cross-section note only if a cross-section was actually rendered
    section_note = ""
    for i, label in enumerate(view_labels):
        if "cross-section" in label.lower() or "section" in label.lower():
            section_note = (
                f"\nImage {i+1} is a CROSS-SECTION — use it to check internal features "
                f"(bores, ribs, channels, wall thickness).\n"
            )
            break

    # Build proportion expectations from spec so the model can verify sizes, not just presence
    proportion_lines: list[str] = []
    if spec.get("od_mm") and spec.get("bore_mm"):
        ratio_pct = float(spec["bore_mm"]) / float(spec["od_mm"]) * 100
        proportion_lines.append(
            f"- bore diameter should appear ~{ratio_pct:.0f}% of the OD in the top view"
        )
    if spec.get("od_mm") and (spec.get("height_mm") or spec.get("thickness_mm")):
        h = float(spec.get("height_mm") or spec.get("thickness_mm"))
        h_pct = h / float(spec["od_mm"]) * 100
        proportion_lines.append(
            f"- height/thickness should appear ~{h_pct:.0f}% of the OD (front/side view)"
        )
    if spec.get("n_bolts") and spec.get("od_mm") and spec.get("bolt_circle_r_mm"):
        pcd_pct = float(spec["bolt_circle_r_mm"]) * 2 / float(spec["od_mm"]) * 100
        proportion_lines.append(
            f"- bolt circle should be at ~{pcd_pct:.0f}% of OD diameter (top view)"
        )
    proportion_block = ""
    if proportion_lines:
        proportion_block = (
            "\nPROPORTION EXPECTATIONS (verify these sizes, not just presence):\n"
            + "\n".join(proportion_lines) + "\n"
        )

    return (
        f"You are verifying a CAD model. The user asked for: \"{goal}\"\n\n"
        f"You have been provided exactly {len(view_labels)} image(s) of the generated part:\n"
        f"{image_list}\n"
        f"{section_note}"
        f"{proportion_block}\n"
        f"IMPORTANT: Only reference the images listed above. "
        f"Do NOT mention isometric, perspective, or any other views not in this list.\n\n"
        f"CRITICAL: Check BOTH presence AND correctness of features:\n"
        f"- Are features in the RIGHT LOCATION? (bolt holes ON the flange, not floating)\n"
        f"- Are features the RIGHT SIZE? (dimensions in the image titles are the actual mm — compare to spec)\n"
        f"- Are features the RIGHT COUNT? (count carefully, do not round up)\n"
        f"- Are features PROPERLY CONNECTED? (no floating/disconnected geometry)\n"
        f"- Is the overall SHAPE correct? (not a flat disc when it should be a bell nozzle)\n\n"
        f"BIAS INSTRUCTION: When uncertain, mark FAIL rather than PASS. "
        f"A conservative FAIL that gets corrected is better than a false PASS that ships wrong geometry. "
        f"In your 'notes' field, always write what you actually SEE (e.g. 'appears to have 3 holes, expected 4'), "
        f"not just whether you think it passes.\n\n"
        f"Check each of the following features:\n{checklist_text}\n\n"
        f"For each check, determine PASS or FAIL based ONLY on what you can see in the "
        f"images provided. If a feature is not clearly visible in any of the {len(view_labels)} "
        f"images, mark it FAIL — do not assume it exists.\n"
        f"Also note any obvious defects: missing geometry, floating parts, "
        f"zero-thickness walls, or shapes that clearly do not match the description.\n\n"
        f"Respond with ONLY valid JSON (no markdown, no code fences):\n"
        f'{{"checks": [{{"feature": "...", "found": true/false, "notes": "what I actually see: ..."}}], '
        f'"overall_match": true/false, "confidence": 0.0-1.0, "issues": ["..."]}}'
    )


def _parse_vision_json(text: str) -> dict | None:
    """Parse a vision model response into a dict, stripping code fences."""
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        print("[VISUAL] could not parse JSON from vision response")
        return None


# ---------------------------------------------------------------------------
# Geometry pre-check (deterministic — no LLM required)
# ---------------------------------------------------------------------------

def _geometry_precheck(stl_path: str, spec: dict) -> list[dict]:
    """Compare STL bounding box against extracted spec dimensions.

    Returns a list of check dicts (same format as vision check dicts) with
    objective pass/fail results.  These are prepended to the final check list
    so failures here can veto an otherwise-passing vision result.

    Only checks dimensions that are present in spec — never fails on missing
    spec fields.  Tolerance: ±15% on linear dimensions.
    """
    results: list[dict] = []
    try:
        import trimesh
        import numpy as np
        mesh = trimesh.load(stl_path)
        if hasattr(mesh, "geometry"):
            mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

        extents = mesh.bounding_box.extents  # (x_size, y_size, z_size) in mm
        TOL = 0.15  # ±15%

        def _dim_check(label: str, expected: float, actual: float) -> dict:
            err = abs(actual - expected) / max(expected, 1.0)
            ok = err <= TOL
            return {
                "feature": label,
                "found": ok,
                "notes": (
                    f"measured {actual:.1f}mm vs expected ~{expected:.0f}mm "
                    f"({'OK' if ok else f'ERROR: {err*100:.0f}% off, exceeds 15% tolerance'})"
                ),
                "_source": "geometry_precheck",
            }

        # OD / outer diameter
        if spec.get("od_mm"):
            od = float(spec["od_mm"])
            actual_od = max(extents[0], extents[1])
            results.append(_dim_check(f"outer diameter ~{od:.0f}mm", od, actual_od))

        # Width — for disc/gear parts "wide" means face width (Z axis), not X span
        if spec.get("width_mm") and not spec.get("od_mm"):
            w = float(spec["width_mm"])
            pt = spec.get("part_type", "")
            _gear_types = ("gear", "involute_gear", "spur_gear", "bevel_gear",
                           "helical_gear", "sprocket", "disc", "pulley")
            if any(g in pt for g in _gear_types) or any(
                    g in spec.get("goal", "").lower() for g in ("gear", "sprocket")):
                # face width lives on Z axis; skip the X-axis check
                results.append(_dim_check(f"width ~{w:.0f}mm", w, extents[2]))
            else:
                results.append(_dim_check(f"width ~{w:.0f}mm", w, extents[0]))

        # Length/depth
        if spec.get("length_mm"):
            l = float(spec["length_mm"])
            results.append(_dim_check(f"length ~{l:.0f}mm", l, max(extents[0], extents[1])))

        # Height / thickness
        for key in ("height_mm", "thickness_mm"):
            if spec.get(key):
                h = float(spec[key])
                results.append(_dim_check(f"height/thickness ~{h:.0f}mm", h, extents[2]))
                break  # don't double-check

        # Watertight mesh
        is_wt = bool(mesh.is_watertight)
        results.append({
            "feature": "geometry is watertight (no holes or open edges)",
            "found": is_wt,
            "notes": "watertight" if is_wt else "NOT watertight — mesh has open edges or holes",
            "_source": "geometry_precheck",
        })

        # Volume plausibility — compare actual mesh volume to theoretical estimate.
        # Catches grossly wrong parts (solid where it should be hollow, missing bore, etc.)
        # Only meaningful for watertight meshes (trimesh.volume is undefined for open meshes).
        if is_wt:
            try:
                actual_vol = abs(float(mesh.volume))
                theo_vol: float | None = None
                if spec.get("od_mm") and (spec.get("height_mm") or spec.get("thickness_mm")):
                    r_out = float(spec["od_mm"]) / 2
                    r_in  = float(spec.get("bore_mm") or spec.get("id_mm") or 0) / 2
                    h_val = float(spec.get("height_mm") or spec.get("thickness_mm") or 10)
                    theo_vol = np.pi * (r_out ** 2 - r_in ** 2) * h_val
                elif (spec.get("width_mm") and spec.get("height_mm")
                      and spec.get("depth_mm")):
                    theo_vol = (float(spec["width_mm"]) * float(spec["height_mm"])
                                * float(spec["depth_mm"]))
                if theo_vol is not None and theo_vol > 1.0:
                    ratio = actual_vol / theo_vol
                    # Allow 15%–150%: features/holes remove material, fillets add some
                    vol_ok = 0.15 <= ratio <= 1.50
                    results.append({
                        "feature": f"volume plausible (theoretical ~{theo_vol:.0f}mm^3)",
                        "found": vol_ok,
                        "notes": (
                            f"actual {actual_vol:.0f}mm^3, ratio={ratio:.2f} "
                            f"({'OK' if vol_ok else 'UNEXPECTED — part may be solid where hollow expected, or geometry is wrong'})"
                        ),
                        "_source": "geometry_precheck",
                    })
            except Exception:
                pass

        # Topological hole count via Euler characteristic.
        # For a closed orientable surface: chi = V - E + F = 2 - 2*genus.
        # genus = number of topological through-holes.
        # A cylinder with bore has genus=1; flange with bore+4 bolts has genus=5.
        if is_wt:
            try:
                chi = int(mesh.euler_number)
                genus = max(0, (2 - chi) // 2)
                expected_holes = 0
                if spec.get("bore_mm") or spec.get("id_mm"):
                    expected_holes += 1
                if spec.get("n_bolts"):
                    expected_holes += int(spec["n_bolts"])
                if expected_holes > 0:
                    holes_ok = abs(genus - expected_holes) <= 2  # ±2 for chamfers/features
                    results.append({
                        "feature": f"~{expected_holes} through-holes (bore + bolt holes topology)",
                        "found": holes_ok,
                        "notes": (
                            f"mesh genus={genus} (Euler chi={chi}), "
                            f"expected ~{expected_holes} "
                            f"({'OK' if holes_ok else f'MISMATCH: genus={genus} vs expected={expected_holes}'})"
                        ),
                        "_source": "geometry_precheck",
                    })
            except Exception:
                pass

        # Winding consistency — inverted normals cause shaded renders to look dark/wrong
        try:
            if hasattr(mesh, "is_winding_consistent") and not bool(mesh.is_winding_consistent):
                results.append({
                    "feature": "mesh winding consistent (no inverted normals)",
                    "found": False,
                    "notes": "inconsistent winding — some faces point inward; renders will appear dark",
                    "_source": "geometry_precheck",
                })
        except Exception:
            pass

        # Disconnected component count — solid parts should be a single connected body.
        # Multiple components usually means floating geometry, incomplete boolean, or
        # a parametric error where features don't union correctly.
        try:
            components = mesh.split(only_watertight=False)
            n_comp = len(components) if hasattr(components, "__len__") else 1
            comp_ok = n_comp == 1
            results.append({
                "feature": "single connected body (no floating geometry)",
                "found": comp_ok,
                "notes": (
                    f"{n_comp} connected component(s) — OK"
                    if comp_ok else
                    f"{n_comp} disconnected components — likely floating geometry or failed boolean"
                ),
                "_source": "geometry_precheck",
            })
        except Exception:
            pass

        # Minimum face count — fewer than 20 faces is almost always degenerate.
        # Catches cases where CadQuery generated an empty or near-empty solid.
        n_faces = len(mesh.faces)
        face_ok = n_faces >= 20
        results.append({
            "feature": "sufficient geometry (>=20 faces)",
            "found": face_ok,
            "notes": (
                f"{n_faces} faces — OK"
                if face_ok else
                f"only {n_faces} faces — mesh is likely degenerate or empty"
            ),
            "_source": "geometry_precheck",
        })

    except Exception as exc:
        print(f"[VISUAL] geometry precheck error: {exc}")

    return results


# Session-level quota tracker — if Gemini quota is exhausted, skip it for the rest of the session
_gemini_quota_exhausted: bool = False


def _call_vision_gemini(
    image_paths: list[str],
    prompt: str,
    repo_root: Path | None = None,
) -> dict | None:
    """Try Gemini vision API for verification. Returns parsed dict or None."""
    global _gemini_quota_exhausted
    if _gemini_quota_exhausted:
        print("[VISUAL] gemini quota exhausted this session — skipping to Ollama/Anthropic")
        return None

    from .llm_client import get_google_key, _gemini_model

    api_key = get_google_key(repo_root)
    if not api_key:
        return None

    # Try new google-genai SDK
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        return None

    client = genai.Client(api_key=api_key)

    # Read image bytes + prompt as plain string (not Part.from_text which varies by SDK version)
    parts: list = []
    for img_path in image_paths:
        with open(img_path, "rb") as f:
            img_bytes = f.read()
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))
    parts.append(prompt)  # plain string — google-genai accepts str in contents list

    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=4096,
    )

    # Model preference: gemini-2.5-flash first, then configured model, then 2.0-flash
    configured = _gemini_model(repo_root)
    model_candidates = ["gemini-2.5-flash"]
    if configured not in model_candidates:
        model_candidates.append(configured)
    if "gemini-2.0-flash" not in model_candidates:
        model_candidates.append("gemini-2.0-flash")

    for try_model in model_candidates:
        # Retry up to 2 times per model (Gemini sometimes returns short/malformed JSON)
        succeeded = False
        for attempt in range(2):
            try:
                # Use higher temperature on retry to get different output
                retry_cfg = cfg if attempt == 0 else types.GenerateContentConfig(
                    temperature=0.3, max_output_tokens=4096)
                response = client.models.generate_content(
                    model=try_model,
                    contents=parts,
                    config=retry_cfg,
                )
                text = (response.text or "").strip()
                if not text:
                    continue
                print(f"[VISUAL] vision response from gemini/{try_model} ({len(text)} chars)")
                parsed = _parse_vision_json(text)
                if parsed and parsed.get("checks"):
                    parsed["_verified_by"] = f"gemini/{try_model}"
                    return parsed
                if attempt == 0:
                    print(f"[VISUAL] malformed response, retrying...")
                    continue
                if parsed:
                    return parsed  # return whatever we got on 2nd attempt
                succeeded = True  # got a response but unparseable — try next model
                break
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    # All Gemini flash models share the same daily quota —
                    # one 429 means all flash variants are done for the session.
                    _gemini_quota_exhausted = True
                    print(f"[VISUAL] Gemini daily quota exhausted — will use Anthropic for rest of session")
                    return None  # skip remaining models, go straight to Anthropic
                if "model" in err_str.lower() or "not found" in err_str.lower():
                    print(f"[VISUAL] model {try_model} not available, trying next")
                    break  # try next model
                print(f"[VISUAL] gemini vision error ({try_model}): {exc}")
                break  # unexpected error — try next model

    print("[VISUAL] all gemini models exhausted or failed")
    return None


def _call_vision_anthropic(
    image_paths: list[str],
    prompt: str,
    repo_root: Path | None = None,
) -> dict | None:
    """Try Anthropic Claude vision API for verification. Returns parsed dict or None."""
    from .llm_client import get_anthropic_key

    api_key = get_anthropic_key(repo_root)
    if not api_key:
        return None

    try:
        import anthropic  # type: ignore
    except ImportError:
        return None

    # Build content blocks: images + text
    content: list[dict[str, Any]] = []
    for img_path in image_paths:
        b64 = _encode_image(img_path)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
        })
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic(api_key=api_key)

    for model in ("claude-sonnet-4-6", "claude-3-5-sonnet-20241022"):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": content}],
            )
            text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
            if not text:
                continue
            print(f"[VISUAL] vision response from anthropic/{model} ({len(text)} chars)")
            parsed = _parse_vision_json(text)
            if parsed is not None:
                parsed["_verified_by"] = f"anthropic/{model}"
            return parsed
        except Exception as exc:
            err = str(exc).lower()
            if "model" in err or "not_found" in err:
                continue
            print(f"[VISUAL] anthropic vision error ({model}): {exc}")
            return None

    return None


def _call_vision_groq(
    image_paths: list[str],
    prompt: str,
    repo_root: Path | None = None,
) -> dict | None:
    """Try Groq vision API for verification.

    Uses llama-4-scout-17b-16e-instruct (or llama-3.2-11b-vision-preview).
    Fast (~1-3s), free tier, completely separate quota from Gemini.
    Returns parsed dict or None.
    """
    from .llm_client import get_groq_key

    api_key = get_groq_key(repo_root)
    if not api_key:
        return None

    try:
        import groq as _groq  # type: ignore
    except ImportError:
        print("[VISUAL] groq package not installed — run: pip install groq")
        return None

    # Groq vision uses URLs or base64; we'll use base64 inline
    content: list[dict] = []
    for img_path in image_paths:
        b64 = _encode_image(img_path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    content.append({"type": "text", "text": prompt})

    client = _groq.Groq(api_key=api_key)

    # Preference: scout (largest/best) → llama-3.2-11b (reliable fallback)
    for model in ("meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.2-11b-vision-preview"):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=1024,
                temperature=0,
            )
            text = resp.choices[0].message.content.strip()
            if not text:
                continue
            print(f"[VISUAL] vision response from groq/{model} ({len(text)} chars)")
            parsed = _parse_vision_json(text)
            if parsed is not None:
                parsed["_verified_by"] = f"groq/{model}"
            return parsed
        except Exception as exc:
            err = str(exc)
            if "model" in err.lower() or "not found" in err.lower() or "does not support" in err.lower():
                print(f"[VISUAL] groq model {model} not available, trying next")
                continue
            if "429" in err or "rate_limit" in err.lower():
                print(f"[VISUAL] groq rate limited: {exc}")
                return None
            print(f"[VISUAL] groq vision error ({model}): {exc}")
            return None

    print("[VISUAL] all groq models failed")
    return None


def _call_vision_ollama(
    image_paths: list[str],
    prompt: str,
) -> dict | None:
    """Try Ollama vision inference for verification.

    Uses gemma4:e4b (multimodal, already pulled) as primary.
    Falls back to llava:7b, llava-phi3, llava if present.
    Returns parsed dict or None.
    """
    import urllib.request
    import urllib.error

    host = "http://localhost:11434"

    # Check which vision-capable models are available
    available: list[str] = []
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            import json as _json
            data = _json.loads(resp.read().decode("utf-8"))
        available = [m["name"] for m in data.get("models", [])]
    except Exception:
        return None  # Ollama not running

    if not available:
        return None

    # Preference order: gemma4 (multimodal, already pulled) → llava variants
    vision_candidates = [
        "gemma4:e4b", "gemma4:4b", "gemma4",
        "llava-phi3", "llava:7b", "llava-llama3", "llava",
        "minicpm-v", "moondream2",
    ]
    model = next(
        (c for c in vision_candidates
         if any(a == c or a.startswith(c.split(":")[0] + ":") for a in available)),
        None,
    )
    if model is None:
        print("[VISUAL] no vision-capable Ollama model found")
        return None

    # Encode images as base64 — downscale to 400×300 to fit in 6GB VRAM
    # alongside the loaded model weights (800×600 @ 3 images saturates VRAM)
    images_b64: list[str] = []
    for img_path in image_paths:
        try:
            import PIL.Image as _PIL
            import io as _io
            img = _PIL.open(img_path)
            img.thumbnail((400, 300), _PIL.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            images_b64.append(base64.standard_b64encode(buf.getvalue()).decode("ascii"))
        except Exception:
            # PIL not available — send full-size and hope for the best
            with open(img_path, "rb") as f:
                images_b64.append(base64.standard_b64encode(f.read()).decode("ascii"))

    payload = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": images_b64,
        }],
        "stream": False,
        "options": {"temperature": 0},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{host}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("message", {}).get("content", "").strip()
        if not text:
            return None
        print(f"[VISUAL] vision response from ollama/{model} ({len(text)} chars)")
        parsed = _parse_vision_json(text)
        if parsed is not None:
            # Local 4B models are reliable for catching obvious failures but
            # overconfident on subtle issues. Cap at 0.85 so Ollama can never
            # single-handedly confirm a PASS — only Gemini/Claude can reach 0.90+.
            raw_conf = float(parsed.get("confidence", 0.0))
            if raw_conf > 0.85:
                parsed["confidence"] = 0.85
                print(f"[VISUAL] ollama confidence capped: {raw_conf:.2f} -> 0.85 (local model ceiling)")
            parsed["_verified_by"] = f"ollama/{model}"
        return parsed
    except urllib.error.HTTPError as exc:
        print(f"[VISUAL] ollama vision HTTP error: {exc.code}")
        return None
    except Exception as exc:
        print(f"[VISUAL] ollama vision error: {exc}")
        return None


def _call_vision(
    image_paths: list[str],
    view_labels: list[str],
    goal: str,
    checks: list[str],
    repo_root: Path | None = None,
    spec: dict | None = None,
) -> dict | None:
    """Send rendered views to vision AI and parse the verification result.

    Priority: Gemini 2.5 Flash → Groq llama-4-scout → Ollama gemma4:e4b → Anthropic Claude.

    Cross-validation: if a non-Anthropic provider reports PASS at ≥0.80, a second
    provider is also called.  The final result takes the lower confidence and requires
    both to agree on PASS.  This prevents a single overconfident model from approving
    bad geometry.

    Returns parsed dict or None if no API is available.
    """
    prompt = _build_vision_prompt(goal, checks, view_labels, spec=spec)

    # Confidence caps per provider — self-reported 100% is never trustworthy
    _CONF_CAPS = {
        "gemini": 0.95,
        "groq": 0.92,
        "ollama": 0.85,  # already capped inside _call_vision_ollama, but belt+suspenders
        "anthropic": 1.0,  # Claude is authoritative — no cap
    }

    def _apply_cap(result: dict) -> dict:
        if result is None:
            return result
        provider = result.get("_verified_by", "").split("/")[0]
        cap = _CONF_CAPS.get(provider, 0.92)
        raw = float(result.get("confidence", 0.0))
        if raw > cap:
            result["confidence"] = cap
            print(f"[VISUAL] {provider} confidence capped: {raw:.2f} -> {cap:.2f}")
        return result

    # Provider call order
    providers = [
        ("gemini",    lambda: _call_vision_gemini(image_paths, prompt, repo_root)),
        ("groq",      lambda: _call_vision_groq(image_paths, prompt, repo_root)),
        ("ollama",    lambda: _call_vision_ollama(image_paths, prompt)),
        ("anthropic", lambda: _call_vision_anthropic(image_paths, prompt, repo_root)),
    ]

    primary: dict | None = None
    primary_provider: str = ""
    for name, fn in providers:
        try:
            result = fn()
            if result is not None:
                primary = _apply_cap(result)
                primary_provider = name
                break
        except Exception as exc:
            print(f"[VISUAL] {name} unexpected error: {exc}")

    if primary is None:
        return None

    # Cross-validation: if primary is high-confidence PASS from a non-authoritative
    # provider, run the next provider to confirm.  Anthropic is considered authoritative
    # (paid, high quality) — it can solo-approve without a second opinion.
    CROSS_VAL_THRESHOLD = 0.80  # was 0.90 — cross-validate more aggressively
    primary_passes = (
        primary.get("overall_match", False)
        and float(primary.get("confidence", 0)) >= CROSS_VAL_THRESHOLD
        and primary_provider != "anthropic"
    )

    if primary_passes:
        # Run the next provider after the primary in the chain
        provider_names = [p[0] for p in providers]
        primary_idx = provider_names.index(primary_provider)
        cross_result: dict | None = None
        for name, fn in providers[primary_idx + 1:]:
            try:
                cross_result = fn()
                if cross_result is not None:
                    cross_result = _apply_cap(cross_result)
                    break
            except Exception as exc:
                print(f"[VISUAL] cross-validation {name} error: {exc}")

        if cross_result is not None:
            cross_by = cross_result.get("_verified_by", "unknown")
            cross_pass = cross_result.get("overall_match", False)
            cross_conf = float(cross_result.get("confidence", 0.0))
            primary_conf = float(primary.get("confidence", 0.0))

            print(f"[VISUAL] cross-validation via {cross_by}: "
                  f"{'PASS' if cross_pass else 'FAIL'} @ {cross_conf:.2f}")

            # Take conservative result: both must agree on PASS
            if not cross_pass:
                primary["overall_match"] = False
                primary["issues"] = list(primary.get("issues", [])) + [
                    f"Cross-validation by {cross_by} disagreed — marking FAIL to be safe"
                ]
            # Always take the lower confidence score
            primary["confidence"] = min(primary_conf, cross_conf)
            primary["_cross_validated_by"] = cross_by

    return primary


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def verify_visual(
    step_path: str,
    stl_path: str,
    goal: str,
    spec: dict,
    *,
    repo_root: Path | None = None,
) -> dict:
    """Render the part from 3 angles, send to vision AI, get verification.

    Parameters
    ----------
    step_path : str
        Path to the STEP file (used for metadata only; rendering uses STL).
    stl_path : str
        Path to the STL file to render.
    goal : str
        The natural-language goal that was used to generate the part.
    spec : dict
        Extracted spec dict (from spec_extractor.extract_spec).
    repo_root : Path | None
        Repository root for .env lookup.

    Returns
    -------
    dict with keys:
        verified     : bool | None   — True if all checks pass, None if API unavailable
        confidence   : float          — 0.0-1.0 confidence score
        checks       : list[dict]     — per-feature check results
        issues       : list[str]      — detected issues
        screenshots  : list[str]      — paths to rendered PNGs
        reason       : str | None     — explanation if verification was skipped
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    result: dict[str, Any] = {
        "verified": None,
        "confidence": 0.0,
        "checks": [],
        "issues": [],
        "screenshots": [],
        "reason": None,
    }

    # Validate STL exists
    stl = Path(stl_path)
    if not stl.exists():
        result["reason"] = f"STL file not found: {stl_path}"
        print(f"[VISUAL] {result['reason']}")
        return result

    # --- Step 1: Render views -----------------------------------------------
    screenshot_dir = repo_root / "outputs" / "screenshots"
    try:
        paths, view_labels = _render_views(stl_path, goal, screenshot_dir)
        result["screenshots"] = paths
        print(f"[VISUAL] rendered {len(paths)} views to {screenshot_dir}")
    except Exception as exc:
        result["reason"] = f"rendering failed: {exc}"
        print(f"[VISUAL] {result['reason']}")
        return result

    # --- Step 2: Geometry pre-check (deterministic, no LLM) -----------------
    precheck_results = _geometry_precheck(stl_path, spec or {})
    if precheck_results:
        n_pre_pass = sum(1 for c in precheck_results if c.get("found", False))
        n_pre_total = len(precheck_results)
        pre_ok = n_pre_pass == n_pre_total
        print(f"[VISUAL] geometry precheck: {n_pre_pass}/{n_pre_total} checks passed"
              + (" (all OK)" if pre_ok else " (FAILURES — geometry may not match spec)"))
        for c in precheck_results:
            flag = "OK" if c.get("found") else "XX"
            print(f"  [{flag}] {c.get('feature', '?')}: {c.get('notes', '')}")

    # --- Step 3: Build visual checklist -------------------------------------
    checks = _build_checklist(goal, spec or {})
    print(f"[VISUAL] built {len(checks)} feature checks from goal + spec")

    # --- Step 4: Send to vision API -----------------------------------------
    vision_result = _call_vision(paths, view_labels, goal, checks, repo_root, spec=spec)

    if vision_result is None:
        result["reason"] = "vision API unavailable"
        print("[VISUAL] vision API unavailable — skipping visual verification")
        # Still return precheck results even if vision is unavailable
        if precheck_results:
            result["checks"] = precheck_results
            pre_ok = all(c.get("found", False) for c in precheck_results)
            result["verified"] = pre_ok
            result["confidence"] = 0.7 if pre_ok else 0.0
            result["verified_by"] = "geometry_precheck_only"
        return result

    # --- Step 5: Combine precheck + vision results --------------------------
    vision_checks = vision_result.get("checks", [])
    issues = vision_result.get("issues", [])
    overall = vision_result.get("overall_match", False)
    confidence = vision_result.get("confidence", 0.0)

    # Ensure confidence is a float in [0, 1]
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    # Precheck failures veto the final result regardless of vision confidence
    precheck_failed = any(
        not c.get("found", True) and c.get("_source") == "geometry_precheck"
        for c in precheck_results
    )
    if precheck_failed:
        overall = False
        # Penalise confidence proportional to how many precheck items failed
        n_pre_fail = sum(1 for c in precheck_results if not c.get("found", True))
        confidence = confidence * max(0.0, 1.0 - 0.15 * n_pre_fail)
        issues = list(issues) + [
            "Geometry precheck: one or more dimension checks failed "
            "(bounding box does not match spec within 15%)"
        ]

    # Merge precheck + vision checks for full picture
    all_checks = precheck_results + vision_checks
    n_pass = sum(1 for c in all_checks if c.get("found", False))
    n_total = len(all_checks)

    result["verified"] = bool(overall) and n_pass == n_total
    result["confidence"] = confidence
    result["checks"] = all_checks
    result["issues"] = [i for i in issues if i]  # filter empty strings
    result["verified_by"] = vision_result.get("_verified_by", "unknown")
    if vision_result.get("_cross_validated_by"):
        result["verified_by"] += f" + {vision_result['_cross_validated_by']}"

    status = "PASS" if result["verified"] else "FAIL"
    verified_by = result["verified_by"]
    n_vision_pass = sum(1 for c in vision_checks if c.get("found", False))
    n_vision_total = len(vision_checks)
    print(
        f"[VISUAL] verification {status} (via {verified_by}): "
        f"{n_vision_pass}/{n_vision_total} vision checks passed, "
        f"confidence={confidence:.2f}, "
        f"{len(result['issues'])} issue(s)"
    )
    for check in all_checks:
        flag = "OK" if check.get("found") else "XX"
        src = " [geo]" if check.get("_source") == "geometry_precheck" else ""
        print(f"  [{flag}]{src} {check.get('feature', '?')}: {check.get('notes', '')}")
    for issue in result["issues"]:
        print(f"  [!!] {issue}")

    return result
