"""AutoCAD bridge for ARIA-OS.

HTTP listener (port 7503) that drives AutoCAD via COM on Windows.
Primarily for 2D technical drawings but includes 3D solid modeling,
dimensioning (DIMLINEAR, DIMDIAMETER), and GD&T (TOLERANCE command).

Entry point: aria_autocad_server.main()
"""
