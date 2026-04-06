"""Scan-to-CAD reverse pipeline orchestrator.

Usage:
    from aria_os.scan_pipeline import run_scan_pipeline
    entry = run_scan_pipeline("part.stl")

Or via CLI:
    python run_aria_os.py --scan part.stl
    python run_aria_os.py --scan part.stl --material aluminium_6061 --tags "bracket,legacy"
    python run_aria_os.py --catalog
    python run_aria_os.py --catalog --topology prismatic
    python run_aria_os.py --catalog --search "50x30x20"
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from . import event_bus
from .models.scan_models import BoundingBox, CatalogEntry, PartFeatureSet


def run_scan_pipeline(
    scan_path: str | Path,
    material: str = "unknown",
    tags: Optional[List[str]] = None,
    output_dir: Optional[str | Path] = None,
    catalog_path: Optional[str | Path] = None,
) -> CatalogEntry:
    """
    Full scan-to-CAD pipeline:
      load mesh → clean → extract features → catalog

    Returns a CatalogEntry with all metadata and file paths.
    """
    scan_path = Path(scan_path)
    if not scan_path.exists():
        raise FileNotFoundError(f"Scan file not found: {scan_path}")

    event_bus.emit("scan", f"[ScanPipeline] Starting: {scan_path.name}")

    # Determine output directory
    part_stem = scan_path.stem
    if output_dir is None:
        output_dir = Path("outputs/scan_catalog") / part_stem
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Load and clean mesh
    from .agents.mesh_interpret_agent import MeshInterpretAgent

    mesh_agent = MeshInterpretAgent(output_dir=output_dir)
    cleaned = mesh_agent.run(scan_path)

    # Step 2: Extract features
    features: Optional[PartFeatureSet] = None
    try:
        from .agents.feature_extraction_agent import FeatureExtractionAgent

        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)
    except Exception as exc:
        event_bus.emit("scan", f"[ScanPipeline] Feature extraction failed: {exc}")
        # Continue with degraded entry

    # Save features JSON
    features_path = str(output_dir / "features.json")
    if features:
        _save_features(features, features_path)

    # Step 3: Build catalog entry
    prims_summary = []
    if features:
        # Summarize primitives by type
        type_counts: dict[str, int] = {}
        type_dims: dict[str, list] = {}
        for p in features.primitives:
            type_counts[p.type] = type_counts.get(p.type, 0) + 1
            if p.type == "cylinder":
                type_dims.setdefault("cylinder", []).append(
                    round(p.parameters.get("radius_mm", 0) * 2, 2)
                )
            elif p.type == "plane":
                type_dims.setdefault("plane", []).append(
                    round(p.parameters.get("extent_u_mm", 0), 2)
                )
        for ptype, count in type_counts.items():
            entry = {"type": ptype, "count": count}
            if ptype in type_dims:
                entry["dimensions_mm"] = sorted(set(type_dims[ptype]))
            prims_summary.append(entry)

    entry = CatalogEntry(
        source_file=scan_path.name,
        bounding_box=cleaned.bounding_box,
        volume_mm3=cleaned.volume_mm3,
        material=material,
        topology=features.topology if features else "unknown",
        primitives_summary=prims_summary,
        stl_path=cleaned.file_path,
        features_path=features_path if features else "",
        confidence=features.confidence if features else 0.0,
        tags=tags or [],
    )

    # Step 4: Add to catalog
    from .agents.scan_catalog_agent import ScanCatalogAgent

    catalog = ScanCatalogAgent(catalog_path=catalog_path)
    catalog.add(entry)

    event_bus.emit("scan",
                   f"[ScanPipeline] Complete: {entry.id} — {entry.topology} "
                   f"({cleaned.bounding_box.x}x{cleaned.bounding_box.y}x{cleaned.bounding_box.z}mm, "
                   f"confidence={entry.confidence:.0%})",
                   {"part_id": entry.id})

    # Print summary
    print(f"\n  [SCAN] {'=' * 56}")
    print(f"  [SCAN] Part ID:    {entry.id}")
    print(f"  [SCAN] Source:     {entry.source_file}")
    print(f"  [SCAN] Dimensions: {cleaned.bounding_box.x} x {cleaned.bounding_box.y} x {cleaned.bounding_box.z} mm")
    print(f"  [SCAN] Volume:     {cleaned.volume_mm3:.1f} mm³")
    print(f"  [SCAN] Watertight: {cleaned.watertight}")
    print(f"  [SCAN] Topology:   {entry.topology}")
    print(f"  [SCAN] Confidence: {entry.confidence:.0%}")
    if prims_summary:
        print(f"  [SCAN] Primitives:")
        for ps in prims_summary:
            dims_str = f" — dims: {ps['dimensions_mm']}mm" if "dimensions_mm" in ps else ""
            print(f"  [SCAN]   {ps['count']}x {ps['type']}{dims_str}")
    print(f"  [SCAN] Cleaned:    {cleaned.file_path}")
    if features:
        print(f"  [SCAN] Features:   {features_path}")
    print(f"  [SCAN] {'=' * 56}\n")

    return entry


def _save_features(features: PartFeatureSet, path: str):
    """Save feature set to JSON."""
    data = {
        "topology": features.topology,
        "coverage": features.coverage,
        "confidence": features.confidence,
        "parametric_description": features.parametric_description,
        "primitives": [
            {
                "type": p.type,
                "parameters": p.parameters,
                "surface_area_mm2": p.surface_area_mm2,
                "inlier_count": p.inlier_count,
                "confidence": p.confidence,
            }
            for p in features.primitives
        ],
    }
    Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def list_catalog(
    catalog_path: Optional[str | Path] = None,
    topology: Optional[str] = None,
    tags: Optional[List[str]] = None,
    search_dims: Optional[str] = None,
) -> List[CatalogEntry]:
    """List/search the catalog. Used by CLI."""
    from .agents.scan_catalog_agent import ScanCatalogAgent

    catalog = ScanCatalogAgent(catalog_path=catalog_path)

    if search_dims:
        # Parse "50x30x20" format
        parts = search_dims.lower().replace("mm", "").split("x")
        if len(parts) == 3:
            x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
            entries = catalog.search_by_size(x, y, z)
        else:
            print(f"[CATALOG] Invalid dimensions format: {search_dims} (expected: 50x30x20)")
            entries = catalog.list_all()
    elif topology or tags:
        entries = catalog.search(topology=topology, tags=tags)
    else:
        entries = catalog.list_all()

    if not entries:
        print("[CATALOG] No parts found.")
        return entries

    print(f"\n  {'ID':<10} {'Dims (mm)':<22} {'Topology':<14} {'Conf':>5}  {'Material':<16} {'Tags'}")
    print(f"  {'—' * 10} {'—' * 22} {'—' * 14} {'—' * 5}  {'—' * 16} {'—' * 20}")
    for e in entries:
        bb = f"{e.bounding_box.x}x{e.bounding_box.y}x{e.bounding_box.z}" if e.bounding_box else "?"
        tags_str = ", ".join(e.tags) if e.tags else ""
        print(f"  {e.id:<10} {bb:<22} {e.topology:<14} {e.confidence:>4.0%}  {e.material:<16} {tags_str}")
    print()

    return entries
