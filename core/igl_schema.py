"""
core/igl_schema.py — Intermediate Geometry Language (IGL) Pydantic models.

IGL is a CAD-agnostic JSON representation of design intent: a part is an
ordered list of features applied to an initial stock shape. This mirrors how
every parametric CAD system works internally (feature tree / history tree).

A valid IGL document looks like:

    {
      "igl_version": "1.0",
      "part": {"name": "Turbine Mount Bracket", "units": "inches",
               "material": "6061-T6 Aluminum"},
      "stock": {"type": "block", "x": 6.0, "y": 4.0, "z": 2.0},
      "features": [
        {"id": "f1", "type": "pocket",
         "params": {"face": "top", "profile": "rectangle", "center_x": 2.0,
                    "center_y": 2.0, "length": 2.5, "width": 1.8,
                    "depth": 0.5, "corner_radius": 0.125}},
        ...
      ]
    }

Design principles:
- Units are explicit and uniform throughout a document (no mixed mm/inch).
- Feature params are a free-form dict per feature type, validated by type.
- Features are ordered; later features may reference earlier ones via depends_on.
- Drivers declare which feature types they support via get_supported_features().

This module only validates document SHAPE. Semantic validation (e.g. pocket
deeper than stock, feature outside bounds) lives in core/igl_validator.py.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enums and literal types
# ---------------------------------------------------------------------------

class Units(str, Enum):
    """Supported unit systems. Units are document-wide, not per-feature."""
    MM = "mm"
    INCHES = "inches"
    METERS = "meters"


class Face(str, Enum):
    """Named stock faces. Used to anchor features."""
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"
    FRONT = "front"
    BACK = "back"


# ---------------------------------------------------------------------------
# Part metadata
# ---------------------------------------------------------------------------

class PartInfo(BaseModel):
    """Top-level metadata for a part."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Human-readable part name")
    units: Units = Field(Units.MM, description="Document-wide unit system")
    material: Optional[str] = Field(None, description="Material name or spec")
    description: Optional[str] = Field(None, description="Free-form description")


# ---------------------------------------------------------------------------
# Stock shapes
# ---------------------------------------------------------------------------

class StockBlock(BaseModel):
    """Rectangular prism stock centered on origin, extending in +X/+Y/+Z."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["block"] = "block"
    x: float = Field(..., gt=0, description="Length along X (world units)")
    y: float = Field(..., gt=0, description="Length along Y")
    z: float = Field(..., gt=0, description="Length along Z")


class StockCylinder(BaseModel):
    """Solid cylinder along Z axis, base on Z=0."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["cylinder"] = "cylinder"
    diameter: float = Field(..., gt=0)
    height: float = Field(..., gt=0)


class StockTube(BaseModel):
    """Hollow cylinder along Z axis."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["tube"] = "tube"
    outer_diameter: float = Field(..., gt=0)
    inner_diameter: float = Field(..., ge=0)
    height: float = Field(..., gt=0)

    @field_validator("inner_diameter")
    @classmethod
    def _inner_lt_outer(cls, v: float, info) -> float:
        od = info.data.get("outer_diameter")
        if od is not None and v >= od:
            raise ValueError("inner_diameter must be strictly less than outer_diameter")
        return v


class StockFromProfile(BaseModel):
    """Extruded 2D sketch — for complex outlines."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["from_profile"] = "from_profile"
    profile_id: str = Field(..., description="Reference to a Sketch element")
    height: float = Field(..., gt=0)


Stock = Union[StockBlock, StockCylinder, StockTube, StockFromProfile]


# ---------------------------------------------------------------------------
# Feature types
#
# Each feature carries a type-specific params dict. The params are intentionally
# loose (dict[str, Any]) at the schema level and enforced at the validator
# level so we can add new feature types without breaking older documents.
# ---------------------------------------------------------------------------

class FeatureBase(BaseModel):
    """Base for every feature. Enforces id + type + params + depends_on."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique ID used by dependent features")
    type: str = Field(..., description="Feature type tag")
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: Optional[list[str]] = Field(
        None, description="IDs of features that must exist before this one"
    )


# The set of known feature type tags. Drivers can still accept unknown tags
# (and report them via validate_igl) but we keep a canonical list here so the
# validator can warn on typos.
KNOWN_FEATURE_TYPES: frozenset[str] = frozenset({
    # Subtractive
    "pocket", "hole", "hole_pattern", "slot", "groove", "cutout",
    # Additive
    "boss", "rib", "pad",
    # Modifiers
    "fillet", "chamfer", "shell", "mirror", "pattern_linear", "pattern_circular",
    # Sheet metal
    "bend", "flange", "tab", "relief",
    # Sketches (usually referenced by other features, not standalone)
    "sketch",
})


# ---------------------------------------------------------------------------
# Document root
# ---------------------------------------------------------------------------

class IGLMetadata(BaseModel):
    """Optional metadata block at the document root."""
    model_config = ConfigDict(extra="allow")  # generators may add fields

    generated_by: Optional[str] = None
    timestamp: Optional[str] = None
    source_prompt: Optional[str] = None


class IGLDocument(BaseModel):
    """
    Root of an IGL document.

    Use IGLDocument.model_validate(data) to parse a dict into a validated
    document, and model.model_dump(mode='json') to serialize back.
    """
    model_config = ConfigDict(extra="forbid")

    igl_version: str = Field("1.0", description="IGL schema version tag")
    part: PartInfo
    stock: Stock
    features: list[FeatureBase] = Field(default_factory=list)
    metadata: Optional[IGLMetadata] = None

    @field_validator("features")
    @classmethod
    def _unique_ids(cls, v: list[FeatureBase]) -> list[FeatureBase]:
        seen: set[str] = set()
        for f in v:
            if f.id in seen:
                raise ValueError(f"duplicate feature id: {f.id}")
            seen.add(f.id)
        return v


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def parse(data: Any) -> IGLDocument:
    """Parse a dict (or JSON-ish structure) into a validated IGLDocument."""
    return IGLDocument.model_validate(data)


def serialize(doc: IGLDocument) -> dict[str, Any]:
    """Dump a validated IGLDocument back to a plain dict (JSON-safe)."""
    return doc.model_dump(mode="json", exclude_none=True)
