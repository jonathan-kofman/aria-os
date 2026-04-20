"""
Emit a KiCad 10 project bundle: `.kicad_pro` + `sym-lib-table` + `fp-lib-table`.

Purpose: make generated `.kicad_sch` and `.kicad_pcb` files open cleanly in
eeschema / pcbnew without the ARIA_ rename hack. The project-level lib-tables
let `(lib_id "Device:R")` / footprints `"Package_QFP:LQFP-64_..."` resolve
against KiCad's bundled libraries via `${KICAD10_SYMBOL_DIR}` /
`${KICAD10_FOOTPRINT_DIR}` env vars (KiCad 10 sets these automatically).

Primary sources researched 2026-04-19:
- KiCad 10 template tables at:
  <install>/share/kicad/template/sym-lib-table
  <install>/share/kicad/template/fp-lib-table
- Real demo projects (pic_programmer, amplifier-ac, video, ecc83) verified
  self-contained with embedded lib_symbols cache + minimal project tables.

Usage:
    from aria_os.ecad.kicad_project import emit_kicad_project

    emit_kicad_project(
        out_dir="outputs/boards/fc_pcb/",
        project_name="fc_pcb",
        sym_libs={"Device", "MCU_ST_STM32F4", "Sensor_Motion", "power"},
        fp_libs={"Package_QFP", "Resistor_SMD", "Capacitor_SMD",
                 "Package_LGA", "Connector_USB"},
    )
    # Writes: fc_pcb.kicad_pro, sym-lib-table, fp-lib-table into out_dir.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


_SYM_LIB_TABLE_HDR = '(sym_lib_table\n\t(version 7)\n'
_FP_LIB_TABLE_HDR  = '(fp_lib_table\n\t(version 7)\n'


# Short descriptions for the most common KiCad 10 libraries. Populated as
# needed; anything not listed gets an empty descr (legal).
_SYM_LIB_DESCRS = {
    "Device":                     "Basic devices (R, C, L, D, Q)",
    "power":                      "Power symbols (VCC, GND, rails)",
    "Connector":                  "Generic connectors",
    "Connector_Generic":          "Generic multi-pin connectors",
    "MCU_ST_STM32F4":             "STM32F4 family microcontrollers",
    "MCU_ST_STM32F3":             "STM32F3 family microcontrollers",
    "MCU_ST_STM32F1":             "STM32F1 family microcontrollers",
    "MCU_ST_STM32H7":             "STM32H7 family microcontrollers",
    "MCU_Microchip_ATmega":       "Microchip ATmega microcontrollers",
    "MCU_Microchip_PIC":          "Microchip PIC microcontrollers",
    "MCU_Espressif":              "Espressif ESP32/ESP8266 MCUs",
    "RF_Module":                  "RF modules (ESP32 WROOM, nRF, etc.)",
    "Sensor_Motion":              "Motion sensors (IMU, accel, gyro)",
    "Sensor_Pressure":            "Pressure / barometric sensors",
    "Sensor_Temperature":         "Temperature sensors",
    "Sensor_Magnetic":            "Magnetometers / hall sensors",
    "Sensor":                     "Miscellaneous sensors",
    "Amplifier_Operational":      "Operational amplifiers",
    "Regulator_Linear":           "Linear voltage regulators",
    "Regulator_Switching":        "Switching voltage regulators",
    "74xx":                       "74xx series logic",
    "Diode":                      "Diodes (signal, power, zener)",
    "Transistor_BJT":             "BJT transistors",
    "Transistor_FET":             "FET / MOSFET transistors",
    "Driver_Motor":               "Motor drivers",
}

_FP_LIB_DESCRS = {
    "Resistor_SMD":               "Resistors, SMD",
    "Resistor_THT":               "Resistors, through-hole",
    "Capacitor_SMD":              "Capacitors, SMD",
    "Capacitor_THT":              "Capacitors, through-hole",
    "Inductor_SMD":               "Inductors, SMD",
    "Inductor_THT":               "Inductors, through-hole",
    "Diode_SMD":                  "Diodes, SMD",
    "Diode_THT":                  "Diodes, through-hole",
    "LED_SMD":                    "LEDs, SMD",
    "LED_THT":                    "LEDs, through-hole",
    "Package_QFP":                "QFP packages",
    "Package_QFN":                "QFN packages",
    "Package_DFN_QFN":            "DFN / QFN packages",
    "Package_LGA":                "LGA packages",
    "Package_BGA":                "BGA packages",
    "Package_SO":                 "SOIC / SO packages",
    "Package_TO_SOT_SMD":         "TO / SOT SMD packages",
    "Package_TO_SOT_THT":         "TO / SOT through-hole",
    "Package_SIP":                "Single-inline packages",
    "Package_DIP":                "Dual-inline packages",
    "Connector":                  "Generic connectors",
    "Connector_Generic":          "Generic connector footprints",
    "Connector_USB":              "USB connectors",
    "Connector_JST":              "JST connectors",
    "Connector_Molex":            "Molex connectors",
    "Connector_PinHeader_2.54mm": "0.1in pin headers",
    "MountingHole":               "Mounting holes",
    "RF_Module":                  "RF module footprints",
}


def _lib_row(name: str, is_symbol: bool) -> str:
    descr_map = _SYM_LIB_DESCRS if is_symbol else _FP_LIB_DESCRS
    dir_var = "${KICAD10_SYMBOL_DIR}" if is_symbol else "${KICAD10_FOOTPRINT_DIR}"
    ext = ".kicad_sym" if is_symbol else ".pretty"
    descr = descr_map.get(name, "")
    return (f'\t(lib (name "{name}") (type "KiCad") '
            f'(uri "{dir_var}/{name}{ext}") '
            f'(options "") (descr "{descr}"))\n')


def emit_sym_lib_table(lib_names, out_path: Path) -> Path:
    """Write a sym-lib-table that references the given KiCad bundled
    symbol libraries (minus the `.kicad_sym` extension). `lib_names` is
    iterable of strings like {"Device", "MCU_ST_STM32F4"}.
    """
    body = _SYM_LIB_TABLE_HDR
    for name in sorted(set(lib_names)):
        body += _lib_row(name, is_symbol=True)
    body += ")\n"
    out_path.write_text(body, encoding="utf-8")
    return out_path


def emit_fp_lib_table(lib_names, out_path: Path) -> Path:
    """Write an fp-lib-table that references KiCad bundled footprint
    libraries (minus the `.pretty` extension).
    """
    body = _FP_LIB_TABLE_HDR
    for name in sorted(set(lib_names)):
        body += _lib_row(name, is_symbol=False)
    body += ")\n"
    out_path.write_text(body, encoding="utf-8")
    return out_path


# Minimum `.kicad_pro` content. KiCad 10 needs `meta`, `net_settings`,
# `board`, `schematic`, `pcbnew`, `text_variables`. Defaults work for
# everything not listed; any missing block causes KiCad to auto-populate
# on first open.
def _minimal_kicad_pro(project_name: str) -> dict:
    return {
        "board": {
            "3dviewports": [],
            "design_settings": {
                "defaults": {
                    "board_outline_line_width": 0.05,
                    "copper_line_width": 0.2,
                    "copper_text_size_h": 1.5,
                    "copper_text_size_v": 1.5,
                    "copper_text_thickness": 0.3,
                    "courtyard_line_width": 0.05,
                    "fab_line_width": 0.1,
                    "fab_text_size_h": 1.0,
                    "fab_text_size_v": 1.0,
                    "fab_text_thickness": 0.15,
                    "other_line_width": 0.1,
                    "pads": {"drill": 0.8, "height": 0.8, "width": 0.8},
                    "silk_line_width": 0.1,
                    "silk_text_size_h": 1.0,
                    "silk_text_size_v": 1.0,
                    "silk_text_thickness": 0.1,
                    "zones": {"min_clearance": 0.13},
                },
            },
            "layer_presets": [],
            "viewports": [],
        },
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {
            "pinned_footprint_libs": [],
            "pinned_symbol_libs": [],
        },
        "meta": {
            "filename": f"{project_name}.kicad_pro",
            "version": 3,
        },
        "net_settings": {
            "classes": [
                {
                    "bus_width": 12,
                    "clearance": 0.2,
                    "diff_pair_gap": 0.25,
                    "diff_pair_via_gap": 0.25,
                    "diff_pair_width": 0.2,
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Default",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "priority": 2147483647,
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "wire_width": 6,
                },
                {
                    "name": "Power",
                    "track_width": 0.5,
                    "via_diameter": 0.8,
                    "via_drill": 0.4,
                    "clearance": 0.25,
                    "priority": 10,
                },
                {
                    "name": "Signal",
                    "track_width": 0.2,
                    "via_diameter": 0.5,
                    "via_drill": 0.25,
                    "clearance": 0.15,
                    "priority": 20,
                },
                {
                    "name": "HS_Diff",
                    "track_width": 0.2,
                    "diff_pair_width": 0.15,
                    "diff_pair_gap": 0.2,
                    "via_diameter": 0.5,
                    "via_drill": 0.25,
                    "clearance": 0.15,
                    "priority": 30,
                },
            ],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {
            "last_paths": {
                "gencad": "",
                "idf": "",
                "netlist": "",
                "plot": "",
                "pos_files": "",
                "specctra_dsn": "",
                "step": "",
                "vrml": "",
            },
            "page_layout_descr_file": "",
        },
        "schematic": {
            "annotate_start_num": 0,
            "drawing": {
                "dashed_lines_dash_length_ratio": 12.0,
                "dashed_lines_gap_length_ratio": 3.0,
                "default_line_thickness": 6.0,
                "default_text_size": 50.0,
                "field_names": [],
                "intersheets_ref_own_page": False,
                "intersheets_ref_prefix": "",
                "intersheets_ref_short": False,
                "intersheets_ref_show": False,
                "intersheets_ref_suffix": "",
                "junction_size_choice": 3,
                "label_size_ratio": 0.375,
                "operating_point_overlay_i_precision": 3,
                "operating_point_overlay_i_range": "~A",
                "operating_point_overlay_v_precision": 3,
                "operating_point_overlay_v_range": "~V",
                "overbar_offset_ratio": 1.23,
                "pin_symbol_size": 25.0,
                "text_offset_ratio": 0.15,
            },
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "meta": {"version": 1},
            "net_format_name": "",
            "page_layout_descr_file": "",
            "plot_directory": "",
            "spice_current_sheet_as_root": False,
            "spice_external_command": "spice \"%I\"",
            "spice_model_current_sheet_as_root": True,
            "spice_save_all_currents": False,
            "spice_save_all_dissipations": False,
            "spice_save_all_voltages": False,
            "subpart_first_id": 65,
            "subpart_id_separator": 0,
        },
        "sheets": [[str(uuid4()), "Root"]],
        "text_variables": {},
    }


def emit_kicad_project(*, out_dir,
                        project_name: str,
                        sym_libs,
                        fp_libs) -> dict:
    """Top-level helper — write a complete project bundle.

    Returns dict of {kicad_pro, sym_lib_table, fp_lib_table} paths.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sym_path = out_dir / "sym-lib-table"
    fp_path = out_dir / "fp-lib-table"
    pro_path = out_dir / f"{project_name}.kicad_pro"

    emit_sym_lib_table(sym_libs, sym_path)
    emit_fp_lib_table(fp_libs, fp_path)
    pro_path.write_text(
        json.dumps(_minimal_kicad_pro(project_name), indent=2),
        encoding="utf-8")

    return {
        "kicad_pro":     str(pro_path),
        "sym_lib_table": str(sym_path),
        "fp_lib_table":  str(fp_path),
    }


