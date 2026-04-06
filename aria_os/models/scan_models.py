"""Data models for the scan-to-CAD reverse pipeline."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class BoundingBox:
    x: float  # mm
    y: float  # mm
    z: float  # mm

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "z": self.z}


@dataclass
class CleanedMesh:
    vertices: int
    faces: int
    bounding_box: BoundingBox
    volume_mm3: float
    surface_area_mm2: float
    watertight: bool
    file_path: str
    center_of_mass: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class DetectedPrimitive:
    type: str  # "plane", "cylinder", "sphere", "cone"
    parameters: Dict[str, Any]  # type-specific params
    surface_area_mm2: float
    inlier_count: int  # number of mesh points belonging to this primitive
    confidence: float = 0.0  # fit quality 0-1


@dataclass
class PartFeatureSet:
    primitives: List[DetectedPrimitive]
    topology: str  # "turned_part", "prismatic", "freeform"
    coverage: float  # 0-1, fraction of surface explained by primitives
    confidence: float  # 0-1, overall reconstruction confidence
    # Backend-agnostic parametric description — dict of part parameters
    # that any CAD backend can consume (dimensions, hole positions, etc.)
    parametric_description: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    scan_date: str = field(default_factory=lambda: datetime.now().isoformat())
    source_file: str = ""
    bounding_box: Optional[BoundingBox] = None
    volume_mm3: float = 0.0
    material: str = "unknown"
    topology: str = "unknown"
    primitives_summary: List[dict] = field(default_factory=list)
    stl_path: str = ""
    features_path: str = ""
    confidence: float = 0.0
    tags: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "scan_date": self.scan_date,
            "source_file": self.source_file,
            "bounding_box": self.bounding_box.as_dict() if self.bounding_box else None,
            "volume_mm3": self.volume_mm3,
            "material": self.material,
            "topology": self.topology,
            "primitives_summary": self.primitives_summary,
            "stl_path": self.stl_path,
            "features_path": self.features_path,
            "confidence": self.confidence,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CatalogEntry":
        bb = d.get("bounding_box")
        return cls(
            id=d.get("id", str(uuid.uuid4())[:8]),
            scan_date=d.get("scan_date", ""),
            source_file=d.get("source_file", ""),
            bounding_box=BoundingBox(**bb) if bb else None,
            volume_mm3=d.get("volume_mm3", 0.0),
            material=d.get("material", "unknown"),
            topology=d.get("topology", "unknown"),
            primitives_summary=d.get("primitives_summary", []),
            stl_path=d.get("stl_path", ""),
            features_path=d.get("features_path", ""),
            confidence=d.get("confidence", 0.0),
            tags=d.get("tags", []),
        )
