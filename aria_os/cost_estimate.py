"""
Total cost estimate per build — sums material + PCB fab + CNC + electronics
+ fasteners → headline `total_usd`. Turns "I generated stuff" into
"this drone costs $X to make."

Pricing sources (USD, mid-2026, conservative):
  - Filament: PETG ~$22/kg, ABS ~$25/kg, PLA ~$20/kg, PC ~$35/kg, PETG-CF ~$45/kg
  - PCB fab: JLCPCB 5pc minimum, 100×100mm 2-layer ~$2 + shipping ~$8 = ~$10/board
  - CNC milling: hobby 3-axis CNC ~$0.50/min; commercial ~$2/min. Use $1/min default
  - Electronics: per-component MSRP from catalog (Hobbyking / Banggood / GetFPV avg)
  - Fasteners: pulled from fasteners.md if present

Output:
  cost_breakdown.json with line-item costs + headline total_usd
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Filament cost in USD per kg, conservative retail
_FILAMENT_USD_PER_KG = {
    "petg":          22.0,
    "abs":           25.0,
    "asa":           28.0,
    "pla":           20.0,
    "polycarbonate": 35.0,
    "petg-cf":       45.0,
}

# CNC stock material cost in USD per kg
_CNC_STOCK_USD_PER_KG = {
    "aluminum_6061":  6.0,
    "aluminum_7075": 12.0,
    "aluminum":       6.0,
    "steel":          2.5,
    "steel_4140":     5.0,
    "stainless_steel": 8.0,
    "titanium":      40.0,
    "titanium_6al4v":40.0,
    "brass":          9.0,
    "cfrp":          80.0,    # pre-laminated CFRP plate stock
    "carbon_fiber":  80.0,
}

# Per-board PCB fab estimate (JLCPCB 5pc minimum, 2-layer, ≤100×100mm + ship)
PCB_FAB_USD_PER_BOARD = 10.0

# CNC machine time rate (USD per minute) — hobby 3-axis CNC default
CNC_USD_PER_MIN = 1.00

# Catalog electronics — typical retail per part (MSRP avg from GetFPV / RDQ / Banggood)
_ELECTRONICS_USD = {
    # MCUs / FCs (when bare chip)
    "stm32f405":  4.50,
    "stm32f411":  3.20,
    "stm32f722":  6.50,
    "stm32f745":  8.20,
    "esp32":      4.00,
    "esp32-s3":   5.50,
    "rp2040":     1.20,
    "atmega328":  2.00,
    # Sensors
    "mpu-6000":   3.50,
    "mpu-6050":   2.50,
    "mpu-9250":   5.50,
    "icm-20602":  4.00,
    "bmi270":     5.00,
    "bmp280":     1.20,
    "bmp180":     0.80,
    "ms5611":     4.00,
    "qmc5883l":   1.50,
    "hmc5883l":   2.00,
    # Power
    "ams1117-3.3": 0.20,
    "ap2112":      0.40,
    "tp4056":      0.30,
    # Connectors
    "usb-c-receptacle": 0.80,
    "jst-ph-2p":   0.20,
    "jst-xh-2p":   0.25,
    "jst-xh-4p":   0.40,
    "jst-xh-6p":   0.55,
    "jst-gh-6p":   0.65,
    "xt30-2p":     1.20,
    "xt60-2p":     2.00,
    "xt90-2p":     3.50,
    "barrel-jack": 0.50,
    "kf350":       0.40,
    "molex":       1.50,
    # Drone-specific assemblies (pre-built modules — typical retail)
    "elrs":       18.0,    # ELRS receiver module
    "vtx":        25.0,    # video transmitter
    "fpv-camera": 22.0,
    "thermal-camera": 180.0, # FLIR Lepton
    "gps-m8n":    18.0,
    "fc-board":   45.0,    # mid-tier flight controller, assembled
    "esc-4in1":   55.0,    # 4-in-1 ESC, assembled
    "motor-2306": 22.0,    # per motor
    "prop-5":     1.00,    # per prop
    "prop-7":     2.00,
    "lipo-4s-1500": 25.0,
    "lipo-4s-2200": 32.0,
    # Catch-all passives — average per
    "default_passive": 0.05,
}


def estimate_cost(bom_path: str | Path,
                  preset_id: str | None = None) -> dict[str, Any]:
    """Compute total cost breakdown for a built bundle.

    Inputs:
      - bom_path: outputs/.../bom.json (the mechanical BOM with mass_g)
      - preset_id: used to estimate catalog electronics (e.g. military
        adds GPS + Rx + VTX + fiber spool electronics)

    Walks the bundle directory:
      - Print costs from print_summary.json (if present)
      - PCB fab from ecad/*/bom.json count
      - CNC from cam/*/cam_summary.json times
      - Electronics from BOM material classes + preset
      - Fasteners from fasteners.md (if present)

    Returns dict with line items + total_usd. Writes cost_breakdown.json
    to the bundle directory.
    """
    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    bundle_dir = bom_path.parent
    breakdown: dict[str, Any] = {
        "bundle_dir": str(bundle_dir),
        "preset_id": preset_id,
        "lines": [],
        "totals": {},
    }

    # ── 1. Print material cost (PETG/ABS/PC printed parts) ──────────────────
    print_summary_path = bundle_dir / "print" / "print_summary.json"
    print_total = 0.0
    if print_summary_path.is_file():
        try:
            ps = json.loads(print_summary_path.read_text(encoding="utf-8"))
            for part in ps.get("parts", []):
                fil = (part.get("filament") or "").lower()
                price_per_kg = _FILAMENT_USD_PER_KG.get(fil)
                if price_per_kg is None:
                    # Try by material class
                    price_per_kg = next(
                        (v for k, v in _FILAMENT_USD_PER_KG.items() if k in fil),
                        20.0,  # default
                    )
                grams = float(part.get("estimated_grams", 0))
                cost = (grams / 1000.0) * price_per_kg
                if cost > 0:
                    breakdown["lines"].append({
                        "category": "print",
                        "item": f"{part['name']} ({fil}, {grams:.1f}g)",
                        "qty": 1, "unit_usd": price_per_kg,
                        "cost_usd": round(cost, 2),
                    })
                    print_total += cost
        except Exception as exc:
            breakdown["lines"].append({"category": "print",
                                       "item": f"failed to read: {exc}",
                                       "cost_usd": 0.0})
    breakdown["totals"]["print_usd"] = round(print_total, 2)

    # ── 2. CNC stock + machine time ─────────────────────────────────────────
    cam_dir = bundle_dir / "cam"
    cnc_total = 0.0
    if cam_dir.is_dir():
        # Sum estimated cycle time across CAM scripts
        for summary_file in cam_dir.rglob("*_cam_summary.json"):
            try:
                cs = json.loads(summary_file.read_text(encoding="utf-8"))
                cycle_min = float(cs.get("estimated_cycle_min", 0))
                machine_cost = cycle_min * CNC_USD_PER_MIN
                if machine_cost > 0:
                    breakdown["lines"].append({
                        "category": "cnc",
                        "item": f"{summary_file.parent.name} machine time",
                        "qty": 1, "unit_usd": CNC_USD_PER_MIN,
                        "cost_usd": round(machine_cost, 2),
                    })
                    cnc_total += machine_cost
            except Exception:
                pass

    # CNC stock material from BOM (parts marked CFRP / aluminum / etc.)
    for p in bom.get("parts", []):
        if not isinstance(p, dict):
            continue
        material = (p.get("material") or "").lower()
        mass_g = float(p.get("mass_g", 0))
        price_per_kg = _CNC_STOCK_USD_PER_KG.get(material)
        if price_per_kg and mass_g > 0:
            cost = (mass_g / 1000.0) * price_per_kg
            breakdown["lines"].append({
                "category": "cnc_stock",
                "item": f"{p.get('name', '?')} ({material}, {mass_g:.1f}g)",
                "qty": 1, "unit_usd": price_per_kg,
                "cost_usd": round(cost, 2),
            })
            cnc_total += cost
    breakdown["totals"]["cnc_usd"] = round(cnc_total, 2)

    # ── 3. PCB fabrication (per-board flat fee, assumes JLCPCB) ─────────────
    pcb_total = 0.0
    n_boards = len(list(bundle_dir.glob("ecad/*/")))
    if n_boards > 0:
        cost = n_boards * PCB_FAB_USD_PER_BOARD
        breakdown["lines"].append({
            "category": "pcb_fab",
            "item": f"JLCPCB 5pc 2-layer (×{n_boards} board{'s' if n_boards>1 else ''})",
            "qty": n_boards, "unit_usd": PCB_FAB_USD_PER_BOARD,
            "cost_usd": cost,
        })
        pcb_total = cost
    breakdown["totals"]["pcb_usd"] = round(pcb_total, 2)

    # ── 4. Electronics catalog parts (estimated from preset + BOM hints) ─────
    electronics_total = 0.0
    # Common per-preset electronics (pre-built modules)
    if preset_id:
        bundle_parts = []
        if "5inch" in preset_id or "fpv" in preset_id:
            bundle_parts = [
                ("fc-board", 1), ("esc-4in1", 1), ("motor-2306", 4),
                ("prop-5", 4), ("lipo-4s-1500", 1), ("elrs", 1),
                ("fpv-camera", 1), ("vtx", 1),
            ]
        elif "7inch" in preset_id or "long_range" in preset_id:
            bundle_parts = [
                ("fc-board", 1), ("esc-4in1", 1), ("motor-2306", 4),
                ("prop-7", 4), ("lipo-4s-2200", 1), ("elrs", 1),
                ("fpv-camera", 1), ("vtx", 1),
            ]
        elif "military" in preset_id:
            bundle_parts = [
                ("fc-board", 1), ("esc-4in1", 1), ("motor-2306", 4),
                ("prop-7", 4), ("lipo-4s-2200", 1), ("elrs", 1),
                ("fpv-camera", 1), ("thermal-camera", 1), ("gps-m8n", 1),
                ("vtx", 1),
            ]
        for sku, qty in bundle_parts:
            unit = _ELECTRONICS_USD.get(sku, 0)
            cost = unit * qty
            if cost > 0:
                breakdown["lines"].append({
                    "category": "electronics",
                    "item": f"{sku} × {qty}",
                    "qty": qty, "unit_usd": unit,
                    "cost_usd": round(cost, 2),
                })
                electronics_total += cost
    breakdown["totals"]["electronics_usd"] = round(electronics_total, 2)

    # ── 5. Fasteners (read from fasteners.md if present) ────────────────────
    fastener_total = 0.0
    fasteners_md = bundle_dir / "fasteners.md"
    if fasteners_md.is_file():
        try:
            text = fasteners_md.read_text(encoding="utf-8")
            # Parse markdown table rows for "$X.XX" totals at end
            for m in re.finditer(r"\|\s*\$?([\d.]+)\s*\|", text):
                try:
                    fastener_total += float(m.group(1))
                except Exception:
                    pass
            # Heuristic: if a "Total: $X.XX" line exists, prefer it
            tot_match = re.search(r"\*\*Total[^$]*\$([\d.]+)", text)
            if tot_match:
                fastener_total = float(tot_match.group(1))
        except Exception:
            pass
    if fastener_total == 0.0:
        # Fallback estimate: $5 of fasteners for a typical drone
        fastener_total = 5.0
        breakdown["lines"].append({
            "category": "fasteners",
            "item": "Hardware kit (M3/M5 mix, estimated)",
            "qty": 1, "unit_usd": 5.0, "cost_usd": 5.0,
        })
    else:
        breakdown["lines"].append({
            "category": "fasteners",
            "item": "Hardware (from fasteners.md)",
            "qty": 1, "unit_usd": fastener_total, "cost_usd": fastener_total,
        })
    breakdown["totals"]["fasteners_usd"] = round(fastener_total, 2)

    # ── Total ───────────────────────────────────────────────────────────────
    total = (print_total + cnc_total + pcb_total +
             electronics_total + fastener_total)
    breakdown["totals"]["total_usd"] = round(total, 2)
    breakdown["headline"] = f"${total:.2f}"

    # Write to bundle for downstream consumption (UI display, MillForge)
    out = bundle_dir / "cost_breakdown.json"
    out.write_text(json.dumps(breakdown, indent=2), encoding="utf-8")
    breakdown["cost_breakdown_path"] = str(out)
    return breakdown


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m aria_os.cost_estimate <bom.json> [preset_id]")
        sys.exit(1)
    preset = sys.argv[2] if len(sys.argv) > 2 else None
    r = estimate_cost(sys.argv[1], preset_id=preset)
    print(f"Total: ${r['totals']['total_usd']}")
    for cat, v in r["totals"].items():
        if cat != "total_usd":
            print(f"  {cat:18s} ${v}")