def sym_lib_from_bom(bom: dict) -> set[str]:
    """Inspect a BOM and return the set of symbol-library names the
    schematic writer will need to reference. Uses kicad_symbol_lib.
    Unresolved components fall through silently — caller can detect via
    symbol lookup separately."""
    try:
        from .kicad_symbol_lib import lookup_symbol
    except Exception:
        return set()
    libs: set[str] = set()
    for comp in bom.get("components", []) or []:
        value = comp.get("value", "")
        if not value:
            continue
        sym = lookup_symbol(value)
        if sym is not None:
            libs.add(sym.get("lib_name", ""))
    libs.discard("")
    return libs


def fp_lib_from_bom(bom: dict) -> set[str]:
    """Inspect a BOM and return the set of footprint-library names.
    Pulls library name from the component's footprint field
    ('Package_QFP:LQFP-64_10x10mm_P0.5mm' -> 'Package_QFP').
    """
    libs: set[str] = set()
    for comp in bom.get("components", []) or []:
        fp_ref = comp.get("footprint", "")
        if ":" in fp_ref:
            libs.add(fp_ref.split(":", 1)[0].strip())
    libs.discard("")
    return libs


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m aria_os.ecad.kicad_project <bom.json> <out_dir>")
        raise SystemExit(2)
    bom = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sym_libs = sym_lib_from_bom(bom)
    fp_libs = fp_lib_from_bom(bom)
    paths = emit_kicad_project(
        out_dir=sys.argv[2],
        project_name=Path(sys.argv[1]).stem.replace("_bom", ""),
        sym_libs=sym_libs, fp_libs=fp_libs)
    print(f"symbol libs: {sorted(sym_libs)}")
    print(f"footprint libs: {sorted(fp_libs)}")
    print(f"wrote: {paths}")
