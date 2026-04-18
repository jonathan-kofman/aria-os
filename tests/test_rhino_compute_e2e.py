"""
End-to-end test: Rhino Compute headless geometry pipeline.

Pipeline:
1. Compute: boolean operations on NURBS breps (server-side)
2. rhino3dm: save brep to 3DM
3. CadQuery: parallel geometry + STEP/STL export
4. trimesh: visual verification (dimensions, volume, watertight)

Requires: Rhino Compute running on localhost:8081
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "cad" / "compute_test"


def test_compute_geometry():
    """Create brake drum via Compute boolean ops."""
    import rhino3dm
    import compute_rhino3d.Util
    import compute_rhino3d.Brep

    compute_rhino3d.Util.url = "http://localhost:8081/"

    # Verify server
    from aria_os.compute_client import ComputeClient
    client = ComputeClient()
    assert client.is_available(), "Compute not running"
    v = client.version()
    print(f"  Server: Rhino {v['rhino']}, Compute {v['compute']}")

    # Build brake drum: OD=200, wall=8, bore=40, height=60
    center = rhino3dm.Point3d(0, 0, 0)
    outer = rhino3dm.Cylinder(rhino3dm.Circle(center, 100), 60).ToBrep(True, True)
    inner = rhino3dm.Cylinder(rhino3dm.Circle(center, 92), 60.5).ToBrep(True, True)
    bore = rhino3dm.Cylinder(rhino3dm.Circle(center, 20), 61).ToBrep(True, True)

    shell = compute_rhino3d.Brep.CreateBooleanDifference1([outer], [inner], 0.001, True)
    assert shell and len(shell) > 0, "Shell boolean failed"

    drum = compute_rhino3d.Brep.CreateBooleanDifference1(shell, [bore], 0.001, True)
    brep = drum[0] if drum else shell[0]
    bb = brep.GetBoundingBox()
    dims = (bb.Max.X - bb.Min.X, bb.Max.Y - bb.Min.Y, bb.Max.Z - bb.Min.Z)
    print(f"  Dimensions: {dims[0]:.0f}x{dims[1]:.0f}x{dims[2]:.0f}mm")
    print(f"  Valid: {brep.IsValid}, Faces: {len(brep.Faces)}")
    assert brep.IsValid
    assert abs(dims[0] - 200) < 1, f"Expected ~200mm OD, got {dims[0]}"

    return brep


def test_export(brep):
    """Export to 3DM + STEP + STL."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 3DM via rhino3dm
    from aria_os.rhino_export import brep_to_3dm
    dm_path = brep_to_3dm(brep, OUT_DIR / "brake_drum.3dm")
    dm_size = os.path.getsize(dm_path) / 1024
    print(f"  3DM: {dm_size:.1f} KB")
    assert dm_size > 1, "3DM too small"

    # STEP + STL via CadQuery
    import cadquery as cq
    od, height, wall, bore_d = 200.0, 60.0, 8.0, 40.0
    cq_result = (
        cq.Workplane("XY")
        .circle(od / 2).extrude(height)
        .cut(cq.Workplane("XY").circle((od - 2 * wall) / 2).extrude(height))
        .cut(cq.Workplane("XY").circle(bore_d / 2).extrude(height))
    )

    step_path = str(OUT_DIR / "brake_drum.step")
    stl_path = str(OUT_DIR / "brake_drum.stl")
    cq.exporters.export(cq_result, step_path)
    cq.exporters.export(cq_result, stl_path, exportType="STL")
    print(f"  STEP: {os.path.getsize(step_path) / 1024:.1f} KB")
    print(f"  STL:  {os.path.getsize(stl_path) / 1024:.1f} KB")

    return stl_path


def test_verify(stl_path: str):
    """Verify exported geometry."""
    import trimesh
    m = trimesh.load(stl_path)
    d = m.bounds[1] - m.bounds[0]
    print(f"  Dims: {d[0]:.1f}x{d[1]:.1f}x{d[2]:.1f}mm")
    print(f"  Volume: {m.volume:.0f} mm3")
    print(f"  Watertight: {m.is_watertight}")
    print(f"  Mesh: {len(m.vertices)} verts, {len(m.faces)} faces")
    assert m.is_watertight, "Mesh not watertight"
    assert abs(d[0] - 200) < 1, f"X dimension wrong: {d[0]}"
    assert m.volume > 200000, f"Volume too small: {m.volume}"


def main():
    print("=" * 50)
    print("Rhino Compute E2E Pipeline Test")
    print("=" * 50)

    print("\n1. Geometry via Compute...")
    brep = test_compute_geometry()

    print("\n2. Export (3DM + STEP + STL)...")
    stl_path = test_export(brep)

    print("\n3. Verification...")
    test_verify(stl_path)

    print("\n" + "=" * 50)
    print("ALL TESTS PASSED")
    print(f"Output: {OUT_DIR}")
    print("=" * 50)


if __name__ == "__main__":
    main()
