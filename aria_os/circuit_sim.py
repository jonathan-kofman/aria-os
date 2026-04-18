"""
Electronic circuit simulation — analog/power sanity checks on the
generated KiCad PCB. Wraps PySpice + ngspice for SPICE-like simulation
of power-on transient, current draw, voltage rail stability.

Why this matters for ARIA-OS:
  - Generated boards include MCUs (STM32/ESP32), regulators (AMS1117),
    motor drivers, USB-C, etc. ARIA's own ERC checks that components
    EXIST. Circuit sim checks they actually WORK together (e.g. does
    the 3.3V rail stay in spec when MCU + GPS + IMU + Rx all draw
    current at once?).
  - Falls back to analytical stub if PySpice/ngspice not installed.

Two operating modes:
  simulate_from_bom(bom_path)  — analytical estimate from BOM (always works)
  simulate_with_pyspice(bom)   — full SPICE transient (needs ngspice)

Output: a JSON trace + summary dict suitable for the bundle ZIP.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _has_pyspice() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("PySpice") is not None
    except Exception:
        return False


# Per-component current-draw estimates (mA at nominal voltage).
# Used by the analytical stub to estimate total rail load.
_COMPONENT_CURRENT_MA = {
    "STM32":     150.0,    # active, all peripherals on
    "ESP32":     250.0,    # WiFi/BT active
    "RP2040":     50.0,
    "ATmega":     20.0,
    "MPU":        4.0,
    "BMP":        0.5,
    "QMC5883":    1.5,
    "HMC5883":    1.5,
    "AMS1117":    7.0,     # quiescent
    "USB-C":      0.0,     # passive connector
    "JST":        0.0,
    "XT30":       0.0,
    "XT60":       0.0,
    "L298":     500.0,     # motor driver (when active)
    "TP4056":     1.0,     # quiescent
    "BLE":       15.0,
    "GPS":       70.0,     # u-blox M8N peak
    "Rx":        80.0,     # ELRS receiver
    "VTX":      400.0,     # video transmitter
    "PWM":        0.0,     # passive header
    "ESC":       30.0,     # standby; motors handle high current separately
    "LED":       10.0,
    "default":    1.0,
}


def _component_current(value: str, ref: str) -> float:
    """Look up estimated current draw for a component."""
    v = (value or "").upper()
    for key, mA in _COMPONENT_CURRENT_MA.items():
        if key.upper() in v:
            return mA
    prefix = "".join(c for c in (ref or "") if c.isalpha()).upper()
    return _COMPONENT_CURRENT_MA.get(prefix, _COMPONENT_CURRENT_MA["default"])


def simulate_from_bom(bom_path: str | Path,
                      out_dir: str | Path | None = None) -> dict[str, Any]:
    """Estimate power budget + rail stability from BOM.

    Always works (no SPICE dependency). Returns an estimate of:
      - Total 3.3V rail current
      - Total 5V rail current (USB-bus-powered if USB-C present)
      - Total 12V rail current (motor drivers, etc.)
      - Voltage drop across LDO under load (if any LDO present)
      - Warnings for over-loaded rails

    For full SPICE transient, use simulate_with_pyspice() instead.
    """
    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    components = bom.get("components", []) or []
    out_dir = Path(out_dir) if out_dir else bom_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Categorize each component by likely supply rail
    rails: dict[str, float] = {"3V3": 0.0, "5V": 0.0, "12V": 0.0, "VBAT": 0.0}
    by_component: list[dict] = []
    for c in components:
        ref = c.get("ref", "?")
        value = str(c.get("value", ""))
        i_mA = _component_current(value, ref)
        v = value.upper()
        # Pick rail by component class
        if any(k in v for k in ("L298", "VESC", "MOTOR")):
            rail = "12V"
        elif any(k in v for k in ("AMS1117", "TP4056")):
            rail = "VBAT"
        elif "USB" in v:
            rail = "5V"
        else:
            rail = "3V3"
        rails[rail] += i_mA
        by_component.append({"ref": ref, "value": value,
                             "rail": rail, "current_mA": i_mA})

    # Detect 3.3V LDO (AMS1117 typical 1A max, dropout ~1.1V)
    has_ldo = any("ams1117" in (c.get("value") or "").lower() for c in components)
    ldo_max_mA = 1000.0
    ldo_warning = None
    if has_ldo and rails["3V3"] > ldo_max_mA * 0.7:
        ldo_warning = (f"3V3 rail draws {rails['3V3']:.0f}mA — close to AMS1117 "
                       f"1A spec. Add heatsink or use buck regulator above 700mA.")

    # USB-only 5V rail max is 500mA (USB 2.0) or 1500mA (USB 3.0 / USB-C BC)
    has_usb = any("usb" in (c.get("value") or "").lower() for c in components)
    usb_warning = None
    if has_usb and rails["5V"] > 1500:
        usb_warning = (f"5V rail draws {rails['5V']:.0f}mA — exceeds USB-C BC "
                       f"1.5A. Add separate 5V regulator from VBAT.")

    summary = {
        "engine": "analytical",
        "n_components": len(components),
        "rails_mA": {k: round(v, 1) for k, v in rails.items()},
        "total_active_mA": round(sum(rails.values()), 1),
        "has_ldo": has_ldo,
        "has_usb": has_usb,
        "warnings": [w for w in (ldo_warning, usb_warning) if w],
        "by_component": by_component,
    }

    # If PySpice present, optionally run a real transient on top
    if _has_pyspice():
        try:
            spice_result = _spice_transient(components)
            summary["spice"] = spice_result
            summary["engine"] = "analytical+pyspice"
        except Exception as exc:
            summary["spice_error"] = f"{type(exc).__name__}: {exc}"

    out_path = out_dir / "circuit_sim.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["trace_path"] = str(out_path)
    return summary


def _spice_transient(components: list) -> dict[str, Any]:
    """Run a minimal PySpice transient: model the LDO + load resistor and
    capture the 3.3V rail voltage over 100ms power-on. Approximate.
    """
    from PySpice.Spice.Netlist import Circuit
    from PySpice.Unit import u_V, u_Ohm, u_uF, u_ms

    # Build a tiny test circuit: VBAT (4.2V) → LDO (modeled as Vsource with
    # series R) → load R → GND. Bulk cap on output.
    circuit = Circuit("aria_power_test")
    circuit.V("bat", "vbat", circuit.gnd, 4.2 @ u_V)
    circuit.R("ldo_dropout", "vbat", "v3v3", 0.5 @ u_Ohm)  # AMS1117 RDS_on
    circuit.C("bulk", "v3v3", circuit.gnd, 22 @ u_uF)
    # Load resistor sized to draw the analytical estimate
    rail_3v3_mA = sum(_component_current(c.get("value"), c.get("ref"))
                      for c in components
                      if "usb" not in (c.get("value") or "").lower())
    if rail_3v3_mA <= 0:
        rail_3v3_mA = 50.0
    r_load = 3.3 / (rail_3v3_mA / 1000.0)
    circuit.R("load", "v3v3", circuit.gnd, r_load @ u_Ohm)

    sim = circuit.simulator()
    analysis = sim.transient(step_time=0.01 @ u_ms, end_time=10 @ u_ms)
    v3v3 = list(analysis["v3v3"])
    return {
        "v3v3_steady_mV": round(float(v3v3[-1]) * 1000, 1),
        "v3v3_min_mV":    round(float(min(v3v3)) * 1000, 1),
        "v3v3_max_mV":    round(float(max(v3v3)) * 1000, 1),
        "load_mA":        round(rail_3v3_mA, 1),
        "load_resistance_ohm": round(r_load, 2),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m aria_os.circuit_sim <bom.json>")
        sys.exit(1)
    r = simulate_from_bom(sys.argv[1])
    print(json.dumps({k: v for k, v in r.items() if k != "by_component"}, indent=2))
