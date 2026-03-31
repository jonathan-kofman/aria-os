"""
aria_fault_behavior.py — ARIA Fault Behavior Specification & Simulator
=======================================================================
Defines exactly what ARIA does for every gray-area fault condition.
This is the spec that firmware must implement — not just ESTOP.

Two things in this file:
  1. FAULT_TABLE — the complete fault catalog with response per fault
  2. FaultSimulator — simulates fault injection for HIL testing

Usage:
    from aria_fault_behavior import FAULT_TABLE, FaultSimulator, print_fault_table
    print_fault_table()   # print all faults as a readable table
    sim = FaultSimulator()
    sim.inject('LOAD_CELL_DROPOUT', at_state='CLIMBING')

Fault severity levels:
  ESTOP     — motor disabled immediately, latch until power cycle
  HOLD      — motor holds current rope position, alert sent, wait for recovery
  DEGRADE   — continue with reduced functionality, log warning
  IGNORE    — log only, no state change (transient expected)

Author: Jonathan Kofman — ARIA Project
"""

from dataclasses import dataclass, field
from typing import Optional

# ═══════════════════════════════════════════════════════════════════════════════
# FAULT TABLE
# Each entry defines: what the fault is, what triggers it, what ARIA does,
# how it recovers, and which ANSI requirement it relates to.
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FaultEntry:
    id:              str   # unique fault code — matches firmware fault_t enum
    name:            str   # human-readable name
    trigger:         str   # what causes this fault to be detected
    severity:        str   # ESTOP / HOLD / DEGRADE / IGNORE
    motor_action:    str   # what the motor does immediately
    state_action:    str   # what state is entered
    led_pattern:     str   # LED strip behavior (blue = normal, amber = warn, red = fault)
    app_alert:       str   # what the app shows
    recovery:        str   # how the fault clears
    ansi_relevance:  str   # which ANSI Z359.14 requirement this satisfies
    notes:           str   = ""


