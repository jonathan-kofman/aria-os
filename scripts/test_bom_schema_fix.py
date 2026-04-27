"""Quick verification that catalog_parts expansion produces a populated
purchased[] list when run through generate_bom().

Builds a synthetic build_config.json matching what /api/system/full-build
now writes (post-fix), feeds it to generate_bom(), and prints the
purchased breakdown.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aria_os.components  # noqa: F401 — register catalog
from aria_os.assembly_bom import generate_bom


def _drone_catalog_match() -> list[dict]:
    """Mirrors dashboard/aria_server.py:_drone_catalog_match for a 5"
    quad with 4S 1500 mAh."""
    return [
        {"designation": "2207-1750KV",            "quantity": 4},
        {"designation": "5x4.3_3blade",           "quantity": 4},
        {"designation": "ESC_30A_BLHeli32_4in1",  "quantity": 1},
        {"designation": "LiPo_4S_1500mAh_120C",   "quantity": 1},
        {"designation": "M3x8_12.9",              "quantity": 16},
        {"designation": "M3x10_12.9",             "quantity": 4},
        {"designation": "M3x10_brass_standoff",   "quantity": 4},
        {"designation": "Velcro_strap_200x20mm",  "quantity": 1},
        {"designation": "XT60_connector",         "quantity": 1},
        {"designation": "GPS_M8N_module",         "quantity": 1},
        {"designation": "Telemetry_LoRa_433",     "quantity": 1},
        {"designation": "RC_receiver_ELRS_2.4G",  "quantity": 1},
        {"designation": "VTX_5.8GHz_400mW",       "quantity": 1},
        {"designation": "FPV_camera_micro",       "quantity": 1},
    ]


def main() -> int:
    bundle = ROOT / "outputs" / "system_builds" / "drone_ukraine_v2"
    frame_step = bundle / "drone_frame.step"
    pcb_step   = bundle / "fc_pcb.step"

    catalog_parts: list[dict] = []
    instance_idx = 0
    for cat_entry in _drone_catalog_match():
        for _ in range(int(cat_entry.get("quantity", 1))):
            instance_idx += 1
            catalog_parts.append({
                "id":         f"{cat_entry['designation']}__{instance_idx}",
                "component":  cat_entry["designation"],
                "pos":        [0, 0, 0],
                "rot":        [0, 0, 0],
            })

    config = {
        "name":   "drone_bom_schema_test",
        "preset": "fpv_drone",
        "parts": [
            {"id": "frame",  "step": str(frame_step),
             "pos": [0, 0, 0], "rot": [0, 0, 0], "fabricated": True},
            {"id": "fc_pcb", "step": str(pcb_step),
             "pos": [0, 0, 8], "rot": [0, 0, 0]},
            *catalog_parts,
        ],
    }
    test_dir = ROOT / "outputs" / "system_builds" / "drone_bom_schema_test"
    test_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = test_dir / "build_config.json"
    cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"[1/2] wrote {cfg_path}")
    print(f"      total parts in config: {len(config['parts'])}")
    print(f"      catalog instances:     {len(catalog_parts)}")

    bom = generate_bom(config, config_path=cfg_path)
    bom_path = test_dir / "bom.json"
    bom_path.write_text(json.dumps(bom, indent=2, default=str),
                          encoding="utf-8")
    print(f"[2/2] wrote {bom_path}")

    s = bom["summary"]
    print()
    print("===== BOM summary =====")
    print(f"  total_parts            : {s['total_parts']}")
    print(f"  fabricated_count       : {s['fabricated_count']}")
    print(f"  purchased_count        : {s['purchased_count']}")
    print(f"  unique_components      : {s['unique_components']}")
    print(f"  total_purchased_cost_$ : {s['total_purchased_cost_usd']}")
    print(f"  total_mass_g           : {s['total_mass_g']}")
    print()
    print("===== purchased[] =====")
    for row in bom["purchased"]:
        print(f"  {row.get('quantity'):>3} × {row.get('designation'):<28} "
              f"@ ${row.get('unit_cost_usd', 0):>6.2f} = "
              f"${row.get('total_cost_usd', 0):>8.2f}  "
              f"({row.get('total_mass_g', 0):>6.1f} g)")

    if s["purchased_count"] == 0:
        print("\n[FAIL] purchased[] still empty — schema fix did NOT work")
        return 1
    print(f"\n[PASS] {s['purchased_count']} purchased instances "
          f"across {s['unique_components']} SKUs — fix works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
