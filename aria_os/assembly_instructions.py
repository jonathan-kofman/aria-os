"""
Human-readable assembly instructions for drone bundles.

Consumes the populated BOM (mass_g present) produced by the build pipeline and
renders an ordered, sub-assembly-grouped Markdown document the user actually
follows on the bench. Optionally renders a PDF if weasyprint or markdown-pdf
is installed.

Entry points:

    from aria_os.assembly_instructions import (
        generate_assembly_md,
        generate_assembly_pdf,
    )

    md_path  = generate_assembly_md(bom_dict, output_dir)
    pdf_path = generate_assembly_pdf(md_path)   # None if no renderer available
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Fastener / tool specs per part family
#
# Each entry: (fastener, torque, tool, tip). These are the real values an
# operator needs at the bench — they were previously scattered across tribal
# knowledge, comments, and "ask a senior tech". Centralizing here makes the
# instructions deterministic and reviewable.
# ---------------------------------------------------------------------------

# Per-spec (not per-instance) hardware notes.
_HARDWARE: dict[str, dict[str, str]] = {
    "bottom_plate": {
        "fastener": "–",
        "torque":   "–",
        "tool":     "Clean work surface, 2.5mm hex bit (for later steps)",
        "tip":      "Orient the plate so the battery-strap slots run front-to-back. "
                    "This is the reference frame for everything that follows.",
    },
    "arm": {
        "fastener": "2× M3×8 button-head per arm (8 total) through bottom plate",
        "torque":   "0.6 Nm",
        "tool":     "2.5mm hex bit",
        "tip":      "Install arms in order FR → FL → BL → BR. Motor wires must "
                    "exit along the TOP face of each arm (they get routed into "
                    "the stack). Snug all 8 bolts before torquing any one.",
    },
    "standoff": {
        "fastener": "4× M3×8 into bottom plate (arm-clamp bolts pass through)",
        "torque":   "0.6 Nm",
        "tool":     "2.5mm hex bit",
        "tip":      "Standoffs sandwich the arm roots between top and bottom "
                    "plate — do NOT overtighten now, you'll torque after the "
                    "top plate goes on.",
    },
    "top_plate": {
        "fastener": "4× M3×8 button-head into standoff tops",
        "torque":   "0.6 Nm (final)",
        "tool":     "2.5mm hex bit",
        "tip":      "Cross-pattern tighten: FR → BL → FL → BR. Check arms rotate "
                    "≤ 0.1° under hand pressure — if they move, retorque.",
    },
    "esc_pcb": {
        "fastener": "4× M3 nylon spacer / rubber grommet stack onto standoffs",
        "torque":   "Hand tight",
        "tool":     "Fingers",
        "tip":      "ESC goes on FIRST (lower in stack). Match motor-output "
                    "pads M1..M4 to FR/FL/BL/BR arm wire entry. XT60 faces aft.",
    },
    "fc_pcb": {
        "fastener": "4× M3 nylon spacer on top of ESC spacers",
        "torque":   "Hand tight",
        "tool":     "Fingers",
        "tip":      "Flight controller arrow points FORWARD (toward +X, between "
                    "arm_fr and arm_fl). Align before plugging the ESC ribbon — "
                    "a rotated FC causes a crash on the first arm.",
    },
    "motor": {
        "fastener": "4× M3×8 (or M2×6 for smaller frames) into arm-tip bosses",
        "torque":   "0.5 Nm",
        "tool":     "2mm or 2.5mm hex bit",
        "tip":      "Use threadlocker on motor screws — these vibrate loose first. "
                    "FR + BL spin CCW (props-in); FL + BR spin CW. Wire order at "
                    "ESC: swap any 2 phases to reverse a motor if direction is wrong.",
    },
    "prop": {
        "fastener": "Prop nut (M5 typical) per motor shaft",
        "torque":   "Snug + locknut (do NOT over-torque)",
        "tool":     "8mm or 10mm prop wrench",
        "tip":      "Props are handed. Check blade leading edge orientation vs "
                    "motor rotation direction before spinning. Install LAST — "
                    "after arming test, not before.",
    },
    "battery": {
        "fastener": "1× battery strap through the slots in bottom plate",
        "torque":   "Hand snug",
        "tool":     "–",
        "tip":      "Battery rides on top plate (not bottom) with foam anti-slip "
                    "pad underneath. Route the XT60 lead aft. Check strap is "
                    "centered over the CG mark before flight.",
    },
    "canopy": {
        "fastener": "2× M3×6 into top-plate inserts (or 3M-VHB tape)",
        "torque":   "0.3 Nm",
        "tool":     "2.5mm hex bit",
        "tip":      "Canopy goes LAST — it buries the FC. Before fitting, verify "
                    "ExpressLRS is bound and OSD is visible in goggles.",
    },

    # ── Military preset parts ────────────────────────────────────────────
    "armor_top": {
        "fastener": "Bonded (2-part epoxy) + 4× M3×10 through stack holes",
        "torque":   "Clamp 12h, then 0.6 Nm on bolts",
        "tool":     "Epoxy applicator, 2.5mm hex bit",
        "tip":      "Top armor is aramid/CFRP sandwich — apply thin epoxy bead "
                    "around the stack-hole circle only. Excess epoxy contaminates "
                    "the FC USB port.",
    },
    "armor_bottom": {
        "fastener": "Bonded to underside of bottom plate",
        "torque":   "Clamp 12h",
        "tool":     "Epoxy applicator, C-clamps",
        "tip":      "Bottom armor is the ballistic layer. Bond FIRST (before "
                    "arms go on) because the clamp pressure is hard to apply "
                    "after the frame is populated.",
    },
    "vision_pod": {
        "fastener": "4× M3×8 through pod flange into canopy front",
        "torque":   "0.4 Nm",
        "tool":     "2.5mm hex bit",
        "tip":      "Wire the FPV cam + thermal sensor to the FC before bolting "
                    "the pod — the cable run through the canopy window is "
                    "fiddly once sealed.",
    },
    "gimbal_yoke": {
        "fastener": "2× M3×8 through yoke base into pod nose",
        "torque":   "0.4 Nm",
        "tool":     "2.5mm hex bit",
        "tip":      "Yoke arms hold the 2-axis servo block. Check yaw free-play "
                    "before mounting servos — any bind here costs you the first "
                    "30° of pan authority.",
    },
    "fiber_spool": {
        "fastener": "1× M5 shoulder bolt through spool hub into rear bracket",
        "torque":   "Hand tight + nyloc",
        "tool":     "4mm hex bit, 8mm wrench",
        "tip":      "Spool must spin FREE with zero drag. Any friction here snaps "
                    "the fiber on the first aggressive yaw. Pre-test spin by "
                    "hand — 3+ seconds of coast means go.",
    },
    "fiber_eyelet": {
        "fastener": "Press-fit into aft frame cutout (or 1× M2 setscrew)",
        "torque":   "Light tap or 0.1 Nm",
        "tool":     "Soft mallet or 1.5mm hex bit",
        "tip":      "Eyelet is the last friction point before fiber exits the "
                    "airframe. Polished chamfer MUST face outward — a rough "
                    "inner edge will scar the fiber on every payout cycle.",
    },
    "gps_puck": {
        "fastener": "Double-sided foam tape on top armor, aft of stack",
        "torque":   "Press firm 30s",
        "tool":     "–",
        "tip":      "Keep GPS 30mm+ away from the ESC and VTX — switching noise "
                    "eats satellite lock. Antenna face UP, cable routes under "
                    "the battery strap.",
    },
    "rx_module": {
        "fastener": "Double-sided foam tape next to FC",
        "torque":   "Press firm 30s",
        "tool":     "–",
        "tip":      "ExpressLRS module antenna should exit the canopy — not be "
                    "buried under it. T-antenna arrangement (one up, one out) "
                    "doubles your link margin.",
    },
    "payload_rail": {
        "fastener": "4× M4×10 into bottom-armor threaded inserts",
        "torque":   "1.0 Nm",
        "tool":     "3mm hex bit",
        "tip":      "Picatinny-pattern holes accept standard NATO accessory "
                    "rails. Do NOT exceed payload mass limit (check TWR margin "
                    "with loaded mass before first flight).",
    },
}


# Ordered build stages — each stage is a list of part-spec names. Instance
# counts come from the BOM (so a preset with 0 prop_guards drops that row).
_FPV_STAGES: list[tuple[str, list[str], str]] = [
    ("1. Frame",
        ["bottom_plate", "arm", "standoff", "top_plate"],
        "Build the mechanical skeleton. This is the load-bearing structure "
        "that every other part bolts to."),
    ("2. Electronic stack",
        ["esc_pcb", "fc_pcb"],
        "Stack the PCBs on the standoffs. ESC goes in first (lower), flight "
        "controller on top. Check orientation arrows before locking down."),
    ("3. Power",
        ["battery"],
        "Battery rides the top plate via strap. Don't plug it in yet — you "
        "have more wiring to do."),
    ("4. Propulsion",
        ["motor", "prop"],
        "Motors bolt onto arm tips; props go on LAST after a bench-arm test. "
        "Verify each motor direction before spinning props."),
    ("5. Cover",
        ["canopy"],
        "Canopy closes up the stack. This is your last chance to catch a "
        "wiring mistake — do the pre-flight checklist before snapping it on."),
]


_MILITARY_STAGES: list[tuple[str, list[str], str]] = _FPV_STAGES + [
    ("6. Armor",
        ["armor_top", "armor_bottom"],
        "Aramid sandwich plates — one above the stack, one below the bottom "
        "plate. Bond with epoxy and torque through-bolts."),
    ("7. Vision pod + gimbal",
        ["vision_pod", "gimbal_yoke"],
        "Mount the hardened ISR pod and gimbal yoke on the canopy nose. "
        "Wire through the canopy BEFORE sealing."),
    ("8. Fiber tether",
        ["fiber_spool", "fiber_eyelet"],
        "Rear-mounted spool + aft eyelet. Friction-free spin is non-negotiable."),
    ("9. Nav + RF",
        ["gps_puck", "rx_module"],
        "GPS on top armor (aft of stack), ELRS receiver next to FC. Keep "
        "antennas away from high-current switching."),
    ("10. Payload rail",
        ["payload_rail"],
        "Picatinny-pattern underside rail for modular payloads."),
]


_PREFLIGHT_CHECKLIST = [
    "Motor directions correct (FR/BL = CCW, FL/BR = CW)",
    "Props balanced — spin test on a jig, no visible wobble",
    "Props seated against motor hub, prop nuts locked",
    "Battery strap centered over CG mark, XT60 routed aft",
    "FC orientation arrow points to front (+X)",
    "ESC calibrated (throttle range confirmed in Betaflight)",
    "Radio binds — failsafe set to DISARM on signal loss",
    "OSD visible in goggles; VTX channel matches goggle channel",
    "Compass/GPS lock before takeoff (if using GPS mode)",
    "First flight: LOW rates, grassy field, LOS only",
]


_PREFLIGHT_MILITARY_EXTRA = [
    "Fiber spool spins ≥ 3s coast-down by hand",
    "Vision pod FPV cam + thermal sensor both live on OSD",
    "Gimbal yoke servos centered; full ±90° yaw range clear",
    "GPS satellite count ≥ 10 before arming",
    "ELRS link RSSI ≥ -80 dBm at 50m ground check",
    "Payload rail torque re-checked (vibration loosens M4 first)",
]


# ---------------------------------------------------------------------------
# Core markdown generator
# ---------------------------------------------------------------------------

def _estimate_assembly_time(is_military: bool, n_parts: int) -> str:
    """Return an estimated assembly time string.

    ~30 min baseline for FPV (5/7inch consumer), ~60 min for military recon
    (roughly 2× the parts + bond cure dwell). Scales mildly with part count.
    """
    base = 60 if is_military else 30
    # Nudge ±5min for very large / very small BOMs
    if n_parts > 30:
        base += 10
    elif n_parts < 15:
        base -= 5
    return f"~{base} minutes"


def _is_military(bom: dict[str, Any]) -> bool:
    """Detect military preset from BOM metadata or part names."""
    if (bom.get("platform") or "").lower() == "military_recon":
        return True
    names = {p.get("spec") or p.get("name") or "" for p in bom.get("parts") or []}
    return any(n.startswith(("armor_", "vision_pod", "fiber_", "gps_puck",
                             "rx_module", "payload_rail", "gimbal_yoke"))
               for n in names)


def _group_by_spec(bom: dict[str, Any]) -> dict[str, list[dict]]:
    """Group BOM parts by spec name. Returns {spec_name: [part, ...]}."""
    out: dict[str, list[dict]] = {}
    for p in bom.get("parts") or []:
        if not isinstance(p, dict):
            continue
        key = p.get("spec") or p.get("name")
        if not key:
            continue
        out.setdefault(key, []).append(p)
    return out


def _format_mass(g: float | None) -> str:
    if g is None or g == 0:
        return "—"
    return f"{g:.1f} g"


def _render_stage(
    stage_num: str,
    stage_title: str,
    stage_blurb: str,
    specs: list[str],
    groups: dict[str, list[dict]],
) -> str:
    """Render one build stage section."""
    lines: list[str] = []
    # The title already starts with "N. " — strip that from the heading
    # level, it's already encoded by the Markdown structure.
    title = stage_title
    lines.append(f"## {title}")
    lines.append("")
    lines.append(stage_blurb)
    lines.append("")

    for spec in specs:
        parts = groups.get(spec) or []
        if not parts:
            continue
        count = len(parts)
        # Sum mass across all instances of this spec
        mass_total = 0.0
        for p in parts:
            m = p.get("mass_g") or (p.get("measured") or {}).get("mass_g")
            if isinstance(m, (int, float)):
                mass_total += float(m)
        material = parts[0].get("material", "—")
        instance_names = ", ".join(
            p.get("name", spec) for p in parts
        )

        hw = _HARDWARE.get(spec, {})

        qty_tag = f"{count}× " if count > 1 else ""
        lines.append(f"### {qty_tag}{spec.replace('_', ' ').title()}")
        lines.append("")
        lines.append(f"- **Quantity:** {count}")
        lines.append(f"- **Instances:** {instance_names}")
        lines.append(f"- **Material:** {material}")
        lines.append(f"- **Mass (total for this spec):** {_format_mass(mass_total)}")
        if hw:
            lines.append(f"- **Fastener:** {hw.get('fastener', '–')}")
            lines.append(f"- **Torque:** {hw.get('torque', '–')}")
            lines.append(f"- **Tool:** {hw.get('tool', '–')}")
            lines.append("")
            tip = hw.get("tip")
            if tip:
                lines.append(f"> **Tip.** {tip}")
        lines.append("")

        # Install-location table per instance — pulled straight from the
        # placer() output that was captured in the BOM.
        if count > 0:
            lines.append("**Install location (mm / deg):**")
            lines.append("")
            lines.append("| Instance | Position (x, y, z) | Rotation (rx, ry, rz) |")
            lines.append("|---|---|---|")
            for p in parts:
                pos = p.get("position_mm") or [0, 0, 0]
                rot = p.get("rotation_deg") or [0, 0, 0]
                pos_s = "(" + ", ".join(f"{v:.1f}" for v in pos) + ")"
                rot_s = "(" + ", ".join(f"{v:.1f}" for v in rot) + ")"
                lines.append(f"| {p.get('name', spec)} | {pos_s} | {rot_s} |")
            lines.append("")
    return "\n".join(lines)


def generate_assembly_md(
    bom: dict[str, Any] | str | Path,
    output_dir: str | Path,
) -> Path:
    """Render assembly_instructions.md into output_dir. Returns the file path.

    `bom` may be a dict, or a path to bom.json.
    """
    if isinstance(bom, (str, Path)):
        bom_path = Path(bom)
        bom = json.loads(bom_path.read_text(encoding="utf-8"))

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "assembly_instructions.md"

    name = bom.get("assembly_name") or bom.get("name") or "drone_assembly"
    parts = bom.get("parts") or []
    n_parts = len(parts)
    total_mass_g = float(bom.get("total_mass_g") or 0.0)
    if total_mass_g == 0.0:
        # Fallback — sum mass_g across parts if the top-level field wasn't set
        total_mass_g = sum(
            float(p.get("mass_g") or 0.0)
            for p in parts if isinstance(p, dict)
        )

    military = _is_military(bom)
    stages = _MILITARY_STAGES if military else _FPV_STAGES
    est_time = _estimate_assembly_time(military, n_parts)

    groups = _group_by_spec(bom)

    # ── Header ──────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"# {name} — Assembly Instructions")
    lines.append("")
    platform = "Military recon (7-inch, armored, tethered)" if military else \
               "FPV / consumer quadcopter"
    lines.append(f"**Platform:** {platform}  ")
    lines.append(f"**Total mass (as built):** {_format_mass(total_mass_g)}  ")
    lines.append(f"**Part count:** {n_parts}  ")
    lines.append(f"**Estimated assembly time:** {est_time}  ")
    lines.append("")
    lines.append("This document is generated automatically by ARIA-OS after "
                 "every build. Part positions, masses, and quantities are "
                 "pulled straight from the STEP assembly / BOM — no hand "
                 "editing required.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Mass breakdown ──────────────────────────────────────────────────
    breakdown = bom.get("mass_breakdown") or {}
    if breakdown:
        lines.append("## Mass breakdown")
        lines.append("")
        lines.append("| Sub-assembly | Mass (g) |")
        lines.append("|---|---|")
        for k in sorted(breakdown.keys(), key=lambda x: -float(breakdown[x] or 0)):
            lines.append(f"| {k} | {float(breakdown[k] or 0):.2f} |")
        lines.append(f"| **Total** | **{total_mass_g:.2f}** |")
        lines.append("")

    # ── Tools you need ──────────────────────────────────────────────────
    lines.append("## Tools required")
    lines.append("")
    lines.append("- 2mm, 2.5mm, 3mm hex bits (ball-end preferred)")
    lines.append("- 4mm hex bit (military preset only — spool shoulder bolt)")
    lines.append("- 8mm / 10mm prop wrench")
    lines.append("- Torque driver calibrated 0.3–1.0 Nm")
    lines.append("- Threadlocker (medium / blue)")
    if military:
        lines.append("- 2-part epoxy (for armor bonding) + C-clamps, 12h cure")
        lines.append("- Soft mallet for fiber eyelet press-fit")
    lines.append("- Wire strippers, soldering iron, heatshrink")
    lines.append("- Prop balancer")
    lines.append("")

    # ── Stages ──────────────────────────────────────────────────────────
    for stage_num, specs, blurb in stages:
        section = _render_stage(stage_num, stage_num, blurb, specs, groups)
        if section.strip():
            lines.append(section)
            lines.append("---")
            lines.append("")

    # ── Pre-flight checklist ────────────────────────────────────────────
    lines.append("## Pre-flight checklist")
    lines.append("")
    lines.append("Complete every item before applying throttle:")
    lines.append("")
    for item in _PREFLIGHT_CHECKLIST:
        lines.append(f"- [ ] {item}")
    if military:
        lines.append("")
        lines.append("**Military / ISR additions:**")
        lines.append("")
        for item in _PREFLIGHT_MILITARY_EXTRA:
            lines.append(f"- [ ] {item}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"_Generated by ARIA-OS assembly_instructions.py — "
                 f"bundle: `{name}`_")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


# ---------------------------------------------------------------------------
# Optional PDF renderer
# ---------------------------------------------------------------------------

def generate_assembly_pdf(md_path: str | Path) -> Path | None:
    """Render the MD to PDF if a renderer is available. Returns None otherwise.

    Renderer preference order:
      1. weasyprint  — pure-Python, best fidelity on Linux/Mac
      2. markdown-pdf CLI — Node-based, cross-platform
      3. None
    """
    md_path = Path(md_path)
    if not md_path.is_file():
        return None
    pdf_path = md_path.with_suffix(".pdf")

    # Try weasyprint (Python)
    try:
        import markdown as _md  # noqa: F401
        from weasyprint import HTML
        import markdown as md_lib
        html_body = md_lib.markdown(
            md_path.read_text(encoding="utf-8"),
            extensions=["tables", "fenced_code"],
        )
        html_full = (
            "<html><head><meta charset='utf-8'>"
            "<style>body{font-family:sans-serif;max-width:820px;margin:2em auto;}"
            "table{border-collapse:collapse;}"
            "th,td{border:1px solid #999;padding:4px 8px;}"
            "h1,h2,h3{color:#113366;}"
            "blockquote{border-left:3px solid #3366aa;margin:1em 0;"
            "padding:0.5em 1em;background:#eef;}</style></head><body>"
            f"{html_body}</body></html>"
        )
        HTML(string=html_full).write_pdf(str(pdf_path))
        return pdf_path
    except Exception:
        pass

    # Try markdown-pdf CLI (npm i -g markdown-pdf)
    exe = shutil.which("markdown-pdf")
    if exe:
        try:
            subprocess.run(
                [exe, "-o", str(pdf_path), str(md_path)],
                check=True, timeout=60,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if pdf_path.is_file():
                return pdf_path
        except Exception:
            pass

    return None


__all__ = ["generate_assembly_md", "generate_assembly_pdf"]
