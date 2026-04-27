"""Verify a /api/system/full-build bundle has all expected artifacts.

Usage:
    python scripts/verify_bundle.py drone_ukraine_v9
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXPECTED = [
    ("STEP — combined assembly",   "assembly.step",            10_000),
    ("STEP — frame",               "drone_frame.step",         10_000),
    ("STEP — flight controller",   "fc_pcb.step",              50_000),
    ("BOM (mechanical+electrical)","bom.json",                 1_000),
    ("Build config",               "build_config.json",        500),
    ("Assembly instructions",      "assembly_instructions.md", 1_000),
    ("Design rationale",           "design_rationale.md",      4_000),
    ("eBOM (KiCad)",               "ebom.csv",                 100),
    ("PCB fab drawing (PDF)",      "fc_pcb_fab.pdf",           5_000),
    ("PCB fab DXF + GD&T",         "fc_pcb_fab_gdt.dxf",       10_000),
    ("Frame DXF",                  "drone_frame.dxf",          5_000),
    ("Frame DWG",                  "drone_frame.dwg",          5_000),
    ("Frame DXF + GD&T",           "drone_frame_gdt.dxf",      10_000),
]


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "drone_ukraine_v9"
    bundle = ROOT / "outputs" / "system_builds" / name
    if not bundle.is_dir():
        print(f"[FAIL] bundle not found: {bundle}")
        return 1
    print(f"=== Verifying {bundle.name} ===\n")

    missing: list[str] = []
    too_small: list[str] = []
    for label, fname, min_size in EXPECTED:
        p = bundle / fname
        if not p.is_file():
            print(f"  [MISS]  {label:<32} {fname}")
            missing.append(fname)
            continue
        size = p.stat().st_size
        if size < min_size:
            print(f"  [SMALL] {label:<32} {fname:<32} {size} < {min_size} bytes")
            too_small.append(fname)
        else:
            print(f"  [OK]    {label:<32} {fname:<32} {size:>10,} bytes")

    print()

    # BOM evidence
    bom_path = bundle / "bom.json"
    if bom_path.is_file():
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
        s = bom.get("summary", {})
        print(f"  BOM evidence:")
        print(f"    Total parts            : {s.get('total_parts')}")
        print(f"    Purchased instances    : {s.get('purchased_count')}")
        print(f"    Unique SKUs            : {s.get('unique_components')}")
        print(f"    Total purchased $      : ${s.get('total_purchased_cost_usd')}")
        print(f"    Total mass             : {s.get('total_mass_g')} g")
        print(f"    Export classification  : "
              f"{(bom.get('export_control') or {}).get('overall_classification')}")
        if s.get('purchased_count', 0) == 0:
            print("    [FAIL] purchased[] is empty")
            missing.append("bom.purchased")

    # Rationale evidence — key sections present
    md_path = bundle / "design_rationale.md"
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        sections = [
            "Performance summary",
            "Component selection",
            "Manufacturing tolerances",
            "PCB design-rule check",
            "Export-control",
            "References & standards",
        ]
        print()
        print(f"  Rationale sections present:")
        for s in sections:
            present = s in text
            print(f"    [{'OK' if present else 'MISS'}] {s}")
            if not present:
                missing.append(f"rationale§{s}")

    print()
    if missing or too_small:
        print(f"[FAIL] missing={len(missing)} too_small={len(too_small)}")
        return 1
    print("[PASS] all expected artifacts present and non-trivial")
    return 0


if __name__ == "__main__":
    sys.exit(main())
