"""Engineering drawings — headless via FreeCAD TechDraw.

Separate from the existing cadquery-based 2D projection SVGs because
pro drawings need real GD&T annotations (feature control frames,
datums, surface finish, tolerance callouts) that CadQuery doesn't
produce. TechDraw is FreeCAD's drawing workbench and supports those.
"""
