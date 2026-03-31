"""
aria_os/blender_generator.py
Writes headless Blender script artifacts.

For lattice part_ids (lattice, gyroid_lattice, aria_lattice, sdf_lattice),
delegates to the real Blender lattice pipeline (arc_weave, interlaced).
For all other parts, writes a placeholder solid script.
"""
from pathlib import Path
from typing import Any, Optional

_LATTICE_PART_IDS = {"lattice", "gyroid_lattice", "aria_lattice", "sdf_lattice"}

# Goal keywords that select a non-default lattice pattern
_GYROID_KW = {"gyroid", "tpms", "minimal surface"}
_OCTET_KW  = {"octet", "octet truss", "3d truss"}
_WEAVE_KW  = {"weave", "coil", "interwov", "over.under", "celtic", "braid",
               "woven", "arc weave", "arc_weave"}


def _pick_lattice_pattern(goal: str) -> tuple[str, bool]:
    """Return (pattern_name, interlaced) based on goal keywords."""
    g = goal.lower()
    if any(kw in g for kw in _GYROID_KW):
        return "honeycomb", False   # closest headless approximation to gyroid
    if any(kw in g for kw in _OCTET_KW):
        return "octet_truss", False
    # default: arc_weave with interlacing ON (best match for over-under coil image)
    return "arc_weave", True


def _extract_lattice_dims(plan: dict[str, Any], goal: str) -> dict[str, Any]:
    """Pull lattice dimensions from plan params or fall back to defaults."""
    p = plan.get("params") or {}
    b = plan.get("base_shape") or {}
    if not isinstance(b, dict):
        b = {}
    return {
        "width_mm":        float(p.get("width_mm")  or b.get("width")  or 100.0),
        "height_mm":       float(p.get("height_mm") or b.get("height") or 100.0),
        "depth_mm":        float(p.get("depth_mm")  or b.get("depth")  or 10.0),
        "cell_size_mm":    float(p.get("cell_size_mm", 10.0)),
        "strut_diameter_mm": float(p.get("strut_diameter_mm", 1.5)),
    }


def write_blender_artifacts(
    plan: dict[str, Any],
    goal: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    part_slug = (plan.get("part_id") or "aria_part").replace("/", "_")
    out_dir = repo_root / "outputs" / "cad" / "blender"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Lattice-specific path: use the real generate_lattice() pipeline      #
    # ------------------------------------------------------------------ #
    if part_slug in _LATTICE_PART_IDS or part_slug.startswith("lattice"):
        try:
            from aria_os.lattice import generate_lattice, LatticeParams  # type: ignore

            pattern, interlaced = _pick_lattice_pattern(goal)
            dims = _extract_lattice_dims(plan, goal)

            lp = LatticeParams(
                pattern=pattern,
                form="volumetric",
                width_mm=dims["width_mm"],
                height_mm=dims["height_mm"],
                depth_mm=dims["depth_mm"],
                cell_size_mm=dims["cell_size_mm"],
                strut_diameter_mm=dims["strut_diameter_mm"],
                interlaced=interlaced,
                stl_path=stl_path,
                part_name=part_slug,
            )
            result = generate_lattice(lp)
            return {
                "script_path": str(out_dir / f"{part_slug}.py"),
                "stl_path":    result.stl_path if result else stl_path,
                "pattern":     pattern,
                "interlaced":  str(interlaced),
                "summary":     result.summary if result else "",
            }
        except Exception as exc:
            # Fall through to placeholder if Blender not installed
            print(f"[BLENDER] Lattice pipeline unavailable ({exc}); writing placeholder")

    # ------------------------------------------------------------------ #
    # Generic placeholder (non-lattice or Blender not available)           #
    # ------------------------------------------------------------------ #
    script_path = out_dir / f"{part_slug}.py"
    script_path.write_text(
        f'''"""
Run:
  blender --background --python "{script_path}"
"""
import bpy
from pathlib import Path

def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = "{part_slug}"
    Path(r"{stl_path}").parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.stl_export(filepath=r"{stl_path}", export_selected_objects=False)
    print("Exported STL:", r"{stl_path}")

if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )
    return {"script_path": str(script_path)}
