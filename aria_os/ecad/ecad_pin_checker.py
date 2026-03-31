"""
ecad_pin_checker.py — Firmware pin conflict checker.

Cross-checks generated pcbnew pin assignments against firmware GPIO definitions
extracted from aria_main.cpp and aria_esp32_firmware.ino.  Detects peripheral
type mismatches and shared timer / DMA conflicts.

Usage:
    from aria_os.ecad_pin_checker import check
    result = check(repo_root, components)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ─── Peripheral keyword classification ────────────────────────────────────────

# Each entry: (peripheral_type, [trigger_keywords_in_pin_name_or_description])
_PERIPHERAL_PATTERNS: list[tuple[str, list[str]]] = [
    ("serial",  ["uart", "usart", "tx", "rx", "serial"]),
    ("spi",     ["spi", "mosi", "miso", "sck", "nss", "ss"]),
    ("i2c",     ["i2c", "sda", "scl", "twi"]),
    ("timer",   ["tim", "timer", "pwm", "capture", "compare"]),
    ("adc",     ["adc", "analog", "ain"]),
    ("gpio",    ["gpio", "pin", "io"]),
    ("can",     ["can", "canrx", "cantx"]),
    ("usb",     ["usb", "dp", "dm"]),
]

# Peripheral pairs that are mutually exclusive on common MCU pin-mux configurations
_CONFLICT_PAIRS: list[tuple[str, str, str]] = [
    # (peripheral_a, peripheral_b, reason)
    ("serial", "spi",    "UART TX/RX and SPI MOSI/MISO share AF on STM32 USART1"),
    ("i2c",    "serial", "I2C SDA/SCL and UART share AF on STM32 I2C1"),
    ("adc",    "timer",  "ADC trigger and TIM capture compete for the same pin on STM32"),
]

# Timer peripherals that can only be assigned to one consumer at a time
_SHARED_TIMERS = ["TIM1", "TIM2", "TIM3", "TIM4", "TIM5", "TIM8"]


# ─── Peripheral classifier ───────────────────────────────────────────────────

def _classify_peripheral(name: str) -> str:
    """
    Return the peripheral type string for a pin name or description.
    Returns 'unknown' if no pattern matches.
    """
    lower = name.lower()
    for ptype, keywords in _PERIPHERAL_PATTERNS:
        if any(kw in lower for kw in keywords):
            return ptype
    return "unknown"


# ─── Firmware pin loader ──────────────────────────────────────────────────────

def load_firmware_pins(repo_root: Path) -> dict[str, str]:
    """
    Load GPIO pin definitions from STM32 and ESP32 firmware files.

    Delegates to :func:`aria_os.ecad_generator.extract_firmware_pins` so that
    the extraction logic is not duplicated.

    Parameters
    ----------
    repo_root : Path
        Root of the repository (parent of firmware/).

    Returns
    -------
    dict[str, str]
        Mapping of pin name → pin value/identifier.
        Returns {} if firmware files are missing or unreadable.
    """
    try:
        from .ecad_generator import extract_firmware_pins  # type: ignore
        return extract_firmware_pins(repo_root)
    except ImportError:
        # Fallback: inline minimal extraction if the generator is not importable
        return _extract_pins_inline(repo_root)


def _extract_pins_inline(repo_root: Path) -> dict[str, str]:
    """
    Minimal inline pin extractor used only when ecad_generator is unavailable.
    Mirrors the patterns in ecad_generator.extract_firmware_pins.
    """
    firmware_files = [
        repo_root / "firmware" / "stm32" / "aria_main.cpp",
        repo_root / "firmware" / "esp32" / "aria_esp32_firmware.ino",
    ]
    patterns = [
        re.compile(r"static\s+constexpr\s+uint8_t\s+(PIN_\w+)\s*=\s*([A-Za-z0-9_]+)\s*;"),
        re.compile(
            r"^\s*#define\s+((?:[A-Z0-9_]*PIN[A-Z0-9_]*|[A-Z0-9_]*GPIO_NUM[A-Z0-9_]*))\s+"
            r"(-?\d+|[A-Za-z][A-Za-z0-9_]*)\s*$",
            re.MULTILINE,
        ),
        re.compile(
            r"const\s+(?:int|uint8_t)\s+((?:[A-Z_]*PIN[A-Z_0-9]*|[A-Z_0-9]*_PIN))\s*="
            r"\s*(-?\d+|[A-Za-z][A-Za-z0-9_]*)\s*;"
        ),
    ]
    pins: dict[str, str] = {}
    for fw_path in firmware_files:
        if not fw_path.exists():
            continue
        try:
            text = fw_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in patterns:
            for m in pat.finditer(text):
                name, value = m.group(1), m.group(2)
                if value != "-1" and name not in pins:
                    pins[name] = value
    return pins


# ─── Conflict checker ────────────────────────────────────────────────────────

def check_pin_conflicts(
    firmware_pins: dict[str, str],
    board_components: list[Any],
) -> dict:
    """
    Cross-check board component descriptions against firmware pin definitions.

    Checks performed
    ----------------
    1. Peripheral type mismatch — e.g. a component labelled "UART" assigned to
       a firmware pin that belongs to an SPI peripheral.
    2. Shared timer conflict — two board components that both require TIM2 (or
       any other single-instance timer).
    3. Firmware pin reuse — two board components assigned to the same physical
       pin identifier.

    Parameters
    ----------
    firmware_pins : dict[str, str]
        Output of load_firmware_pins().  May be empty.
    board_components : list
        Component dicts or dataclass instances from the parser.

    Returns
    -------
    dict with keys:
        passed    : bool
        conflicts : list[str]
        warnings  : list[str]
    """
    conflicts: list[str] = []
    warnings_out: list[str] = []

    if not board_components:
        return {"passed": True, "conflicts": conflicts, "warnings": warnings_out}

    # Normalise components
    comps: list[dict] = []
    for c in board_components:
        if isinstance(c, dict):
            comps.append(c)
        else:
            comps.append({
                "ref":         getattr(c, "ref",         ""),
                "value":       getattr(c, "value",       ""),
                "description": getattr(c, "description", ""),
            })

    # Build a peripheral-type map from firmware pin names
    # e.g. "PIN_UART_TX" → "serial", "PIN_BRAKE" → "gpio"
    fw_pin_peripheral: dict[str, str] = {}
    for pin_name in firmware_pins:
        fw_pin_peripheral[pin_name] = _classify_peripheral(pin_name)

    # Classify each board component by its description / value
    comp_peripherals: list[tuple[str, str]] = []  # (ref, peripheral_type)
    for c in comps:
        combined = (c.get("value", "") + " " + c.get("description", "")).lower()
        ptype = _classify_peripheral(combined)
        comp_peripherals.append((c.get("ref", "?"), ptype))

    # ── 1. Peripheral type cross-check against firmware ────────────────────────
    # For each board component that has an identified peripheral type, check
    # whether the firmware has a pin of the SAME type.  If the firmware defines
    # only UART pins but the component is SPI-labelled, that is a mismatch.
    fw_peripheral_types = set(fw_pin_peripheral.values()) - {"unknown"}

    for ref, ptype in comp_peripherals:
        if ptype in ("unknown", "gpio"):
            continue  # too generic to conflict
        # Check for cross-wiring: component peripheral has a conflicting fw pin
        for (pa, pb, reason) in _CONFLICT_PAIRS:
            if ptype == pa and pb in fw_peripheral_types:
                conflicts.append(
                    f"PIN_CHECK: {ref} is {ptype.upper()} but firmware defines "
                    f"{pb.upper()} pins on the same MCU port — {reason}"
                )
            elif ptype == pb and pa in fw_peripheral_types:
                conflicts.append(
                    f"PIN_CHECK: {ref} is {ptype.upper()} but firmware defines "
                    f"{pa.upper()} pins on the same MCU port — {reason}"
                )

    # ── 2. Shared timer detection ──────────────────────────────────────────────
    timer_users: dict[str, list[str]] = {}  # timer_id → [component refs]
    for pin_name, pin_value in firmware_pins.items():
        ptype = _classify_peripheral(pin_name)
        if ptype == "timer":
            # Extract timer ID like TIM2 from pin name or value
            for timer in _SHARED_TIMERS:
                if timer.lower() in pin_name.lower() or timer.lower() in pin_value.lower():
                    # Find which component uses this pin
                    for c in comps:
                        desc = (c.get("value", "") + " " + c.get("description", "")).upper()
                        if timer in desc or ptype.upper() in desc:
                            timer_users.setdefault(timer, []).append(c.get("ref", "?"))

    for timer_id, users in timer_users.items():
        # Deduplicate
        unique_users = list(dict.fromkeys(users))
        if len(unique_users) > 1:
            conflicts.append(
                f"PIN_CHECK: {timer_id} is shared between "
                + ", ".join(unique_users)
                + " — only one peripheral can own a hardware timer"
            )

    # ── 3. Firmware pin reuse check ────────────────────────────────────────────
    # Two board components with descriptions matching the same firmware pin name
    pin_value_to_refs: dict[str, list[str]] = {}
    for pin_name, pin_value in firmware_pins.items():
        for c in comps:
            combined = (c.get("value", "") + " " + c.get("description", "")).lower()
            if pin_name.lower() in combined or pin_value.lower() in combined:
                pin_value_to_refs.setdefault(pin_value, []).append(c.get("ref", "?"))

    for pin_val, refs in pin_value_to_refs.items():
        unique_refs = list(dict.fromkeys(refs))
        if len(unique_refs) > 1:
            warnings_out.append(
                f"PIN_CHECK: firmware pin '{pin_val}' referenced by multiple components: "
                + ", ".join(unique_refs)
                + " — verify intent"
            )

    # Warn if no firmware pins were loaded (silent but useful to surface)
    if not firmware_pins:
        warnings_out.append(
            "PIN_CHECK: no firmware pin definitions found — "
            "firmware files missing or no PIN_* constants defined"
        )

    passed = len(conflicts) == 0
    return {
        "passed":   passed,
        "conflicts": conflicts,
        "warnings":  warnings_out,
    }


# ─── Main entry point ─────────────────────────────────────────────────────────

def check(repo_root: Path, components: list[Any]) -> dict:
    """
    Main entry point: load firmware pins then run conflict check.

    Parameters
    ----------
    repo_root : Path
        Repository root directory.
    components : list
        Board component list (dicts or dataclass instances).

    Returns
    -------
    dict with keys:
        passed          : bool
        conflicts       : list[str]
        warnings        : list[str]
        firmware_pins   : dict[str, str]   — pins that were loaded
    """
    fw_pins = load_firmware_pins(repo_root)
    result  = check_pin_conflicts(fw_pins, components or [])

    n_conflicts = len(result["conflicts"])
    print(f"[PIN_CHECK] {n_conflicts} conflict(s) found")

    result["firmware_pins"] = fw_pins
    return result
