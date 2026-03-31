"""
ecad_validator.py — Electrical Rules Check (ERC) + Design Rules Check (DRC) engine.

Runs before SPICE simulation to catch wiring and layout problems in a generated
KiCad board description.  All checks are deterministic and require no external
tools (no KiCad installation needed).

Usage:
    from aria_os.ecad_validator import run_full_check
    result = run_full_check(description, components, pins, board_w, board_h)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ─── IPC-2221 trace-width lookup (1 oz copper, 10 °C rise, external layer) ────
# Columns: (min_current_A, min_trace_width_mm)
_IPC2221_TABLE: list[tuple[float, float]] = [
    (0.1,  0.10),
    (0.5,  0.25),
    (1.0,  0.40),
    (2.0,  0.65),
    (3.0,  0.90),
    (5.0,  1.20),
]

# Nets that must have a driving source component to be considered valid
_POWER_NETS = {"VCC", "VCC_3V3", "VCC_5V", "VCC_12V", "VCC_24V", "3V3", "5V", "12V", "24V", "GND"}

# Component value keywords that identify ICs / MCUs requiring decoupling
_IC_KEYWORDS = ("esp32", "stm32", "arduino", "mcu", "hx711", "mpu", "ams1117", "tp4056", "vesc")

# Component references whose output pins should never share a net with another
# output (GPIO conflict candidates)
_OUTPUT_REF_PREFIXES = ("U", "IC")

# Resistor value pattern: matches e.g. "330R", "10k", "4.7K", "100 kΩ", "1Mohm"
_RESISTOR_VALUE_RE = re.compile(
    r"(\d+\.?\d*)\s*(k|K|M|Ω|ohm|R)\b", re.IGNORECASE
)


def _ipc2221_min_width(current_a: float) -> float:
    """
    Return the minimum trace width in mm for the given current using the
    IPC-2221 precomputed table (1 oz copper, 10 °C temperature rise).

    Extrapolates linearly beyond the highest table entry.
    """
    if current_a <= 0:
        return 0.0
    # Walk table: return first entry whose current threshold >= requested
    for threshold, width in _IPC2221_TABLE:
        if current_a <= threshold:
            return width
    # Beyond table maximum (5 A): scale proportionally from last entry
    last_i, last_w = _IPC2221_TABLE[-1]
    return last_w * (current_a / last_i)


def _parse_resistor_ohms(value_str: str) -> float | None:
    """
    Parse a resistor value string to ohms.
    Returns None if the pattern is not recognised.

    Examples:
        "330R"    → 330.0
        "10k"     → 10_000.0
        "4.7K"    → 4_700.0
        "1M"      → 1_000_000.0
        "100 kΩ"  → 100_000.0
    """
    m = _RESISTOR_VALUE_RE.search(value_str)
    if not m:
        return None
    mantissa = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix in ("K", "KΩ"):
        return mantissa * 1_000.0
    if suffix == "M":
        return mantissa * 1_000_000.0
    # "R", "Ω", "OHM" → direct ohms
    return mantissa


@dataclass
class _ComponentMeta:
    """Lightweight view of a component dict used by the checks."""
    ref: str
    value: str
    description: str
    pins: list[str]
    net: str


def _coerce_components(components: list[dict]) -> list[_ComponentMeta]:
    """
    Convert raw component dicts (or Component dataclass instances) to
    _ComponentMeta.  Handles both dict and object-with-attributes forms.
    """
    out: list[_ComponentMeta] = []
    for c in (components or []):
        if isinstance(c, dict):
            ref   = str(c.get("ref",   ""))
            value = str(c.get("value", ""))
            desc  = str(c.get("description", ""))
            pins  = list(c.get("pins", []))
            net   = str(c.get("net",  ""))
        else:
            ref   = str(getattr(c, "ref",         ""))
            value = str(getattr(c, "value",        ""))
            desc  = str(getattr(c, "description",  ""))
            pins  = list(getattr(c, "pins",        []))
            net   = str(getattr(c, "net",          ""))
        out.append(_ComponentMeta(ref=ref, value=value, description=desc, pins=pins, net=net))
    return out


# ─── ERC ─────────────────────────────────────────────────────────────────────

def run_erc(
    board_desc: str,
    components: list[dict],
    pins: dict[str, Any],
) -> dict:
    """
    Electrical Rules Check.

    Checks performed
    ----------------
    1. Unconnected pins — any component pin with no net assigned.
    2. Floating power nets — VCC/GND nets with no source component present.
    3. GPIO conflicts — two output-type pins mapped to the same net.
    4. Missing decoupling caps — MCU/IC with a VCC pin but no 100 nF cap in BOM.
    5. Trace-width vs current — IPC-2221 lookup for expected current on each net.
    6. Pull-up/pull-down resistor sizing — flag values outside 1 kΩ–100 kΩ range.
    7. Power rail completeness — require LDO for MCUs, USB/boost for 5V, barrel for VESC.

    Parameters
    ----------
    board_desc : str
        Natural-language description of the board (used for current estimation).
    components : list[dict]
        List of component dicts or Component dataclass instances from the parser.
    pins : dict
        Firmware pin definitions dict (from extract_firmware_pins).  May be empty.

    Returns
    -------
    dict with keys:
        passed            : bool
        errors            : list[str]
        warnings          : list[str]
        trace_violations  : list[dict]
    """
    errors:   list[str]  = []
    warnings: list[str]  = []
    trace_violations: list[dict] = []

    if not components:
        warnings.append("ERC: no components provided — nothing to check")
        return {
            "passed": True,
            "errors": errors,
            "warnings": warnings,
            "trace_violations": trace_violations,
        }

    comps = _coerce_components(components)
    lower_desc = (board_desc or "").lower()

    # ── 1. Unconnected pins ────────────────────────────────────────────────────
    # The pcbnew generator does not emit per-pin net assignments, so we treat
    # any component with an explicit "pins" list but empty entries as unconnected.
    for c in comps:
        unconnected = [p for p in c.pins if not str(p).strip()]
        if unconnected:
            errors.append(
                f"ERC: {c.ref} ({c.value}) has {len(unconnected)} unconnected pin(s)"
            )

    # ── 2. Floating power nets ─────────────────────────────────────────────────
    # A power net is "sourced" if a regulator, MCU module, or barrel jack is present.
    has_3v3_source = any(
        "ams1117" in c.value.lower() or "3.3" in c.value or "ldo" in c.description.lower()
        for c in comps
    )
    has_5v_source = any(
        "usb" in c.description.lower() or "usb" in c.value.lower()
        for c in comps
    )
    has_12v_source = any(
        "barrel" in c.description.lower() or "barrel" in c.value.lower()
        for c in comps
    )
    has_gnd = len(comps) > 0  # GND is implicit when any component exists

    # Check that boards claiming certain voltages actually have a source
    if re.search(r"\b3\.3\s*v\b|3v3", lower_desc) and not has_3v3_source:
        warnings.append("ERC: board description mentions 3.3 V but no 3.3 V LDO found in BOM")
    if re.search(r"\b5\s*v\b", lower_desc) and not has_5v_source and not has_3v3_source:
        warnings.append("ERC: board description mentions 5 V but no USB or LDO source found in BOM")
    if re.search(r"\b12\s*v\b|\b24\s*v\b", lower_desc) and not has_12v_source:
        warnings.append("ERC: board description mentions 12/24 V but no barrel jack found in BOM")
    if not has_gnd:
        errors.append("ERC: no GND reference — board has zero components")

    # ── 3. GPIO conflicts ──────────────────────────────────────────────────────
    # Map: net_name → list[ref] of output-type components driving it
    # We model each MCU module as driving all its listed pins as outputs.
    net_drivers: dict[str, list[str]] = {}
    for c in comps:
        if c.ref[:1] in ("U", "I"):  # ICs / MCUs
            for pin in c.pins:
                net_name = str(pin).strip()
                if net_name:
                    net_drivers.setdefault(net_name, []).append(c.ref)
    for net, drivers in net_drivers.items():
        if len(drivers) > 1:
            errors.append(
                f"ERC: GPIO conflict — net '{net}' driven by multiple outputs: "
                + ", ".join(drivers)
            )

    # ── 4. Missing decoupling caps ─────────────────────────────────────────────
    has_decoupling_100nf = any(
        "100nf" in c.value.lower() or "100 nf" in c.value.lower()
        for c in comps
    )
    ic_present = any(
        any(kw in c.value.lower() or kw in c.description.lower() for kw in _IC_KEYWORDS)
        for c in comps
    )
    if ic_present and not has_decoupling_100nf:
        errors.append(
            "ERC: MCU/IC found but no 100 nF decoupling capacitor in BOM "
            "(add C_0402 per VCC pin)"
        )

    # ── 5. Trace width vs current (IPC-2221) ───────────────────────────────────
    # Estimate supply current from known loads present on the board.
    current_map: dict[str, float] = {}  # net_label → expected current A

    if any("esp32" in c.value.lower() for c in comps):
        current_map["3V3"] = current_map.get("3V3", 0.0) + 0.5   # ESP32 peak ~500 mA
    if any("stm32" in c.value.lower() for c in comps):
        current_map["3V3"] = current_map.get("3V3", 0.0) + 0.2   # STM32 ~200 mA
    if any("vesc" in c.description.lower() or "motor" in c.description.lower() for c in comps):
        current_map["12V"] = current_map.get("12V", 0.0) + 5.0   # VESC motor drive
    if any("barrel" in c.description.lower() for c in comps):
        current_map["12V"] = current_map.get("12V", 0.0) + 1.0   # input power trace
    if any("hx711" in c.value.lower() for c in comps):
        current_map["3V3"] = current_map.get("3V3", 0.0) + 0.01  # HX711 < 10 mA

    # Default trace width assumption from pcbnew generator: 0.25 mm signal,
    # 0.5 mm power.  Flag when estimated current exceeds what a 0.5 mm trace
    # can carry per IPC-2221.
    ASSUMED_POWER_TRACE_MM = 0.5
    ASSUMED_SIGNAL_TRACE_MM = 0.25

    for net_label, current_a in current_map.items():
        required = _ipc2221_min_width(current_a)
        # Use power trace assumption for supply rails
        actual = ASSUMED_POWER_TRACE_MM if net_label in _POWER_NETS else ASSUMED_SIGNAL_TRACE_MM
        if required > actual:
            trace_violations.append({
                "net":         net_label,
                "current_a":   round(current_a, 3),
                "required_mm": round(required, 3),
                "actual_mm":   actual,
                "verdict":     "VIOLATION",
            })
            errors.append(
                f"ERC: trace width violation on {net_label} — "
                f"need {required:.2f} mm for {current_a:.2f} A (assumed {actual:.2f} mm)"
            )
        else:
            trace_violations.append({
                "net":         net_label,
                "current_a":   round(current_a, 3),
                "required_mm": round(required, 3),
                "actual_mm":   actual,
                "verdict":     "OK",
            })

    # ── 6. Pull-up/pull-down resistor sizing ──────────────────────────────────
    # Flag resistors whose values are outside the safe pull range for 3.3 V GPIO.
    # Applies to: components whose description contains "pull" OR ref starts with "R".
    for c in comps:
        is_pullup_candidate = (
            "pull" in c.description.lower()
            or c.ref.startswith("R")
        )
        if not is_pullup_candidate:
            continue

        ohms = _parse_resistor_ohms(c.value)
        if ohms is None:
            continue  # value not parseable — skip

        if ohms > 100_000:
            warnings.append(
                f"ERC: {c.ref} ({c.value}) pull-up/down > 100 kΩ — "
                "GPIO edges may be too slow at 3.3 V"
            )
        elif ohms < 1_000:
            static_ma = 3.3 / ohms * 1_000  # mA
            warnings.append(
                f"ERC: {c.ref} ({c.value}) pull-up/down < 1 kΩ — "
                f"{static_ma:.1f} mA static draw (3.3 V / R)"
            )

    # ── 7. Power rail completeness ────────────────────────────────────────────
    # Require LDO when ESP32 or STM32 is present.
    has_esp32 = any("esp32" in c.value.lower() for c in comps)
    has_stm32 = any("stm32" in c.value.lower() for c in comps)
    has_ldo = any(
        "ams1117" in c.value.lower()
        or "ldo" in c.description.lower()
        or "3.3" in c.value
        for c in comps
    )
    if (has_esp32 or has_stm32) and not has_ldo:
        errors.append(
            "ERC: ESP32/STM32 present but no 3.3 V LDO found in BOM "
            "(add AMS1117-3.3 or equivalent)"
        )

    # Require USB input or boost converter when a 5V component is in the BOM.
    has_5v_component = any(
        "5v" in c.description.lower() or "usb" in c.description.lower()
        for c in comps
    )
    has_usb_input = any(
        "usb" in c.value.lower() or "usb" in c.description.lower()
        for c in comps
    )
    has_boost = any(
        "boost" in c.description.lower() or "boost" in c.value.lower()
        for c in comps
    )
    if has_5v_component and not has_usb_input and not has_boost:
        errors.append(
            "ERC: 5 V component present but no USB input or boost converter found in BOM"
        )

    # Require barrel jack / 12 V input when VESC or motor driver is in the BOM.
    has_vesc_or_motor = any(
        "vesc" in c.description.lower()
        or "vesc" in c.value.lower()
        or "motor driver" in c.description.lower()
        for c in comps
    )
    has_barrel = any(
        "barrel" in c.description.lower() or "barrel" in c.value.lower()
        or "12v" in c.description.lower() or "12v" in c.value.lower()
        for c in comps
    )
    if has_vesc_or_motor and not has_barrel:
        errors.append(
            "ERC: VESC/motor driver present but no barrel jack or 12 V input found in BOM"
        )

    passed = len(errors) == 0
    return {
        "passed":           passed,
        "errors":           errors,
        "warnings":         warnings,
        "trace_violations": trace_violations,
    }


# ─── DRC ─────────────────────────────────────────────────────────────────────

def run_drc(
    board_w_mm: float,
    board_h_mm: float,
    components: list[dict],
) -> dict:
    """
    Design Rules Check.

    Checks performed
    ----------------
    1. Component bounding-box overlap (axis-aligned).
    2. IPC-standard 5 mm keepout from board edge (3 mm for JST connectors).
    3. JST connector 3 mm keepout from board edge (mating-force stress relief).

    Parameters
    ----------
    board_w_mm, board_h_mm : float
        Board dimensions in millimetres.
    components : list[dict]
        List of component dicts or Component dataclass instances.

    Returns
    -------
    dict with keys:
        passed     : bool
        violations : list[str]
    """
    violations: list[str] = []

    if not components:
        return {"passed": True, "violations": violations}

    comps = _coerce_components(components)
    KEEPOUT_MM = 5.0
    JST_KEEPOUT_MM = 3.0

    # Build list of (ref, x, y, w, h) using .x_mm/.y_mm attributes or dict keys
    placed: list[tuple[str, float, float, float, float]] = []
    for raw, meta in zip(components, comps):
        if isinstance(raw, dict):
            x = float(raw.get("x_mm", 0.0))
            y = float(raw.get("y_mm", 0.0))
            w = float(raw.get("width_mm",  2.0))
            h = float(raw.get("height_mm", 2.0))
        else:
            x = float(getattr(raw, "x_mm",      0.0))
            y = float(getattr(raw, "y_mm",      0.0))
            w = float(getattr(raw, "width_mm",  2.0))
            h = float(getattr(raw, "height_mm", 2.0))
        placed.append((meta.ref, x, y, w, h))

    # ── 1. Bounding-box overlap ────────────────────────────────────────────────
    for i in range(len(placed)):
        ref_a, xa, ya, wa, ha = placed[i]
        ax1, ay1, ax2, ay2 = xa, ya, xa + wa, ya + ha
        for j in range(i + 1, len(placed)):
            ref_b, xb, yb, wb, hb = placed[j]
            bx1, by1, bx2, by2 = xb, yb, xb + wb, yb + hb
            # AABB intersection
            overlap_x = ax1 < bx2 and ax2 > bx1
            overlap_y = ay1 < by2 and ay2 > by1
            if overlap_x and overlap_y:
                violations.append(
                    f"DRC: component overlap — {ref_a} and {ref_b} bounding boxes intersect"
                )

    # ── 2 & 3. Edge keepout ───────────────────────────────────────────────────
    # JST connectors use a tighter 3 mm keepout (mating force stresses near edge).
    # All other components use the IPC standard 5 mm keepout.
    for (ref, x, y, w, h), meta in zip(placed, comps):
        is_jst = (
            "jst" in meta.description.lower()
            or "jst" in meta.value.lower()
        )
        keepout = JST_KEEPOUT_MM if is_jst else KEEPOUT_MM
        violation_label = (
            f"DRC: JST connector {ref} within 3 mm of board edge"
            if is_jst
            else None
        )

        too_close: list[str] = []
        if x < keepout:
            too_close.append(f"left edge (x={x:.1f} mm)")
        if y < keepout:
            too_close.append(f"top edge (y={y:.1f} mm)")
        if x + w > board_w_mm - keepout:
            too_close.append(f"right edge (x+w={x+w:.1f} mm, board_w={board_w_mm})")
        if y + h > board_h_mm - keepout:
            too_close.append(f"bottom edge (y+h={y+h:.1f} mm, board_h={board_h_mm})")

        for side in too_close:
            if is_jst:
                violations.append(f"DRC: JST connector {ref} within 3 mm of board edge — {side}")
            else:
                violations.append(f"DRC: {ref} violates 5 mm edge keepout — {side}")

    passed = len(violations) == 0
    return {"passed": passed, "violations": violations}


# ─── Combined check ───────────────────────────────────────────────────────────

def run_full_check(
    board_desc: str,
    components: list[dict],
    pins: dict[str, Any],
    board_w: float,
    board_h: float,
    *,
    out_dir: Path | None = None,
) -> dict:
    """
    Run ERC + DRC and return a combined result dict.

    Parameters
    ----------
    board_desc : str
        Natural-language board description.
    components : list[dict]
        Parsed component list.
    pins : dict
        Firmware pin assignments (from extract_firmware_pins).
    board_w, board_h : float
        Board dimensions in mm.
    out_dir : Path | None
        If provided, write full result to out_dir/validation.json.

    Returns
    -------
    dict with keys:
        passed   : bool   — True only if both ERC and DRC pass
        erc_pass : bool   — ERC sub-result pass flag
        drc_pass : bool   — DRC sub-result pass flag
        errors   : list[str]
        warnings : list[str]
        erc      : dict   — full ERC sub-result
        drc      : dict   — full DRC sub-result
    """
    erc_result = run_erc(board_desc, components or [], pins or {})
    drc_result = run_drc(board_w or 80.0, board_h or 60.0, components or [])

    erc_status = "PASS" if erc_result["passed"] else f"FAIL ({len(erc_result['errors'])} error(s))"
    drc_status = "PASS" if drc_result["passed"] else f"FAIL ({len(drc_result['violations'])} violation(s))"

    print(f"[ERC] {erc_status}")
    print(f"[DRC] {drc_status}")

    combined_errors   = list(erc_result["errors"])
    combined_warnings = list(erc_result["warnings"])

    if not drc_result["passed"]:
        combined_errors.extend(drc_result["violations"])

    passed = erc_result["passed"] and drc_result["passed"]

    result = {
        "passed":   passed,
        "erc_pass": erc_result["passed"],
        "drc_pass": drc_result["passed"],
        "errors":   combined_errors,
        "warnings": combined_warnings,
        "erc":      erc_result,
        "drc":      drc_result,
    }

    # ── Optional JSON logging ──────────────────────────────────────────────────
    if out_dir is not None:
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            validation_json = out_dir / "validation.json"
            validation_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(f"[ERC] Validation written → {validation_json}")
        except OSError as exc:
            print(f"[ERC] Warning: could not write validation.json — {exc}")

    return result
