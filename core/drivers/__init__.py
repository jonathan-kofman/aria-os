"""
core.drivers — CAD backend drivers for the IGL abstraction layer.

Each driver implements the CADDriver interface from base_driver.py and
translates an IGL document into native CAD operations for its backend.

Available drivers:
    CadQueryDriver  — wraps the existing aria_os/generators/cadquery_generator
    FreeCADDriver   — requires FreeCAD Python bindings
    OnshapeDriver   — requires ONSHAPE_ACCESS_KEY + ONSHAPE_SECRET_KEY env
    RhinoDriver     — wraps Rhino Compute at localhost:8081

The DriverManager (manager.py) handles availability checks, feature-coverage
validation, and fallback between drivers.
"""

from .base_driver import CADDriver, DriverResult
from .manager import DriverManager

__all__ = ["CADDriver", "DriverResult", "DriverManager"]
