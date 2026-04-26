"""W13 Path B — kicad_sch_writer no-connect fix regression test.

The bug: `_labels_at_pin_tips` skipped both `power_in` AND `input` etype
pins when no net was assigned. On a 64-pin STM32 with most GPIOs unused,
`input`-type pins flooded ERC with `pin_not_connected` violations (137
of 184 in the drone fixture baseline).

The fix: only skip `power_in` (where dangling means a real wiring bug).
Mark `input` and other unassigned pins as `(no_connect ...)` so ERC
treats them as deliberate.

This test asserts the fix by mocking pins and verifying the sexpr output.
"""
from aria_os.ecad.kicad_sch_writer import _labels_at_pin_tips


def _pin(num, etype, x=0.0, y=0.0):
    return {"number": str(num), "etype": etype, "x": x, "y": y}


def test_input_pin_without_net_gets_no_connect():
    """The audit bug: input pins without nets used to be silently
    skipped, leaving them floating in ERC. Now they emit no_connect."""
    pins = [_pin(7, "input", x=2.54, y=0.0)]   # NRST-style pin
    out = _labels_at_pin_tips(pins, net_map={}, inst_x=0, inst_y=0)
    s = "\n".join(out)
    assert "(no_connect" in s, (
        f"input pin without net should emit no_connect; got: {s!r}")


def test_passive_pin_without_net_gets_no_connect():
    """Passives like R/C two-pin components also get no_connect when
    floating."""
    pins = [_pin(1, "passive", x=0.0, y=2.54)]
    out = _labels_at_pin_tips(pins, net_map={}, inst_x=0, inst_y=0)
    assert "(no_connect" in "\n".join(out)


def test_power_in_pin_without_net_NOT_marked_no_connect():
    """Skipping power_in is intentional -- a dangling power_in pin
    should keep being flagged by ERC because it's a real wiring bug,
    not deliberate float. The fix preserves this."""
    pins = [_pin(19, "power_in", x=0.0, y=0.0)]   # VDD
    out = _labels_at_pin_tips(pins, net_map={}, inst_x=0, inst_y=0)
    s = "\n".join(out)
    assert "(no_connect" not in s, (
        f"power_in pin without net should NOT emit no_connect; got: {s!r}")


def test_pin_with_assigned_net_emits_global_label_not_no_connect():
    """Sanity: pins WITH a net still get a global_label, not no_connect."""
    pins = [_pin(7, "input", x=0.0, y=0.0)]
    out = _labels_at_pin_tips(pins, net_map={"7": "~RESET~"}, inst_x=0, inst_y=0)
    s = "\n".join(out)
    assert "(global_label" in s
    assert "~RESET~" in s
    assert "(no_connect" not in s


def test_mixed_pins_correct_distribution():
    """One component with three pin types -- one wired (label),
    one floating power_in (skipped), one floating input (no_connect)."""
    pins = [
        _pin(1,  "power_in", x=0.0,  y=0.0),  # VBAT, unwired -> SKIP
        _pin(7,  "input",    x=2.54, y=0.0),  # NRST, unwired -> no_connect
        _pin(19, "power_in", x=5.08, y=0.0),  # VDD wired -> label
    ]
    net_map = {"19": "+3V3"}
    out = _labels_at_pin_tips(pins, net_map=net_map, inst_x=0, inst_y=0)
    joined = "\n".join(out)
    assert joined.count("(global_label") == 1
    assert joined.count("(no_connect") == 1
