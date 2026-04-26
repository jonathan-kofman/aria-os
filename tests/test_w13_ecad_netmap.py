"""W13 — ECAD net-map coverage tests.

Locks in the audit's #1 leverage call:
  STM32_PAD_NETS used to declare ~10 pin entries (all power/ground),
  leaving 52/64 pins floating and producing 137/184 ERC violations.
  This test asserts the always-wired non-power pins (VBAT, HSE crystal,
  NRST, USB OTG FS, SWD debug) are now declared so generic STM32F405
  designs net those pins automatically.

If someone later trims the dict back to power-only, this test fails.
"""
from aria_os.ecad.ecad_generator import STM32_PAD_NETS


# Conservative "always-wired" pins the audit fix added. Anything beyond
# these is GPIO and legitimately floats in a generic design.
ALWAYS_WIRED_PIN_NETS = {
    # Backup domain
    "1":  "+3V3",      # VBAT
    # HSE crystal
    "5":  "OSC_IN",
    "6":  "OSC_OUT",
    # Reset
    "7":  "~RESET~",
    # USB OTG FS
    "44": "USB_DM",
    "45": "USB_DP",
    # SWD debug
    "46": "SWDIO",
    "49": "SWCLK",
}


def test_stm32_pad_nets_total_count_increased():
    """The audit baseline was 11 entries; growing to 19 lifts net
    coverage on a generic STM32 design from ~17% (11/64) to ~30%."""
    assert len(STM32_PAD_NETS) >= 19, (
        f"STM32_PAD_NETS has {len(STM32_PAD_NETS)} entries; "
        f"audit fix requires at least 19."
    )


def test_stm32_pad_nets_includes_always_wired():
    """Every always-wired pin must be declared with the canonical net."""
    for pin, expected_net in ALWAYS_WIRED_PIN_NETS.items():
        assert pin in STM32_PAD_NETS, (
            f"STM32 pin {pin} ({expected_net}) missing from STM32_PAD_NETS")
        actual = STM32_PAD_NETS[pin]
        assert actual == expected_net, (
            f"STM32 pin {pin}: expected net {expected_net!r}, got {actual!r}")


def test_stm32_pad_nets_power_unchanged():
    """The original power/ground pins from the 2026-04-20 verification
    pass must still be present at their original nets."""
    power_pins = {
        "19": "+3V3", "32": "+3V3", "48": "+3V3", "64": "+3V3",
        "13": "+3V3",
        "18": "GND", "31": "GND", "47": "GND", "63": "GND",
        "12": "GND", "65": "GND",
    }
    for pin, net in power_pins.items():
        assert STM32_PAD_NETS.get(pin) == net, (
            f"Regression: STM32 power pin {pin} should be {net}, "
            f"got {STM32_PAD_NETS.get(pin)!r}")
