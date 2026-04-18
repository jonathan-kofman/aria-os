"""
scripts/build_synthetic_dataset.py

Generates a synthetic fine-tuning dataset by sweeping parameter combinations
across every _cq_<name> template in aria_os.generators.cadquery_generator.

No LLM calls. Purely deterministic. Safe to run without API keys.

Usage:
    python scripts/build_synthetic_dataset.py [--n N] [--seed SEED] [--out PATH]

    --n     Number of parameter sweeps per template (default: 50)
    --seed  RNG seed for reproducibility (default: 42)
    --out   Output JSONL path (default: outputs/datasets/synthetic_cad_<timestamp>.jsonl)
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import math
import random
import re
import sys
import textwrap
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Resolve repo root and add to path so we can import aria_os regardless of
# the working directory from which the script is invoked.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Parameter sampling configuration
# ---------------------------------------------------------------------------
_MATERIALS = [
    "aluminium_6061", "steel", "cfrp", "peek", "abs", "pla",
    "stainless_steel", "titanium", "nylon", "petg",
]

_BLADE_SWEEPS = ["backward", "forward", "radial",
                 "backward_curved", "forward_curved"]

# Friendly part-type labels used in natural-language goal generation.
# Keys are the _cq_<name> suffixes (function names without the _cq_ prefix).
_PART_LABELS: dict[str, str] = {
    "ratchet_ring":      "ratchet ring",
    "housing":           "housing",
    "hollow_rect":       "hollow rectangular tube",
    "spool":             "spool",
    "cam_collar":        "cam collar",
    "brake_drum":        "brake drum",
    "catch_pawl":        "catch pawl",
    "rope_guide":        "rope guide",
    "phone_case":        "phone case",
    "flat_plate":        "flat plate",
    "bracket":           "bracket",
    "l_bracket":         "L-bracket",
    "impeller":          "impeller",
    "heat_sink":         "heat sink",
    "phone_stand":       "phone stand",
    "flange":            "flange",
    "shaft":             "shaft",
    "pulley":            "pulley",
    "cam":               "cam",
    "pin":               "pin",
    "spacer":            "spacer",
    "tube":              "tube",
    "gear":              "gear",
    "nozzle":            "nozzle",
    "escape_wheel":      "escape wheel",
    "nema_motor":        "NEMA motor mock-up",
    "mgn_rail":          "MGN linear rail",
    "ball_bearing":      "ball bearing",
    "shaft_coupling":    "shaft coupling",
    "profile_extrusion": "aluminium extrusion profile",
    "snap_hook":         "snap hook",
    "thread_insert":     "threaded insert",
    "hinge":             "hinge",
    "clamp":             "clamp",
    "handle":            "handle",
    "enclosure_lid":     "enclosure lid",
    "gusset":            "gusset plate",
    "sheet_metal_panel": "sheet metal panel",
    "u_channel":         "U-channel",
    "sheet_metal_box":   "sheet metal box",
    "weld_bead":         "weld bead",
    "spoked_wheel":      "spoked wheel",
    "t_slot_plate":      "T-slot plate",
    "spring_clip":       "spring clip",
    "involute_gear":     "involute spur gear",
    "cam_profile":       "cam profile",
    "bellows":           "bellows",
    "compression_spring":"compression spring",
    "keyway_shaft":      "keyway shaft",
    "dovetail_joint":    "dovetail joint",
    "slot_plate":        "slotted plate",
    "pcb_enclosure":     "PCB enclosure",
    "bearing_pillow_block": "bearing pillow block",
    "cable_gland":       "cable gland",
    "hex_bolt":          "hex bolt",
    "hex_nut":           "hex nut",
    "socket_cap_screw":  "socket-head cap screw",
    "set_screw":         "set screw",
    "timing_pulley":     "timing pulley",
    "sprocket":          "sprocket",
    "rack":              "gear rack",
    "pipe_elbow":        "pipe elbow",
    "pipe_tee":          "pipe tee",
    "pipe_reducer":      "pipe reducer",
    "ring_gear":         "ring gear",
    "gopro_mount":       "GoPro mount",
    "hex_standoff":      "hex standoff",
    "t_nut":             "T-nut",
    "thrust_washer":     "thrust washer",
    "retaining_ring":    "retaining ring",
    "linear_bushing":    "linear bushing",
    "pipe_cap":          "pipe cap",
    "cross_dowel":       "cross dowel",
    "bevel_gear":        "bevel gear",
    "worm":              "worm shaft",
    "worm_gear":         "worm gear",
    "timing_belt":       "timing belt",
    "jaw_coupling_half": "jaw coupling half",
    "valve_body":        "valve body",
    "extension_spring":  "extension spring",
    "torsion_spring":    "torsion spring",
    "wave_spring":       "wave spring",
    "pipe_cross":        "pipe cross fitting",
    "orifice_plate":     "orifice plate",
    "rivet":             "rivet",
    "climbing_sloper":   "climbing sloper hold",
}


# ---------------------------------------------------------------------------
# Introspect a template function to find all param keys it reads via
#   params.get("key"  or  params.get('key'
# Returns a set of key strings.
# ---------------------------------------------------------------------------
_PARAM_KEY_RE = re.compile(r'params\.get\(["\'](\w+)["\']')

def _extract_param_keys(fn) -> set[str]:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return set()
    return set(_PARAM_KEY_RE.findall(src))


# ---------------------------------------------------------------------------
# Log-uniform sampler
# ---------------------------------------------------------------------------
def _log_uniform(rng: random.Random, lo: float, hi: float) -> float:
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


# ---------------------------------------------------------------------------
# Sample a realistic value for a given parameter key.
# Returns the sampled Python value (float / int / str).
# ---------------------------------------------------------------------------
def _sample_param(rng: random.Random, key: str) -> Any:
    k = key.lower()

    # Counts
    if any(x in k for x in ("n_bolt", "n_teeth", "n_blades", "n_fins",
                              "n_spoke", "n_groove", "n_mount", "n_hole")):
        return rng.randint(2, 24)
    if "n_" in k and any(x in k for x in ("step", "turn", "coil", "layer", "lobe", "slot", "rib")):
        return rng.randint(2, 16)
    if k.startswith("n_") or k in ("n_teeth", "n_bolts", "n_fins", "n_blades",
                                     "n_spokes", "n_grooves"):
        return rng.randint(2, 20)

    # Angles
    if any(x in k for x in ("angle", "deg", "sweep", "helix")):
        return round(rng.uniform(5.0, 85.0), 1)

    # Materials
    if "material" in k:
        return rng.choice(_MATERIALS)

    # Blade sweep direction
    if "blade_sweep" in k:
        return rng.choice(_BLADE_SWEEPS)

    # Small dimensions (wall thickness, chamfer, fillet, pitch, module, wire dia)
    if any(x in k for x in ("wall", "chamfer", "fillet", "module", "pitch",
                              "wire_dia", "tooth_w", "land", "relief",
                              "groove_w", "groove_d", "flange_th",
                              "ramp_rise", "step_h", "slot_w", "slot_d")):
        return round(_log_uniform(rng, 1.0, 20.0), 2)

    # Thread/bolt diameters (metric M2..M24)
    if any(x in k for x in ("bolt_dia", "screw_dia", "thread_dia",
                              "set_screw_dia", "wire", "rod_dia")):
        return rng.choice([2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 24.0])

    # Radius (bolt circle, fillet)
    if "bolt_circle_r" in k or "pcd" in k or "circle_r" in k:
        return round(_log_uniform(rng, 15.0, 200.0), 1)

    # Lengths, widths, diameters — log-uniform in [10, 500] mm
    if any(x in k for x in ("od_mm", "bore_mm", "id_mm", "outer_dia",
                              "inner_dia", "outer_diameter", "inner_diameter",
                              "diameter", "width", "depth", "length", "height",
                              "thickness", "flange_od", "hub_od", "drum_od",
                              "flange_diameter", "hub_diameter", "drum_width",
                              "entry_r", "exit_r", "throat_r", "conv_length",
                              "base_thick", "fin_height", "fin_thick",
                              "leg_a", "leg_b", "plate_w", "plate_h",
                              "body_w", "body_h", "body_d", "boss_od",
                              "mount_d", "shoulder_d", "neck_od", "tail_od")):
        return round(_log_uniform(rng, 10.0, 500.0), 1)

    # Fallback: treat any remaining "_mm" suffixed key as a dimension
    if k.endswith("_mm") or k.endswith("_r"):
        return round(_log_uniform(rng, 10.0, 300.0), 1)

    # Boolean-ish flags (even integers 0/1)
    if k.startswith("has_") or k.startswith("with_") or k.startswith("enable_"):
        return rng.choice([0, 1])

    # Integer counts with no obvious key pattern
    if k.startswith("num_") or k.endswith("_count") or k.endswith("_num"):
        return rng.randint(1, 12)

    # Generic string
    if "material" in k or "finish" in k or "color" in k:
        return rng.choice(_MATERIALS)

    # Default fallback: a moderate dimension
    return round(_log_uniform(rng, 10.0, 200.0), 1)


# ---------------------------------------------------------------------------
# Build a natural-language "goal" string from the template name + params.
# This stays deterministic given the params dict.
# ---------------------------------------------------------------------------
def _build_goal(template_name: str, params: dict[str, Any]) -> str:
    label = _PART_LABELS.get(template_name, template_name.replace("_", " "))

    # Collect the most informative params to mention
    parts: list[str] = []

    def _fmt(v) -> str:
        if isinstance(v, float):
            return f"{v:.0f}" if v == int(v) else f"{v:.1f}"
        return str(v)

    # Primary size descriptor
    if "od_mm" in params:
        parts.append(f"{_fmt(params['od_mm'])}mm OD")
    elif "diameter" in params:
        parts.append(f"{_fmt(params['diameter'])}mm diameter")
    elif "width_mm" in params and "height_mm" in params:
        parts.append(f"{_fmt(params['width_mm'])}x{_fmt(params['height_mm'])}mm")
    elif "width_mm" in params:
        parts.append(f"{_fmt(params['width_mm'])}mm wide")
    elif "diameter_mm" in params:
        parts.append(f"{_fmt(params['diameter_mm'])}mm diameter")
    elif "length_mm" in params:
        parts.append(f"{_fmt(params['length_mm'])}mm long")

    # Bore
    if "bore_mm" in params:
        parts.append(f"{_fmt(params['bore_mm'])}mm bore")

    # Height / thickness
    if "height_mm" in params and "od_mm" in params:
        parts.append(f"{_fmt(params['height_mm'])}mm tall")
    elif "thickness_mm" in params:
        parts.append(f"{_fmt(params['thickness_mm'])}mm thick")

    # Feature counts
    if "n_bolts" in params:
        bolt_dia = params.get("bolt_dia_mm", "")
        bolt_str = f"M{_fmt(bolt_dia)}" if bolt_dia else ""
        parts.append(f"{params['n_bolts']} {bolt_str} bolt holes".strip())
    if "n_teeth" in params:
        parts.append(f"{params['n_teeth']} teeth")
    if "n_blades" in params:
        parts.append(f"{params['n_blades']} blades")
    if "n_fins" in params:
        parts.append(f"{params['n_fins']} fins")
    if "n_spokes" in params:
        parts.append(f"{params['n_spokes']} spokes")
    if "n_grooves" in params:
        parts.append(f"{params['n_grooves']} grooves")

    # Blade sweep
    if "blade_sweep" in params:
        parts.append(f"{params['blade_sweep']}-swept blades")
    if "blade_angle_deg" in params:
        parts.append(f"{_fmt(params['blade_angle_deg'])}deg sweep")

    # Material
    if "material" in params:
        parts.append(str(params["material"]))

    if not parts:
        # Minimal fallback
        return f"{label}"

    return f"{label}: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Discover all _cq_<name> template functions from cadquery_generator,
# deduplicated by function object (different map keys may point to the
# same function).
# ---------------------------------------------------------------------------
def _discover_templates() -> list[tuple[str, Any]]:
    from aria_os.generators.cadquery_generator import _CQ_TEMPLATE_MAP  # type: ignore
    seen: dict[int, tuple[str, Any]] = {}
    for key, fn in _CQ_TEMPLATE_MAP.items():
        if id(fn) not in seen:
            # Derive the canonical template_name from the function name
            fn_name = getattr(fn, "__name__", key)
            if fn_name.startswith("_cq_"):
                template_name = fn_name[4:]  # strip _cq_
            else:
                template_name = key
            seen[id(fn)] = (template_name, fn)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Generate N parameter-sweep records for a single template function.
# Returns a list of dicts, skipping any that raise.
# ---------------------------------------------------------------------------
def _sweep_template(
    template_name: str,
    fn,
    n: int,
    rng: random.Random,
    errors: dict[str, int],
) -> list[dict[str, Any]]:
    keys = _extract_param_keys(fn)
    records: list[dict[str, Any]] = []

    for _ in range(n):
        params: dict[str, Any] = {k: _sample_param(rng, k) for k in keys}

        # Sanity clamp: bore must be < OD when both present
        if "bore_mm" in params and "od_mm" in params:
            if params["bore_mm"] >= params["od_mm"]:
                params["bore_mm"] = params["od_mm"] * rng.uniform(0.2, 0.6)
                params["bore_mm"] = round(params["bore_mm"], 1)

        # For nozzle: throat < entry and exit > throat
        if template_name == "nozzle":
            if "throat_r_mm" in params and "entry_r_mm" in params:
                params["throat_r_mm"] = round(params["entry_r_mm"] * rng.uniform(0.3, 0.7), 1)
            if "throat_r_mm" in params and "exit_r_mm" in params:
                params["exit_r_mm"] = round(params.get("throat_r_mm", 20.0) * rng.uniform(1.5, 4.0), 1)

        try:
            code = fn(params)
        except Exception as exc:
            err_type = type(exc).__name__
            errors[err_type] += 1
            continue

        if not code or not isinstance(code, str):
            errors["EmptyCode"] += 1
            continue

        goal = _build_goal(template_name, params)
        records.append({
            "goal":     goal,
            "params":   params,
            "code":     code,
            "template": template_name,
        })

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic CadQuery fine-tuning dataset."
    )
    parser.add_argument(
        "--n", type=int, default=50,
        help="Parameter sweeps per template (default: 50)"
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="RNG seed (default: 42)"
    )
    parser.add_argument(
        "--out", type=str, default="",
        help="Output JSONL path (default: outputs/datasets/synthetic_cad_<timestamp>.jsonl)"
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)
    t0 = time.monotonic()

    # Resolve output path
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        out_path = _REPO_ROOT / "outputs" / "datasets" / f"synthetic_cad_{ts}.jsonl"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[build_synthetic_dataset] Discovering templates...")
    templates = _discover_templates()
    print(f"  {len(templates)} unique template functions found")
    print(f"  Sweeps per template: {args.n}")
    print(f"  Output: {out_path}")
    print()

    errors: dict[str, int] = defaultdict(int)
    all_records: list[dict[str, Any]] = []
    templates_hit: set[str] = set()

    for i, (template_name, fn) in enumerate(templates, 1):
        records = _sweep_template(template_name, fn, args.n, rng, errors)
        all_records.extend(records)
        if records:
            templates_hit.add(template_name)
        pct = int(i / len(templates) * 100)
        print(
            f"  [{pct:3d}%] {template_name:<30s}  "
            f"{len(records):>4d}/{args.n} ok",
            flush=True,
        )

    # Write JSONL
    with out_path.open("w", encoding="utf-8") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    elapsed = time.monotonic() - t0
    avg_code_len = (
        sum(len(r["code"]) for r in all_records) / len(all_records)
        if all_records else 0
    )

    # Summary
    total_errors = sum(errors.values())
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Templates discovered : {len(templates)}")
    print(f"  Templates with output: {len(templates_hit)}")
    print(f"  Successful triples   : {len(all_records)}")
    print(f"  Failed attempts      : {total_errors}")
    print(f"  Avg code length (B)  : {avg_code_len:.0f}")
    print(f"  Elapsed              : {elapsed:.1f}s")

    if errors:
        print()
        print("  Top error types:")
        for err_type, count in sorted(errors.items(), key=lambda x: -x[1])[:5]:
            print(f"    {err_type:<30s} {count}")

    if all_records:
        print()
        print("  First 3 goals:")
        for rec in all_records[:3]:
            print(f"    [{rec['template']}] {rec['goal']}")

    print()
    print(f"  Written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
