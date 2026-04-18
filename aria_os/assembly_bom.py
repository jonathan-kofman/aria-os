"""
Assembly BOM + manufacturing handoff.

Phase 5 + 6 of the complex-assembly plan:

- **BOM generation**: walk a hierarchical assembly config and produce a bill of
  materials separating purchased (catalog components) from fabricated (raw STEP files).
- **Multi-domain integration**: assembly configs can reference ECAD outputs
  (KiCad PCB files). Auto-generate an enclosure sized to the board and include
  both the PCB STEP and the enclosure STEP as parts.
- **MillForge handoff**: convert BOM + fabricated parts into a `MillForgeJob`
  list using `manufacturing_core.types`.

Example hierarchical config with ECAD:

    {
      "name": "sensor_module",
      "parts": [
        {"id": "controller", "ecad": "outputs/ecad/controller/board.kicad_pcb",
         "auto_enclosure": true},
        {"id": "mounting_bolts", "component": "M3x8_12.9", "quantity": 4}
      ]
    }

`quantity` on a component entry multiplies the BOM row but instantiates only
one part in the geometry (same component reused at different positions is
modeled via separate part entries with unique ids).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# ECAD integration — resolve "ecad:" parts into STEP files
# ---------------------------------------------------------------------------

def resolve_ecad_parts(
    config: dict[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """
    Walk a config and resolve any "ecad" parts to concrete STEP files.
    Auto-generates an enclosure if "auto_enclosure" is set.

    Mutates the config in-place and returns it.
    """
    repo_root = Path(__file__).resolve().parent.parent
    for part in config.get("parts", []):
        ecad_ref = part.get("ecad")
        if not ecad_ref:
            continue

        pcb_path = Path(ecad_ref)
        if not pcb_path.is_absolute() and config_path is not None:
            pcb_path = (config_path.parent / pcb_path).resolve()
        if not pcb_path.is_absolute():
            pcb_path = (repo_root / pcb_path).resolve()

        if not pcb_path.is_file():
            raise FileNotFoundError(f"ECAD file not found: {pcb_path}")

        # Generate the enclosure (which also produces a board STEP if we need it)
        if part.get("auto_enclosure", False):
            try:
                from .ecad.ecad_to_enclosure import generate_enclosure_from_pcb
            except ImportError as exc:
                raise ImportError(
                    f"auto_enclosure requires ecad_to_enclosure module: {exc}"
                )
            out_dir = repo_root / "outputs" / "cad" / "step" / f"ecad_{part.get('id', 'pcb')}"
            out_dir.mkdir(parents=True, exist_ok=True)
            result = generate_enclosure_from_pcb(str(pcb_path), str(out_dir))
            # Convert the ecad part into two entries: the PCB and its enclosure
            enclosure_step = getattr(result, "enclosure_step_path", None) or str(out_dir / "enclosure.step")
            part["step"] = enclosure_step
            part["_ecad_ref"] = str(pcb_path)
        else:
            # Without auto_enclosure, just stash a reference — no STEP generated
            part["_ecad_ref"] = str(pcb_path)
            if "step" not in part:
                part["step"] = str(pcb_path.with_suffix(".step"))

    return config


# ---------------------------------------------------------------------------
# BOM generation
# ---------------------------------------------------------------------------

def generate_bom(
    config: dict[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """
    Produce a bill of materials from a hierarchical assembly config.

    Returns:
        {
          "purchased": [ {designation, quantity, unit_cost, total_cost, ...}, ... ],
          "fabricated": [ {id, step_path, ...}, ... ],
          "ecad": [ {id, pcb_path, enclosure_step}, ... ],
          "summary": {
             "total_parts": int,
             "total_purchased_cost_usd": float,
             "total_mass_g": float,
             "unique_components": int,
          }
        }
    """
    from .hierarchical_assembly import flatten_assembly, list_components_used
    from .components import catalog as _catalog

    flat = flatten_assembly(config, config_path=config_path)

    # Aggregate catalog components by designation
    component_counts = list_components_used(config, config_path=config_path)

    purchased: list[dict[str, Any]] = []
    total_purchased_cost = 0.0
    total_mass_g = 0.0
    for designation, qty in sorted(component_counts.items()):
        spec = _catalog.get(designation)
        if spec is None:
            # Unknown component — still record, but no costs
            purchased.append({
                "designation": designation,
                "quantity": qty,
                "description": "Unknown component (not in catalog)",
                "unit_cost_usd": 0.0,
                "total_cost_usd": 0.0,
            })
            continue
        row = spec.to_bom_row(quantity=qty)
        purchased.append(row)
        total_purchased_cost += row["total_cost_usd"]
        total_mass_g += row["mass_g"] * qty

    # Fabricated parts — STEP files that aren't from the catalog
    fabricated: list[dict[str, Any]] = []
    ecad: list[dict[str, Any]] = []
    for part in flat:
        if part.get("_component"):
            continue  # already counted in purchased
        step_path = part.get("step", "")
        if "_ecad_ref" in part or "ecad" in step_path.lower():
            ecad.append({
                "id": part.get("id"),
                "step_path": step_path,
                "pcb_path": part.get("_ecad_ref"),
            })
        else:
            fabricated.append({
                "id": part.get("id"),
                "step_path": step_path,
                "pos": part.get("pos"),
                "rot": part.get("rot"),
            })

    bom = {
        "name": config.get("name", "assembly"),
        "purchased": purchased,
        "fabricated": fabricated,
        "ecad": ecad,
        "summary": {
            "total_parts": len(flat),
            "fabricated_count": len(fabricated),
            "purchased_count": sum(c["quantity"] for c in purchased),
            "unique_components": len(purchased),
            "ecad_count": len(ecad),
            "total_purchased_cost_usd": round(total_purchased_cost, 2),
            "total_mass_g": round(total_mass_g, 2),
        },
    }

    # Annotate with export control classification
    try:
        from .export_control import annotate_bom_with_export_control
        annotate_bom_with_export_control(bom)
    except Exception:
        pass

    return bom


def write_bom_markdown(bom: dict[str, Any], output_path: str | Path) -> str:
    """Write BOM as a human-readable markdown table."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"# Bill of Materials — {bom.get('name', 'assembly')}", ""]
    s = bom["summary"]
    lines.append(f"**Total parts:** {s['total_parts']}  ")
    lines.append(f"**Fabricated:** {s['fabricated_count']} unique  ")
    lines.append(f"**Purchased:** {s['purchased_count']} ({s['unique_components']} unique SKUs)  ")
    lines.append(f"**ECAD boards:** {s['ecad_count']}  ")
    lines.append(f"**Purchased material cost:** ${s['total_purchased_cost_usd']:.2f}  ")
    lines.append(f"**Estimated mass:** {s['total_mass_g']:.1f} g  ")
    lines.append("")

    if bom["purchased"]:
        lines.append("## Purchased Components")
        lines.append("")
        lines.append("| Designation | Qty | Description | Unit Cost | Total | Supplier |")
        lines.append("|---|---:|---|---:|---:|---|")
        for row in bom["purchased"]:
            lines.append(
                f"| `{row['designation']}` | {row['quantity']} | "
                f"{row.get('description', '')} | "
                f"${row['unit_cost_usd']:.2f} | ${row['total_cost_usd']:.2f} | "
                f"{row.get('supplier', '')} |"
            )
        lines.append("")

    if bom["fabricated"]:
        lines.append("## Fabricated Parts (make-to-order)")
        lines.append("")
        lines.append("| ID | STEP File |")
        lines.append("|---|---|")
        for row in bom["fabricated"]:
            lines.append(f"| {row['id']} | `{row['step_path']}` |")
        lines.append("")

    if bom["ecad"]:
        lines.append("## Electronics")
        lines.append("")
        lines.append("| ID | PCB | Enclosure STEP |")
        lines.append("|---|---|---|")
        for row in bom["ecad"]:
            lines.append(f"| {row['id']} | `{row.get('pcb_path', '')}` | `{row['step_path']}` |")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------------------
