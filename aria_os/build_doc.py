"""Build-instruction emitter for ARIA-generated assemblies.

Reads the artifacts ARIA produces:
  - ECAD BOM JSON (aria_os/ecad_generator output)  [optional, but ≥1 required]
  - MCAD STEP path                                  [optional]
  - Mech BOM JSON (with screws/standoffs/etc.)     [optional]
  - kicad-cli artifact set (Gerbers/STEP/GLB)      [optional, for refs]

Emits:
  - BUILD.md   — ordered, human-readable assembly guide
  - BUILD.json — same data, machine-readable
  - BOM.csv    — flat purchasing list

The output is *generic*: not tied to any one product family. Group structure:
  1. Header (project, date, totals)
  2. Tools required
  3. Materials list (purchased + fabricated)
  4. Mechanical assembly (if mech artifacts present)
  5. PCB assembly (per board, per family: passives → semis → connectors)
  6. Mech-electrical mating (PCB-into-enclosure)
  7. Power-on / functional tests

Fastener torques + tool requirements are looked up from a small table
(_HARDWARE) keyed by metric thread size; same idea as
assembly_instructions.py but generalised so we don't need a drone-shaped
input.

Entry point:

    from aria_os.build_doc import generate_build_doc
    paths = generate_build_doc(
        out_dir   = Path("outputs/runs/<id>/build"),
        ecad_boms = [Path("outputs/ecad/.../board_bom.json")],
        mcad_step = Path("outputs/cad/step/enclosure.step"),
        mech_bom  = None,
        project_name = "ARIA LED demo",
    )
    # paths => {"md": ..., "json": ..., "csv": ...}
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Hardware specs — keyed by ISO thread (M2, M2.5, M3, M4, M5, M6).
# Standard metric SHCS torque per ISO 898-1, class 8.8 unless noted.
# ---------------------------------------------------------------------------
_HARDWARE: dict[str, dict[str, str]] = {
    "M2":   {"torque": "0.15 Nm", "tool": "1.5 mm hex driver",
              "tip":    "Snug + 1/8 turn. M2 strips easily — feel for the seat."},
    "M2.5": {"torque": "0.30 Nm", "tool": "2.0 mm hex driver",
              "tip":    "Snug + 1/8 turn."},
    "M3":   {"torque": "0.60 Nm", "tool": "2.5 mm hex driver",
              "tip":    "Snug + 1/4 turn for SHCS, less for button-head."},
    "M4":   {"torque": "1.20 Nm", "tool": "3.0 mm hex driver",
              "tip":    "Standard PCB / chassis fastener. Hand-tight + 1/4 turn."},
    "M5":   {"torque": "2.50 Nm", "tool": "4.0 mm hex driver",
              "tip":    "Used in mounting brackets; not recommended for PCB stack-ups."},
    "M6":   {"torque": "4.50 Nm", "tool": "5.0 mm hex driver",
              "tip":    "Heavy-duty; structural mounting only."},
}


# ---------------------------------------------------------------------------
# Footprint family classification — drives PCB solder-order grouping.
# Order in this list IS the recommended solder order: low-profile first
# (so subsequent parts don't shadow the iron access), passives → semis →
# connectors. Smallest pad pitch first WITHIN each group.
# ---------------------------------------------------------------------------
_PCB_FAMILY_ORDER = [
    ("passive_smd",   ["Resistor_SMD", "Capacitor_SMD", "Inductor_SMD",
                         "Fuse_SMD", "Diode_SMD"]),
    ("led_smd",       ["LED_SMD"]),
    ("semiconductor", ["Package_SO", "Package_TO", "Package_DFN_QFN",
                         "Package_QFP", "Package_BGA", "Package_TO_SOT_SMD"]),
    ("crystal",       ["Crystal", "Oscillator"]),
    ("through_hole",  ["Resistor_THT", "Capacitor_THT", "Diode_THT",
                         "LED_THT", "Package_DIP"]),
    ("connector",     ["Connector_PinHeader", "Connector_USB",
                         "Connector_JST", "Connector_Molex",
                         "Connector_Phoenix", "Connector_Generic"]),
    ("misc",          []),    # fallback bucket
]


def _classify_footprint(fp: str) -> str:
    if not fp:
        return "misc"
    head = fp.split(":", 1)[0]
    for family, prefixes in _PCB_FAMILY_ORDER:
        if any(head.startswith(p) for p in prefixes):
            return family
    return "misc"


def _bom_to_groups(bom: dict) -> dict[str, list[dict]]:
    """Group BOM components by solder-order family."""
    by_family: dict[str, list[dict]] = {f: [] for f, _ in _PCB_FAMILY_ORDER}
    for comp in bom.get("components", []):
        fam = _classify_footprint(comp.get("footprint", ""))
        by_family.setdefault(fam, []).append(comp)
    # Sort within each family by ref designator for stable output
    for parts in by_family.values():
        parts.sort(key=lambda c: (c.get("ref", ""), c.get("value", "")))
    return by_family


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------
def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    line = "| " + " | ".join(headers) + " |\n"
    line += "| " + " | ".join("---" for _ in headers) + " |\n"
    for r in rows:
        cells = [str(c).replace("|", "\\|") for c in r]
        line += "| " + " | ".join(cells) + " |\n"
    return line


def _list_step(idx: int, body: str) -> str:
    return f"**Step {idx}.** {body}\n\n"


# ---------------------------------------------------------------------------
# Tool inference — walk the BOMs and figure out what's actually needed
# ---------------------------------------------------------------------------
def _infer_tools(ecad_boms: list[dict], mech_bom: dict | None) -> list[str]:
    tools: list[str] = []
    have_smd = False
    have_tht = False
    threads_used: set[str] = set()
    for bom in ecad_boms:
        for c in bom.get("components", []):
            fp = c.get("footprint", "")
            if "_SMD:" in fp:
                have_smd = True
            elif "_THT:" in fp or fp.startswith("Connector_"):
                have_tht = True
    if mech_bom:
        for item in mech_bom.get("items", []):
            spec = (item.get("spec") or "").upper()
            for thread in _HARDWARE:
                if spec.startswith(thread.upper() + "X") or spec.startswith(thread.upper() + " "):
                    threads_used.add(thread)
    if have_smd:
        tools += [
            "Soldering iron with chisel tip (≤ 0.4 mm) at 320 °C",
            "Lead-free solder paste (T4 or T5)",
            "Hot-air rework station for 0805+ packages, 280 °C / med airflow",
            "Tweezers (curved + straight)",
            "Flux pen (no-clean)",
            "Isopropyl alcohol (≥ 99 %) + lint-free wipes for post-clean",
        ]
    if have_tht:
        tools += [
            "Soldering iron with conical tip (0.8 mm) at 350 °C",
            "Lead-free solder wire 0.5 mm",
            "Side cutters (flush)",
        ]
    for thread in sorted(threads_used):
        tools.append(_HARDWARE[thread]["tool"])
    if mech_bom:
        tools.append("Calibrated torque driver, 0.1–2.5 Nm range")
    if not tools:
        tools.append("(no tools inferred — BOM is empty)")
    return tools


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def generate_build_doc(
    out_dir: str | Path,
    *,
    ecad_boms: Iterable[str | Path] = (),
    mcad_step: str | Path | None = None,
    mech_bom: str | Path | None = None,
    project_name: str = "ARIA assembly",
    pcb_step_paths: Iterable[str | Path] = (),
) -> dict[str, Path]:
    """Emit BUILD.md, BUILD.json, BOM.csv for an assembly.

    Returns the paths of the three files. Best-effort: any single section
    that fails (e.g. mech_bom missing) is logged in the doc and skipped
    rather than raising.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Load inputs ---------------------------------------------------
    ecad_data: list[tuple[Path, dict]] = []
    for p in ecad_boms:
        bp = Path(p)
        try:
            ecad_data.append((bp, json.loads(bp.read_text(encoding="utf-8"))))
        except Exception as e:
            ecad_data.append((bp, {"_error": str(e), "components": []}))

    mech_data: dict | None = None
    if mech_bom:
        try:
            mech_data = json.loads(Path(mech_bom).read_text(encoding="utf-8"))
        except Exception as e:
            mech_data = {"_error": str(e), "items": []}

    pcb_step_list = [Path(p) for p in pcb_step_paths]

    # --- Build the structured "build plan" -----------------------------
    plan: dict[str, Any] = {
        "project":       project_name,
        "generated_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tools":         _infer_tools([d for _, d in ecad_data], mech_data),
        "stages":        [],
        "totals": {
            "ecad_boards":   len(ecad_data),
            "mcad_step":     str(mcad_step) if mcad_step else None,
            "components":    sum(len(d.get("components", [])) for _, d in ecad_data),
        },
    }

    # Stage 1: mechanical fabrication / cleanup
    if mcad_step:
        plan["stages"].append({
            "title": "Mechanical sub-assembly",
            "kind":  "mech",
            "step_file": str(mcad_step),
            "steps": [
                "Inspect the machined / printed enclosure for burrs around mounting holes; deburr with a 1 mm chamfer tool if needed.",
                "Tap mounting standoffs to thread spec (typically M3) if blind holes were left raw.",
                "Wipe the bore + mating surfaces with IPA. Allow to fully dry before any electrical work touches it.",
            ],
        })

    # Stage 2: PCB assembly per board, grouped by family
    for bp, bom in ecad_data:
        if "_error" in bom:
            plan["stages"].append({
                "title": f"PCB '{bp.stem}' (failed to load)",
                "kind":  "pcb_error",
                "error": bom["_error"],
            })
            continue
        groups = _bom_to_groups(bom)
        steps: list[dict] = []
        for fam, _ in _PCB_FAMILY_ORDER:
            parts = groups.get(fam, [])
            if not parts:
                continue
            steps.append({
                "family":     fam,
                "n_parts":    len(parts),
                "components": [
                    {
                        "ref":   c.get("ref", "?"),
                        "value": c.get("value", "?"),
                        "footprint": c.get("footprint", ""),
                        "x_mm":  c.get("x_mm"),
                        "y_mm":  c.get("y_mm"),
                    } for c in parts
                ],
            })
        board_dim = bom.get("board", {})
        plan["stages"].append({
            "title": f"Solder PCB '{bp.stem}' "
                      f"({board_dim.get('width_mm', '?')}×{board_dim.get('height_mm', '?')} mm)",
            "kind":  "pcb",
            "bom":   str(bp),
            "groups": steps,
        })

    # Stage 3: mech ↔ ECAD mating (if both present)
    if mcad_step and ecad_data:
        # For each board, suggest a mounting sequence. We don't know the
        # exact mounting hardware unless mech_bom told us, so we describe
        # the canonical M3-on-standoff path and note where to substitute.
        plan["stages"].append({
            "title": "Mate PCB(s) into mechanical enclosure",
            "kind":  "mate",
            "steps": [
                "Power-on test EACH PCB on the bench BEFORE installing — far easier to debug a board outside the enclosure than inside it.",
                "Inspect that all PCB mounting holes line up with enclosure standoffs. If any are off by > 0.3 mm, stop and check the source files.",
                "Insert PCB into the enclosure. The board should drop onto the standoffs without forcing.",
                "Insert M3 SHCS through PCB mounting holes into standoffs. Hand-tight first across ALL screws, then torque to 0.6 Nm in a star pattern.",
                "Re-test power-on with the cover off. Some intermittent grounding faults only appear once the board is grounded to the enclosure.",
                "Install cover. Torque to spec.",
            ],
        })

    # Stage 4: functional verification
    plan["stages"].append({
        "title": "Functional verification",
        "kind":  "test",
        "steps": [
            "Continuity-check power rails to ground BEFORE applying power. If any rail shows < 100 Ω to GND, do NOT power on — there's a short.",
            "Apply nominal supply voltage with current-limit set to 1.5× expected idle draw. If current spikes past the limit, kill power immediately.",
            "Verify each rail at the test points listed in the schematic.",
            "Run any board-level smoke test (e.g. blink LED, send UART character, scan I²C).",
            "If pass: log serial number + result in the build log. If fail: see the troubleshooting matrix in the project README.",
        ],
    })

    # --- Render BUILD.md ----------------------------------------------
    md_lines: list[str] = []
    md_lines.append(f"# Build instructions — {project_name}\n")
    md_lines.append(f"_Generated by ARIA-OS at {plan['generated_at']}_\n")

    md_lines.append("## Tools required\n")
    for t in plan["tools"]:
        md_lines.append(f"- {t}")
    md_lines.append("")

    md_lines.append("## Materials\n")
    bom_rows: list[list[str]] = []
    for bp, bom in ecad_data:
        for c in bom.get("components", []):
            bom_rows.append([
                c.get("ref", "?"),
                c.get("value", "?"),
                c.get("footprint", "?"),
                str(c.get("qty", 1)),
                c.get("description", ""),
                bp.stem,
            ])
    if mech_data:
        for item in mech_data.get("items", []):
            bom_rows.append([
                item.get("id", "?"),
                item.get("spec", "?"),
                item.get("type", "mech"),
                str(item.get("quantity", 1)),
                item.get("notes", ""),
                "mech",
            ])
    if bom_rows:
        md_lines.append(_md_table(
            ["Ref", "Value/Spec", "Footprint/Type", "Qty", "Description", "Source"],
            bom_rows))
    else:
        md_lines.append("_(no materials)_\n")

    md_lines.append("## Assembly steps\n")
    step_idx = 1
    for stage in plan["stages"]:
        md_lines.append(f"### {stage['title']}\n")
        if stage["kind"] in ("mech", "mate", "test"):
            for body in stage.get("steps", []):
                md_lines.append(_list_step(step_idx, body))
                step_idx += 1
        elif stage["kind"] == "pcb":
            for grp in stage["groups"]:
                refs = ", ".join(c["ref"] for c in grp["components"])
                fam = grp["family"].replace("_", " ")
                md_lines.append(_list_step(
                    step_idx,
                    f"**Solder {grp['n_parts']} {fam} part(s):** {refs}. "
                    f"Apply paste, place per the placement file, reflow with hot air. "
                    f"Inspect each joint under 10× magnification before moving on."))
                step_idx += 1
                # Component table inline so the operator doesn't have to flip docs
                md_lines.append(_md_table(
                    ["Ref", "Value", "Footprint", "X (mm)", "Y (mm)"],
                    [[c["ref"], c["value"], c["footprint"],
                      f"{c.get('x_mm', '?')}",
                      f"{c.get('y_mm', '?')}"] for c in grp["components"]]))
        elif stage["kind"] == "pcb_error":
            md_lines.append(f"_PCB BOM failed to load: {stage['error']}_\n")
        md_lines.append("")

    md_lines.append("---")
    md_lines.append(
        f"_{plan['totals']['components']} electrical components across "
        f"{plan['totals']['ecad_boards']} board(s)._")

    # --- Write outputs -------------------------------------------------
    md_path   = out_dir / "BUILD.md"
    json_path = out_dir / "BUILD.json"
    csv_path  = out_dir / "BOM.csv"

    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Ref", "Value/Spec", "Footprint/Type",
                    "Qty", "Description", "Source"])
        for r in bom_rows:
            w.writerow(r)

    return {"md": md_path, "json": json_path, "csv": csv_path}
