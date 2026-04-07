"""ScanCatalogAgent — manage a catalog of scanned/reconstructed parts."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .. import event_bus
from ..models.scan_models import CatalogEntry


_DEFAULT_CATALOG = "outputs/scan_catalog/catalog.json"


class ScanCatalogAgent:
    """
    Stores and searches a JSON-based catalog of scanned parts.
    Each part gets a CatalogEntry with metadata, primitives summary, and file paths.
    """

    def __init__(self, catalog_path: Optional[str | Path] = None):
        self.catalog_path = Path(catalog_path or _DEFAULT_CATALOG)
        self._entries: dict[str, CatalogEntry] = {}
        self._load()

    def _load(self):
        if self.catalog_path.exists():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                for d in data:
                    entry = CatalogEntry.from_dict(d)
                    self._entries[entry.id] = entry
            except Exception:
                self._entries = {}

    def _save(self):
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        data = [e.as_dict() for e in self._entries.values()]
        self.catalog_path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    def add(self, entry: CatalogEntry) -> CatalogEntry:
        self._entries[entry.id] = entry
        self._save()
        event_bus.emit("scan", f"[ScanCatalog] Cataloged: {entry.id} ({entry.topology})",
                       {"part_id": entry.id, "topology": entry.topology})
        return entry

    def get(self, part_id: str) -> Optional[CatalogEntry]:
        return self._entries.get(part_id)

    def delete(self, part_id: str) -> bool:
        if part_id in self._entries:
            del self._entries[part_id]
            self._save()
            event_bus.emit("scan", f"[ScanCatalog] Deleted: {part_id}")
            return True
        return False

    def update(self, part_id: str, material: Optional[str] = None,
               tags: Optional[List[str]] = None) -> Optional[CatalogEntry]:
        entry = self._entries.get(part_id)
        if entry is None:
            return None
        if material is not None:
            entry.material = material
        if tags is not None:
            entry.tags = tags
        self._save()
        return entry

    def list_all(self) -> List[CatalogEntry]:
        return list(self._entries.values())

    def search(
        self,
        min_dims: Optional[tuple[float, float, float]] = None,
        max_dims: Optional[tuple[float, float, float]] = None,
        topology: Optional[str] = None,
        tags: Optional[List[str]] = None,
        has_primitive: Optional[str] = None,
    ) -> List[CatalogEntry]:
        """
        Search catalog entries by criteria.

        min_dims/max_dims: (x, y, z) in mm — filters bounding box
        topology: exact match on topology classification
        tags: entries must have ALL specified tags
        has_primitive: entries must have this primitive type in their summary
        """
        results = list(self._entries.values())

        if topology:
            results = [e for e in results if e.topology == topology]

        if tags:
            tag_set = set(tags)
            results = [e for e in results if tag_set.issubset(set(e.tags))]

        if has_primitive:
            results = [
                e for e in results
                if any(p.get("type") == has_primitive for p in e.primitives_summary)
            ]

        if min_dims:
            results = [
                e for e in results
                if e.bounding_box and
                e.bounding_box.x >= min_dims[0] and
                e.bounding_box.y >= min_dims[1] and
                e.bounding_box.z >= min_dims[2]
            ]

        if max_dims:
            results = [
                e for e in results
                if e.bounding_box and
                e.bounding_box.x <= max_dims[0] and
                e.bounding_box.y <= max_dims[1] and
                e.bounding_box.z <= max_dims[2]
            ]

        event_bus.emit("scan", f"[ScanCatalog] Search: {len(results)} results")
        return results

    def search_by_size(self, x: float, y: float, z: float,
                       tolerance: float = 0.1) -> List[CatalogEntry]:
        """Find parts within ±tolerance (fraction) of target dimensions."""
        return self.search(
            min_dims=(x * (1 - tolerance), y * (1 - tolerance), z * (1 - tolerance)),
            max_dims=(x * (1 + tolerance), y * (1 + tolerance), z * (1 + tolerance)),
        )

    def find_similar(
        self,
        target_dims: Optional[Tuple[float, float, float]] = None,
        target_volume: Optional[float] = None,
        target_primitives: Optional[dict[str, int]] = None,
        top_n: int = 3,
    ) -> List[Tuple[CatalogEntry, float]]:
        """
        Find the most similar catalog entries using a combined distance metric.

        Metric combines (weights sum to 1.0):
          - Bounding box similarity: normalized L2 on sorted dims (weight 0.5)
          - Primitive signature: cosine similarity on [planes, cylinders, spheres, cones] (weight 0.3)
          - Volume ratio: abs(1 - v_entry/v_target), closer to 0 = better (weight 0.2)

        Returns list of (entry, score) tuples sorted by score descending (1.0 = perfect match).
        """
        if not self._entries:
            return []

        scored: List[Tuple[CatalogEntry, float]] = []

        for entry in self._entries.values():
            score = self._similarity_score(entry, target_dims, target_volume, target_primitives)
            scored.append((entry, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    def _similarity_score(
        self,
        entry: CatalogEntry,
        target_dims: Optional[Tuple[float, float, float]],
        target_volume: Optional[float],
        target_primitives: Optional[dict[str, int]],
    ) -> float:
        """Compute similarity score (0-1) between an entry and target criteria."""
        scores = []
        weights = []

        # Bounding box similarity (weight 0.5)
        if target_dims and entry.bounding_box:
            target_sorted = sorted(target_dims, reverse=True)
            entry_sorted = sorted([entry.bounding_box.x, entry.bounding_box.y, entry.bounding_box.z], reverse=True)
            # Normalize by the larger of each pair to get 0-1 range
            dim_diffs = []
            for t, e in zip(target_sorted, entry_sorted):
                denom = max(t, e, 1e-9)
                dim_diffs.append(abs(t - e) / denom)
            # Average normalized difference → convert to similarity
            bbox_sim = 1.0 - (sum(dim_diffs) / 3.0)
            bbox_sim = max(0.0, bbox_sim)
            scores.append(bbox_sim)
            weights.append(0.5)

        # Primitive signature similarity (weight 0.3)
        if target_primitives:
            entry_prims = _primitives_count_vector(entry.primitives_summary)
            target_vec = [
                target_primitives.get("plane", 0),
                target_primitives.get("cylinder", 0),
                target_primitives.get("sphere", 0),
                target_primitives.get("cone", 0),
            ]
            prim_sim = _cosine_similarity(target_vec, entry_prims)
            scores.append(prim_sim)
            weights.append(0.3)

        # Volume ratio similarity (weight 0.2)
        if target_volume and target_volume > 0 and entry.volume_mm3 > 0:
            ratio = entry.volume_mm3 / target_volume
            vol_sim = 1.0 - min(abs(1.0 - ratio), 1.0)
            scores.append(vol_sim)
            weights.append(0.2)

        if not scores:
            return 0.0

        # Weighted average, renormalized to account for missing components
        total_weight = sum(weights)
        return sum(s * w for s, w in zip(scores, weights)) / total_weight


def _primitives_count_vector(primitives_summary: list) -> list:
    """Convert primitives_summary to [planes, cylinders, spheres, cones] count vector."""
    counts = {"plane": 0, "cylinder": 0, "sphere": 0, "cone": 0}
    for p in primitives_summary:
        ptype = p.get("type", "")
        if ptype in counts:
            counts[ptype] += p.get("count", 1)
    return [counts["plane"], counts["cylinder"], counts["sphere"], counts["cone"]]


def _cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity between two vectors. Returns 0-1."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a < 1e-9 or mag_b < 1e-9:
        return 0.0
    return max(0.0, dot / (mag_a * mag_b))


def parse_search_description(description: str) -> dict:
    """
    Parse a natural-language description into search criteria.

    Examples:
        "75x45x12 bracket with 4 holes" → dims=(75,45,12), primitives={plane:6, cylinder:4}
        "50mm diameter shaft"            → dims=(50,50,50), primitives={cylinder:1}
        "small bracket"                  → dims=None, primitives={plane:6}
    """
    result: dict = {"dims": None, "volume": None, "primitives": {}}

    # Extract dimensions: "75x45x12", "75 x 45 x 12", "75mm x 45mm x 12mm"
    dim_match = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:mm)?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[xX×]\s*(\d+(?:\.\d+)?)',
        description,
    )
    if dim_match:
        result["dims"] = (float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3)))
        d = result["dims"]
        result["volume"] = d[0] * d[1] * d[2]

    # Extract hole count: "4 holes", "with 6 holes", "2 bores"
    hole_match = re.search(r'(\d+)\s*(?:holes?|bores?|through.?holes?)', description, re.IGNORECASE)
    n_holes = int(hole_match.group(1)) if hole_match else 0

    # Infer primitives from keywords
    desc_lower = description.lower()
    prims = result["primitives"]

    if any(kw in desc_lower for kw in ("bracket", "box", "plate", "block", "prismatic", "rectangular")):
        prims["plane"] = 6  # box has 6 faces
    if any(kw in desc_lower for kw in ("shaft", "cylinder", "rod", "tube", "pipe", "turned", "round")):
        prims["cylinder"] = prims.get("cylinder", 0) + 1
    if any(kw in desc_lower for kw in ("sphere", "ball", "dome")):
        prims["sphere"] = 1
    if n_holes:
        prims["cylinder"] = prims.get("cylinder", 0) + n_holes

    # Extract diameter: "50mm diameter", "dia 25"
    dia_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:dia(?:meter)?|dia\.)', description, re.IGNORECASE)
    if dia_match and not dim_match:
        d = float(dia_match.group(1))
        result["dims"] = (d, d, d)  # rough approximation for round parts

    return result