# MillForge handoff
# ---------------------------------------------------------------------------

def bom_to_millforge_jobs(
    bom: dict[str, Any],
    *,
    goal: str = "",
    run_id: str = "",
) -> list[dict[str, Any]]:
    """
    Convert fabricated parts in a BOM into a list of MillForgeJob dicts.

    Only fabricated parts need scheduling — purchased parts go into procurement.
    Returns serialized bundle dicts ready for MillForge's /api/aria/bundle endpoint.
    """
    try:
        from manufacturing_core.types import MillForgeJob
    except ImportError:
        # Fall back to raw dicts if manufacturing-core isn't installed
        MillForgeJob = None  # type: ignore

    jobs: list[dict[str, Any]] = []
    for part in bom.get("fabricated", []):
        if MillForgeJob is None:
            jobs.append({
                "run_id": run_id or f"{bom.get('name', 'asm')}-{part.get('id', '?')}",
                "part_name": part.get("id", "unknown"),
                "goal": goal or f"Fabricate {part.get('id')}",
                "step_path": part.get("step_path"),
            })
        else:
            job = MillForgeJob(
                aria_run_id=run_id or f"{bom.get('name', 'asm')}-{part.get('id', '?')}",
                goal=goal or f"Fabricate {part.get('id')} for {bom.get('name', 'assembly')}",
                part_id=part.get("id", "unknown"),
                step_path=part.get("step_path"),
            )
            jobs.append(job.to_bundle_dict())
    return jobs
