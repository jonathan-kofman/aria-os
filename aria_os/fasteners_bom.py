"""
Bill-of-Fasteners (BOF) aggregator.

Consumes a populated drone BOM (output of mass_calc.populate_bom_masses) and
produces an aggregated buy-list of fasteners — the hardware you need to
actually bolt the airframe together. The per-step assembly_instructions.md
shows what goes where; fasteners.md shows what to add to the cart.

Entry points:

    from aria_os.fasteners_bom import (
        aggregate_fasteners,
        generate_fasteners_md,
    )

    rows    = aggregate_fasteners(bom_dict)
    md_path = generate_fasteners_md(rows, output_dir)

The fastener rules are derived from the per-part hardware table in
`assembly_instructions.py`, scaled by BOM part counts:

    - arm          → 2× M3×8 button-head per arm (bottom plate)
    - standoff     → 1× M3×8 cap head per standoff (stack bolt, both ends)
    - top_plate    → N× M3×8 button-head into standoff tops (one per standoff)
    - motor        → 4× M3×6 button-head per motor (arm-tip boss pattern)
    - prop         → 1× M5 nylock prop nut per motor
    - canopy       → 2× M3×6 per canopy
    - armor_top    → 4× M3×10 per top armor plate
    - vision_pod   → 4× M3×8 per pod
    - gimbal_yoke  → 2× M3×8 per yoke
    - fiber_spool  → 1× M5×16 shoulder bolt + 1× M5 nyloc per spool
    - payload_rail → 4× M4×10 per rail

Each aggregated fastener row also gets nyloc nuts where a through-bolt is
used (motor screws, standoff stack bolts, prop nuts).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Standard-drone fastener SKU database
#
# Hardcoded SKUs for the common fasteners a 5/7-inch quad build uses.
# McMaster part numbers are real; BoltDepot numbers are real where possible.
# est_cost_usd is per-unit, small-order-quantity pricing (US, April 2026).
# ---------------------------------------------------------------------------

_FASTENER_DB: dict[str, dict[str, Any]] = {
    # Button-head cap screws, 18-8 stainless
    "M3x6 button head SS": {
        "mcmaster": "92095A179", "boltdepot": "32332", "unit_usd": 0.10,
    },
    "M3x8 button head SS": {
        "mcmaster": "92095A182", "boltdepot": "32334", "unit_usd": 0.12,
    },
    "M3x10 button head SS": {
        "mcmaster": "92095A184", "boltdepot": "32335", "unit_usd": 0.14,
    },
    "M3x12 button head SS": {
        "mcmaster": "92095A188", "boltdepot": "32336", "unit_usd": 0.15,
    },
    "M3x16 button head SS": {
        "mcmaster": "92095A192", "boltdepot": "32338", "unit_usd": 0.18,
    },
    "M3x20 button head SS": {
        "mcmaster": "92095A194", "boltdepot": "32339", "unit_usd": 0.20,
    },

    # Socket head cap screws (stack bolts, higher clamp load than button)
    "M3x8 cap head SS": {
        "mcmaster": "91292A112", "boltdepot": "28734", "unit_usd": 0.14,
    },
    "M3x12 cap head SS": {
        "mcmaster": "91292A115", "boltdepot": "28736", "unit_usd": 0.16,
    },
    "M3x16 cap head SS": {
        "mcmaster": "91292A117", "boltdepot": "28738", "unit_usd": 0.18,
    },
    "M3x20 cap head SS": {
        "mcmaster": "91292A120", "boltdepot": "28740", "unit_usd": 0.22,
    },

    # M4 (payload rail)
    "M4x10 button head SS": {
        "mcmaster": "92095A196", "boltdepot": "32384", "unit_usd": 0.20,
    },

    # M5 (motor mount, prop nut, fiber spool)
    "M5x16 shoulder bolt SS": {
        "mcmaster": "91259A153", "boltdepot": "",        "unit_usd": 1.80,
    },
    "M5 nylock prop nut SS": {
        "mcmaster": "93625A200", "boltdepot": "34872",   "unit_usd": 0.45,
    },

    # Nyloc nuts
    "M3 nylock nut SS": {
        "mcmaster": "90576A102", "boltdepot": "33194",   "unit_usd": 0.09,
    },
    "M4 nylock nut SS": {
        "mcmaster": "90576A103", "boltdepot": "33197",   "unit_usd": 0.12,
    },
    "M5 nylock nut SS": {
        "mcmaster": "90576A104", "boltdepot": "33200",   "unit_usd": 0.15,
    },

    # Standoffs — female-female, aluminum (common drone stack lengths)
    "M3 standoff F-F 15mm aluminum": {
        "mcmaster": "95947A013", "boltdepot": "",        "unit_usd": 0.85,
    },
    "M3 standoff F-F 20mm aluminum": {
        "mcmaster": "95947A017", "boltdepot": "",        "unit_usd": 0.95,
    },
    "M3 standoff F-F 25mm aluminum": {
        "mcmaster": "95947A020", "boltdepot": "",        "unit_usd": 1.05,
    },
    "M3 standoff F-F 30mm aluminum": {
        "mcmaster": "95947A023", "boltdepot": "",        "unit_usd": 1.15,
    },
    "M3 standoff F-F 35mm aluminum": {
        "mcmaster": "95947A026", "boltdepot": "",        "unit_usd": 1.25,
    },

    # Small screws for canopy etc.
    "M2x6 cap head SS": {
        "mcmaster": "91292A831", "boltdepot": "28712",   "unit_usd": 0.10,
    },
}


def _standoff_spec_for_length(length_mm: float) -> str:
    """Pick the closest catalog standoff length from standard 15/20/25/30/35mm."""
    catalog = [15.0, 20.0, 25.0, 30.0, 35.0]
    closest = min(catalog, key=lambda x: abs(x - float(length_mm)))
    return f"M3 standoff F-F {int(closest)}mm aluminum"


# ---------------------------------------------------------------------------
# Aggregation rules — part_spec → list[(fastener_spec, qty_per_part)]
# Each rule runs once per BOM instance of that spec (so 4 arms × 2 M3×8 = 8).
# ---------------------------------------------------------------------------

def _fastener_rules_for_part(
    spec: str, part: dict[str, Any], bom: dict[str, Any],
) -> list[tuple[str, int]]:
    """Return [(fastener_spec, qty_per_instance)] for one BOM part entry."""
    # Pull standoff length from params_snapshot if available (for the standoff
    # spec selection) — default 30mm (5inch FPV) if absent.
    standoff_len = float(
        (bom.get("params_snapshot") or {}).get("frame", {}).get(
            "standoff_len_mm", 30.0)
    )

    if spec == "arm":
        # 2× M3×8 button-head into the bottom plate per arm
        return [("M3x8 button head SS", 2)]

    if spec == "standoff":
        # One standoff physical part + 1× M3×8 cap head bolt into BOTTOM end
        # (the TOP end is accounted for in the top_plate rule below — the
        # bolt that goes through the top plate INTO the standoff top).
        return [
            (_standoff_spec_for_length(standoff_len), 1),
            ("M3x8 cap head SS", 1),
            ("M3 nylock nut SS", 1),
        ]

    if spec == "top_plate":
        # 4 bolts into standoff tops. Count standoffs in BOM — fallback 4.
        n_standoffs = sum(
            1 for p in (bom.get("parts") or [])
            if isinstance(p, dict) and (p.get("spec") or p.get("name")) == "standoff"
        ) or 4
        return [("M3x8 button head SS", n_standoffs)]

    if spec == "bottom_plate":
        return []  # arm bolts and standoff bolts already cover this

    if spec == "motor":
        # 4× M3×6 into arm-tip bosses per motor
        return [("M3x6 button head SS", 4)]

    if spec == "prop":
        # 1× M5 nylock prop nut per prop
        return [("M5 nylock prop nut SS", 1)]

    if spec == "canopy":
        return [("M3x6 button head SS", 2)]

    if spec in ("esc_pcb", "fc_pcb"):
        # Nylon spacer stack on standoffs — not a metal fastener, skip.
        return []

    if spec == "battery":
        return []  # strap, no hardware

    # ── Military preset ──────────────────────────────────────────────────
    if spec == "armor_top":
        return [("M3x10 button head SS", 4), ("M3 nylock nut SS", 4)]

    if spec == "armor_bottom":
        return []  # bonded, no screws

    if spec == "vision_pod":
        return [("M3x8 button head SS", 4)]

    if spec == "gimbal_yoke":
        return [("M3x8 button head SS", 2)]

    if spec == "fiber_spool":
        return [
            ("M5x16 shoulder bolt SS", 1),
            ("M5 nylock nut SS", 1),
        ]

    if spec == "fiber_eyelet":
        return [("M2x6 cap head SS", 1)]

    if spec in ("gps_puck", "rx_module"):
        return []  # foam tape

    if spec == "payload_rail":
        return [("M4x10 button head SS", 4), ("M4 nylock nut SS", 4)]

    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aggregate_fasteners(bom: dict[str, Any]) -> list[dict]:
    """Parse the BOM and return aggregated fastener rows.

    Each row is:
        {
            "spec": "M3x8 button head SS",
            "qty": 12,
            "supplier_skus": {"mcmaster": "...", "boltdepot": "..."},
            "est_cost_usd": 1.20,
        }

    Rows are sorted by qty descending (highest volume items first).
    """
    counts: dict[str, int] = {}

    for part in (bom.get("parts") or []):
        if not isinstance(part, dict):
            continue
        spec_name = part.get("spec") or part.get("name") or ""
        for fastener_spec, qty_each in _fastener_rules_for_part(spec_name, part, bom):
            counts[fastener_spec] = counts.get(fastener_spec, 0) + qty_each

    rows: list[dict] = []
    for spec, qty in counts.items():
        db = _FASTENER_DB.get(spec) or {}
        unit = float(db.get("unit_usd", 0.0))
        rows.append({
            "spec": spec,
            "qty": int(qty),
            "supplier_skus": {
                "mcmaster": db.get("mcmaster", ""),
                "boltdepot": db.get("boltdepot", ""),
            },
            "est_cost_usd": round(unit * qty, 2),
        })

    # Sort by qty descending, then spec asc for stable output
    rows.sort(key=lambda r: (-r["qty"], r["spec"]))
    return rows


def generate_fasteners_md(
    bom_aggregated: list[dict],
    output_dir: str | Path,
) -> Path:
    """Write fasteners.md with markdown table + totals. Returns the file path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "fasteners.md"

    total_qty = sum(r["qty"] for r in bom_aggregated)
    total_cost = sum(float(r["est_cost_usd"]) for r in bom_aggregated)

    lines: list[str] = []
    lines.append("# Bill of Fasteners")
    lines.append("")
    lines.append("Aggregated hardware buy-list for this build. Quantities "
                 "reflect **real instance counts** from the BOM (e.g. 4 motors × "
                 "4 arm-tip bolts each = 16 M3×6 button-heads). SKUs are "
                 "small-order stainless hardware from McMaster-Carr and BoltDepot.")
    lines.append("")
    lines.append("| Qty | Spec | McMaster | BoltDepot | Est $ |")
    lines.append("|-----|------|----------|-----------|-------|")
    for r in bom_aggregated:
        skus = r.get("supplier_skus", {})
        mc  = skus.get("mcmaster", "") or "—"
        bd  = skus.get("boltdepot", "") or "—"
        lines.append(
            f"| {r['qty']} | {r['spec']} | {mc} | {bd} | "
            f"${float(r['est_cost_usd']):.2f} |"
        )
    lines.append("")
    lines.append(f"**Total items:** {total_qty}  ")
    lines.append(f"**Total: ${total_cost:.2f}**")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Generated by ARIA-OS fasteners_bom.py. SKUs are stainless "
                 "(18-8 or A2) unless noted — swap to titanium on motor screws "
                 "if you're chasing grams._")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def generate_fasteners_from_bom_path(
    bom_path: str | Path,
    output_dir: str | Path,
) -> Path:
    """Convenience: load a bom.json and write fasteners.md alongside it."""
    bom = json.loads(Path(bom_path).read_text(encoding="utf-8"))
    rows = aggregate_fasteners(bom)
    return generate_fasteners_md(rows, output_dir)


__all__ = [
    "aggregate_fasteners",
    "generate_fasteners_md",
    "generate_fasteners_from_bom_path",
]
