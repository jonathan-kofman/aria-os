"""ScanCatalogAgent — manage a catalog of scanned/reconstructed parts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

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