FAULT_TABLE: list[FaultEntry] = [

    # ── Load cell faults ──────────────────────────────────────────────────────

    FaultEntry(
        id             = "LC_DROPOUT",
        name           = "Load cell dropout",
        trigger        = "HX711 returns no data for >200 ms (3 consecutive missed reads at 80 Hz)",
        severity       = "HOLD",
        motor_action   = "Hold current position immediately. Do not pay out.",
        state_action   = "Stay in current state. Set fault flag. Log timestamp.",
        led_pattern    = "Amber slow pulse (1 Hz)",
        app_alert      = "⚠ Tension sensor offline — device holding position",
        recovery       = "Auto-recover when HX711 returns valid data for 500 ms continuously. "
                         "If no recovery within 30 s, escalate to ESTOP.",
        ansi_relevance = "ANSI Z359.14 §4.5 — device must not release rope under sensor fault",
        notes          = "Most likely cause: loose JST connector or power supply droop. "
                         "HOLD is correct because we don't know the climber's state — "
                         "paying out rope during a sensor dropout is the worst outcome.",
    ),

    FaultEntry(
        id             = "LC_OVERRANGE",
        name           = "Load cell over-range",
        trigger        = "HX711 reading >120% of rated capacity (>60 kg on 50 kg cell = 588 N) "
                         "for >100 ms",
        severity       = "DEGRADE",
        motor_action   = "No change. Continue operating.",
        state_action   = "Log over-range event with timestamp and peak value. "
                         "If in CLIMBING, increase tension target by 10 N to reduce slack.",
        led_pattern    = "Normal",
        app_alert      = "ℹ Load cell saturation event logged",
        recovery       = "Auto-clears when reading returns below 100% rated capacity.",
        ansi_relevance = "ANSI Z359.14 §5.2 — load measurement accuracy",
        notes          = "Over-range during arrest is expected — the arrest force spike will "
                         "exceed climbing tension. This is not a fault during an arrest event "
                         "(detect arrest by rate-of-change, not absolute value).",
    ),

    FaultEntry(
        id             = "LC_FROZEN",
        name           = "Load cell frozen / stuck reading",
        trigger        = "HX711 returns identical value for >2 s while motor is running "
                         "(motor running implies rope movement implies tension should change)",
        severity       = "HOLD",
        motor_action   = "Stop motor. Hold.",
        state_action   = "Enter fault hold. Log frozen value and duration.",
        led_pattern    = "Amber fast pulse (4 Hz)",
        app_alert      = "⚠ Tension sensor frozen — device holding position",
        recovery       = "Power cycle. HX711 sometimes requires reset after I2C lockup. "
                         "Check wiring if recurring.",
        ansi_relevance = "ANSI Z359.14 §4.5",
        notes          = "A frozen reading with the motor running is always a sensor fault, "
                         "never normal operation.",
    ),

    # ── Encoder faults ────────────────────────────────────────────────────────

    FaultEntry(
        id             = "ENC_DROPOUT",
        name           = "Encoder SPI dropout",
        trigger        = "AS5048A SPI read returns error flag or no-response for >50 ms",
        severity       = "ESTOP",
        motor_action   = "Disable motor driver immediately. Apply passive brake.",
        state_action   = "Transition to ESTOP. Latch.",
        led_pattern    = "Red solid",
        app_alert      = "🚨 Position sensor failure — device stopped for safety",
        recovery       = "Power cycle only.",
        ansi_relevance = "ANSI Z359.14 §4.3 — position control required for controlled lower",
        notes          = "Without encoder feedback the motor cannot do FOC. "
                         "Unlike load cell dropout (where HOLD is safe), encoder dropout "
                         "during LOWER would result in uncontrolled descent — ESTOP is correct. "
                         "If encoder dropout occurs during TAKE or CLIMBING, "
                         "the mechanical catch still holds — ESTOP is safe.",
    ),

    FaultEntry(
        id             = "ENC_JUMP",
        name           = "Encoder position jump",
        trigger        = "AS5048A reading changes by >90 degrees in a single 1 ms sample "
                         "(physically impossible at any rope speed ARIA produces)",
        severity       = "HOLD",
        motor_action   = "Stop motor. Hold.",
        state_action   = "Log jump magnitude and timestamp. Hold current state.",
        led_pattern    = "Amber slow pulse",
        app_alert      = "⚠ Position sensor glitch — device holding position",
        recovery       = "Auto-recover if next 10 readings are consistent (no further jumps). "
                         "If jumps continue, escalate to ESTOP.",
        ansi_relevance = "ANSI Z359.14 §4.3",
        notes          = "Most likely EMI from motor wiring. Add ferrite bead on SPI lines "
                         "if this occurs frequently.",
    ),

    # ── Motor / driver faults ─────────────────────────────────────────────────

    FaultEntry(
        id             = "MOTOR_FAULT",
        name           = "VESC Mini fault flag",
        trigger        = "VESC UART reports fault_code != FAULT_CODE_NONE",
        severity       = "ESTOP",
        motor_action   = "VESC has already disabled output. Do not attempt restart.",
        state_action   = "Transition to ESTOP. Log VESC fault code. Send to app.",
        led_pattern    = "Red fast blink (8 Hz)",
        app_alert      = "🚨 Motor controller fault — device stopped. Code: [fault_code]",
        recovery       = "Power cycle. If fault recurs: check motor wiring, "
                         "reduce current limits in VESC config.",
        ansi_relevance = "ANSI Z359.14 §4.3",
        notes          = "Common VESC faults to expect: FAULT_CODE_OVER_TEMP_FET (reduce "
                         "current limit or improve cooling), FAULT_CODE_OVER_CURRENT "
                         "(reduce motor acceleration in config), FAULT_CODE_DRV (motor "
                         "phase short — inspect wiring).",
    ),

    FaultEntry(
        id             = "MOTOR_THERMAL",
        name           = "Motor over-temperature",
        trigger        = "Motor thermistor (if fitted) reads >80°C, OR VESC FET temp >70°C",
        severity       = "DEGRADE",
        motor_action   = "Reduce motor current limit to 50% of configured value.",
        state_action   = "Stay in current state. Log thermal event. Alert app.",
        led_pattern    = "Amber slow pulse",
        app_alert      = "⚠ Motor temperature high — performance reduced",
        recovery       = "Auto-recover when temperature drops below 65°C for 60 s continuously.",
        ansi_relevance = "Product safety — not specifically ANSI Z359.14",
        notes          = "Expected during long continuous sessions. At 50% current the "
                         "motor can still hold tension and lower — just more slowly. "
                         "Arrest is mechanical and unaffected by motor thermal state.",
    ),

    FaultEntry(
        id             = "MOTOR_BACKDRIVE",
        name           = "Motor back-drive detected",
        trigger        = "Encoder shows rope paying out while motor is commanded to HOLD or "
                         "RETRACT_HOLD, AND load cell shows decreasing tension. "
                         "Back-drive rate >5 mm/s for >500 ms.",
        severity       = "ESTOP",
        motor_action   = "Disable motor. One-way bearing should still hold. "
                         "If rope is still paying out after ESTOP, the one-way bearing has failed.",
        state_action   = "Transition to ESTOP. Critical fault log.",
        led_pattern    = "Red solid",
        app_alert      = "🚨 Rope movement during hold — device stopped. Inspect hardware.",
        recovery       = "Power cycle. Physical inspection required before returning to service.",
        ansi_relevance = "ANSI Z359.14 §4.2 — device must not release held load",
        notes          = "This should be physically impossible if the one-way sprag bearing "
                         "is correctly installed. If this fault fires, the sprag bearing has "
                         "failed or is installed backwards. This is the highest-severity "
                         "non-ESTOP-button fault in the system.",
    ),

    # ── UART / communication faults ───────────────────────────────────────────

    FaultEntry(
        id             = "UART_TIMEOUT",
        name           = "ESP32 UART heartbeat timeout",
        trigger        = "No valid UART message received from ESP32 for >2 s "
                         "(ESP32 sends heartbeat every 500 ms)",
        severity       = "DEGRADE",
        motor_action   = "No change. Continue operating without intelligence layer.",
        state_action   = "Disable voice and CV input processing. Log disconnection time. "
                         "Continue all mechanical/motor functions normally.",
        led_pattern    = "Blue with amber 0.5s flash every 5 s",
        app_alert      = "⚠ Voice/CV offline — device operating in mechanical-only mode",
        recovery       = "Auto-recover when UART heartbeat resumes. Re-enable voice and CV.",
        ansi_relevance = "ANSI Z359.14 §4.5 — mechanical arrest independent of electronics",
        notes          = "This is the key fault that demonstrates the two-layer architecture "
                         "working correctly. The mechanical catch and motor tension control "
                         "continue independently of the ESP32. "
                         "A climber can complete a full climb-arrest-lower cycle with the "
                         "ESP32 offline — they just can't use voice commands.",
    ),

    FaultEntry(
        id             = "UART_CORRUPT",
        name           = "UART message corruption",
        trigger        = "CRC check fails on incoming UART packet from ESP32",
        severity       = "IGNORE",
        motor_action   = "No change.",
        state_action   = "Discard corrupted packet. Increment corruption counter. "
                         "If corruption rate >10% over 30 s, escalate to UART_TIMEOUT handling.",
        led_pattern    = "Normal",
        app_alert      = "None (below threshold). Log only.",
        recovery       = "Auto-clears. Expected occasionally at high motor PWM frequencies "
                         "(EMI coupling into UART lines).",
        ansi_relevance = "N/A",
        notes          = "Add 100 ohm series resistors on UART TX/RX lines if this is frequent.",
    ),

    # ── Watchdog faults ───────────────────────────────────────────────────────

    FaultEntry(
        id             = "STM32_WATCHDOG",
        name           = "STM32 watchdog reset",
        trigger        = "IWDG not fed within WATCHDOG_TIMEOUT_MS (500 ms). "
                         "STM32 resets automatically.",
        severity       = "ESTOP",
        motor_action   = "Motor loses power on reset. One-way bearing holds.",
        state_action   = "On reboot: state = IDLE, all faults cleared. "
                         "Log watchdog reset event with timestamp to flash.",
        led_pattern    = "Red 3-blink pattern on boot if watchdog reset detected",
        app_alert      = "⚠ Device restarted unexpectedly — check activity log",
        recovery       = "Auto-recovery on reboot. If climber is still on wall "
                         "they must re-initiate from IDLE.",
        ansi_relevance = "ANSI Z359.14 §4.5",
        notes          = "The IWDG_TIMEOUT is 500 ms. Main loop runs at 50 Hz (20 ms). "
                         "Feed the watchdog every 20 ms in normal operation. "
                         "Any task blocking longer than 500 ms has a bug.",
    ),

    FaultEntry(
        id             = "ESP32_CRASH",
        name           = "ESP32 crash / reboot",
        trigger        = "ESP32 sends reboot notification packet, OR UART_TIMEOUT fires "
                         "followed by ESP32 sending init packet",
        severity       = "DEGRADE",
        motor_action   = "No change.",
        state_action   = "Same as UART_TIMEOUT: disable voice/CV, continue mechanical ops. "
                         "Re-enable when ESP32 sends ready packet after reboot.",
        led_pattern    = "Same as UART_TIMEOUT",
        app_alert      = "⚠ Intelligence module restarted — voice/CV temporarily offline",
        recovery       = "Auto-recover when ESP32 sends READY packet (typically <5 s reboot).",
        ansi_relevance = "ANSI Z359.14 §4.5 — mechanical arrest independent of ESP32",
        notes          = "ESP32 crashes are more common than STM32 crashes (larger codebase, "
                         "FreeRTOS, WiFi stack). Design for this being a routine event.",
    ),

    # ── Power faults ──────────────────────────────────────────────────────────

    FaultEntry(
        id             = "POWER_LOW",
        name           = "Supply voltage low",
        trigger        = "VCC monitored by STM32 ADC drops below 11.0 V "
                         "(nominal 12 V supply) for >500 ms",
        severity       = "DEGRADE",
        motor_action   = "Reduce motor current to 30% to extend operation.",
        state_action   = "Log undervoltage event. Alert app.",
        led_pattern    = "Amber fast pulse (4 Hz)",
        app_alert      = "⚠ Low supply voltage — check power connection",
        recovery       = "Auto-recover when voltage returns above 11.5 V for 2 s.",
        ansi_relevance = "Product safety",
        notes          = "Most likely cause: undersized power supply, loose barrel connector, "
                         "or shared circuit with high-current loads. ARIA needs a dedicated "
                         "12V 10A supply minimum.",
    ),

    FaultEntry(
        id             = "POWER_CRITICAL",
        name           = "Supply voltage critical",
        trigger        = "VCC drops below 9.0 V for >200 ms",
        severity       = "ESTOP",
        motor_action   = "Disable motor immediately. One-way bearing holds.",
        state_action   = "Transition to ESTOP. Log critical undervoltage.",
        led_pattern    = "Red solid",
        app_alert      = "🚨 Power failure — device stopped",
        recovery       = "Power cycle after supply is restored.",
        ansi_relevance = "ANSI Z359.14 §4.5",
        notes          = "Below 9 V the STM32 itself may reset. ESTOP before that happens.",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# FAULT SIMULATOR — for HIL testing
# ═══════════════════════════════════════════════════════════════════════════════

class FaultSimulator:
    """
    Injects fault conditions into the state machine for HIL testing.
    Used by aria_hil_test.py and the Test Session dashboard tab.
    """

    def __init__(self):
        self.active_faults: dict[str, float] = {}   # fault_id → time_s injected
        self.fault_log:     list[dict]        = []

    def inject(self, fault_id: str, at_time_s: float = 0.0) -> FaultEntry | None:
        """Inject a fault. Returns the FaultEntry or None if unknown."""
        entry = next((f for f in FAULT_TABLE if f.id == fault_id), None)
        if entry is None:
            return None
        self.active_faults[fault_id] = at_time_s
        self.fault_log.append({
            'fault_id':  fault_id,
            'severity':  entry.severity,
            'injected':  at_time_s,
            'cleared':   None,
        })
        return entry

    def clear(self, fault_id: str, at_time_s: float = 0.0):
        """Clear a fault (simulate recovery)."""
        if fault_id in self.active_faults:
            del self.active_faults[fault_id]
            for log in reversed(self.fault_log):
                if log['fault_id'] == fault_id and log['cleared'] is None:
                    log['cleared'] = at_time_s
                    break

    def is_active(self, fault_id: str) -> bool:
        return fault_id in self.active_faults

    def active_severity(self) -> str | None:
        """Return highest active severity, or None if no active faults."""
        priority = ['ESTOP', 'HOLD', 'DEGRADE', 'IGNORE']
        severities = set()
        for fid in self.active_faults:
            entry = next((f for f in FAULT_TABLE if f.id == fid), None)
            if entry:
                severities.add(entry.severity)
        for p in priority:
            if p in severities:
                return p
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# DISPLAY UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def print_fault_table():
    """Print all faults as a readable console table."""
    severity_order = {'ESTOP': 0, 'HOLD': 1, 'DEGRADE': 2, 'IGNORE': 3}
    sorted_faults  = sorted(FAULT_TABLE, key=lambda f: severity_order.get(f.severity, 9))

    print(f"\n{'='*80}")
    print(f"ARIA FAULT BEHAVIOR TABLE  ({len(FAULT_TABLE)} faults)")
    print(f"{'='*80}")
    for f in sorted_faults:
        sev_icons = {'ESTOP': '🔴', 'HOLD': '🟡', 'DEGRADE': '🟠', 'IGNORE': '⚪'}
        icon = sev_icons.get(f.severity, '?')
        print(f"\n{icon}  [{f.id}]  {f.name}  ({f.severity})")
        print(f"   Trigger: {f.trigger[:80]}")
        print(f"   Motor:   {f.motor_action[:80]}")
        print(f"   State:   {f.state_action[:80]}")
        print(f"   Recover: {f.recovery[:80]}")
        if f.notes:
            print(f"   Note:    {f.notes[:80]}")
    print(f"\n{'='*80}\n")


def render_fault_table_tab():
    """Streamlit tab showing the fault table. Add to dashboard routing."""
    import streamlit as st

    st.markdown("## Fault Behavior Reference")
    st.caption(
        "Defines exactly what ARIA does for every gray-area fault. "
        "These behaviors must be implemented in firmware and verified in HIL tests."
    )

    severity_colors = {
        'ESTOP':   ('#991B1B', '#FEE2E2'),
        'HOLD':    ('#92400E', '#FEF3C7'),
        'DEGRADE': ('#7C3AED', '#EDE9FE'),
        'IGNORE':  ('#374151', '#F9FAFB'),
    }

    filter_sev = st.multiselect(
        "Filter by severity",
        ['ESTOP', 'HOLD', 'DEGRADE', 'IGNORE'],
        default=['ESTOP', 'HOLD', 'DEGRADE', 'IGNORE'],
        key="fault_sev_filter"
    )

    import pandas as pd
    rows = []
    for f in FAULT_TABLE:
        if f.severity not in filter_sev:
            continue
        rows.append({
            'ID':        f.id,
            'Name':      f.name,
            'Severity':  f.severity,
            'Motor':     f.motor_action[:60] + '...' if len(f.motor_action) > 60 else f.motor_action,
            'Recovery':  f.recovery[:60] + '...' if len(f.recovery) > 60 else f.recovery,
            'ANSI':      f.ansi_relevance[:40] + '...' if len(f.ansi_relevance) > 40 else f.ansi_relevance,
        })

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Detail expander for selected fault
        selected = st.selectbox("View full details", [r['ID'] for r in rows], key="fault_detail_sel")
        fault = next((f for f in FAULT_TABLE if f.id == selected), None)
        if fault:
            tc, bg = severity_colors.get(fault.severity, ('#000', '#fff'))
            with st.expander(f"{fault.id} — {fault.name}", expanded=True):
                cols = st.columns(2)
                cols[0].markdown(f"**Severity:** {fault.severity}")
                cols[0].markdown(f"**LED:** {fault.led_pattern}")
                cols[1].markdown(f"**ANSI:** {fault.ansi_relevance}")
                st.markdown(f"**Trigger:** {fault.trigger}")
                st.markdown(f"**Motor action:** {fault.motor_action}")
                st.markdown(f"**State action:** {fault.state_action}")
                st.markdown(f"**App alert:** {fault.app_alert}")
                st.markdown(f"**Recovery:** {fault.recovery}")
                if fault.notes:
                    st.info(fault.notes)
    else:
        st.info("No faults match the selected severity filter.")


if __name__ == "__main__":
    print_fault_table()
