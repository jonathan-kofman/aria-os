"""
ecad_generator.py — KiCad PCB script generator.

Parses a natural-language board description (regex + keyword matching + optional
LLM enrichment) and generates a pcbnew Python script the user runs inside
KiCad's scripting console.  The generated script places **real pads** on every
footprint, creates **nets** for power/signal routing, and adds **copper traces**
between connected pads.  Also writes a BOM JSON alongside.

Usage:
    from aria_os.ecad_generator import generate_ecad
    generate_ecad("ARIA ESP32 board, 80x60mm, 12V, UART, BLE")

    # CLI:
    python -m aria_os.ecad_generator "ARIA ESP32 board, 80x60mm, 12V, UART, BLE" --out outputs/ecad/
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent.parent

OUT_ECAD = ROOT / "outputs" / "ecad"

ECAD_BOM_SCHEMA_VERSION = "1.0"


# ─── Firmware pin extractor ───────────────────────────────────────────────────

def extract_firmware_pins(repo_root: Path) -> dict[str, str]:
    """
    Read STM32 and ESP32 firmware files and extract GPIO pin definitions.

    Patterns recognised:
      STM32 (aria_main.cpp):
        static constexpr uint8_t PIN_NAME = PA8;
        static constexpr uint8_t PIN_NAME = 13;
      ESP32 (aria_esp32_firmware.ino):
        #define PIN_NAME value
        #define PIN_NAME GPIO_NUM_X
        #define VARNAME_PIN value
        const int PIN_NAME = value;
        const uint8_t PIN_NAME = value;

    Returns dict like {"PIN_BRAKE": "PB13", "STM32_UART_TX": "43", ...}.
    Returns {} if firmware files are missing.
    """
    firmware_files = [
        repo_root / "device" / "firmware" / "stm32" / "aria_main.cpp",
        repo_root / "device" / "firmware" / "esp32" / "aria_esp32_firmware.ino",
    ]

    # Patterns in order of specificity:
    patterns = [
        # static constexpr uint8_t PIN_NAME = PA8;
        re.compile(
            r"static\s+constexpr\s+uint8_t\s+(PIN_\w+)\s*=\s*([A-Za-z0-9_]+)\s*;"
        ),
        # #define PIN_NAME value  or  #define VARNAME_GPIO_NUM value
        re.compile(
            r"^\s*#define\s+((?:[A-Z0-9_]*PIN[A-Z0-9_]*|[A-Z0-9_]*GPIO_NUM[A-Z0-9_]*))\s+(-?\d+|[A-Za-z][A-Za-z0-9_]*)\s*$",
            re.MULTILINE,
        ),
        # const int PIN_NAME = value;  or  const uint8_t PIN_NAME = value;
        re.compile(
            r"const\s+(?:int|uint8_t)\s+((?:[A-Z_]*PIN[A-Z_0-9]*|[A-Z_0-9]*_PIN))\s*=\s*(-?\d+|[A-Za-z][A-Za-z0-9_]*)\s*;"
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
                # Skip -1 sentinel values (camera "not connected")
                if value == "-1":
                    continue
                if name not in pins:
                    pins[name] = value

    return pins


# ─── Component descriptor ─────────────────────────────────────────────────────

@dataclass
class Component:
    ref: str
    value: str
    footprint: str           # KiCad footprint library:footprint
    width_mm: float          # bounding-box width for placement
    height_mm: float         # bounding-box height for placement
    description: str = ""
    qty: int = 1
    # placement filled in by placer
    x_mm: float = 0.0
    y_mm: float = 0.0
    # pad definitions: list of (name, rel_x_mm, rel_y_mm, w_mm, h_mm, shape)
    # shape: "rect" for pin 1, "oval" for others, "circle" for TH
    pads: list = field(default_factory=list)
    # net assignments: pad_name -> net_name
    net_map: dict = field(default_factory=dict)


# ─── MCU connectivity tables ─────────────────────────────────────────────────
# Maps logical function -> GPIO pin name for common MCUs.
# Used to auto-generate net connections between MCU and peripherals.

ESP32_CONNECTIONS: dict[str, str] = {
    "UART_TX": "GPIO1",
    "UART_RX": "GPIO3",
    "SPI_MOSI": "GPIO23",
    "SPI_MISO": "GPIO19",
    "SPI_CLK": "GPIO18",
    "SPI_CS": "GPIO5",
    "I2C_SDA": "GPIO21",
    "I2C_SCL": "GPIO22",
    "HX711_DOUT": "GPIO4",
    "HX711_SCK": "GPIO16",
    "PWM_SERVO": "GPIO25",
    "HALL_SENSOR": "GPIO34",
    "MOTOR_EN": "GPIO27",
    "LED_STATUS": "GPIO2",
}

STM32_CONNECTIONS: dict[str, str] = {
    "UART_TX": "PA9",
    "UART_RX": "PA10",
    "SPI_MOSI": "PA7",
    "SPI_MISO": "PA6",
    "SPI_CLK": "PA5",
    "SPI_CS": "PA4",
    "I2C_SDA": "PB7",
    "I2C_SCL": "PB6",
    "HX711_DOUT": "PB0",
    "HX711_SCK": "PB1",
    "PWM_SERVO": "PA8",
    "HALL_SENSOR": "PA0",
    "MOTOR_EN": "PB5",
    "LED_STATUS": "PC13",
}

ARDUINO_CONNECTIONS: dict[str, str] = {
    "UART_TX": "D1",
    "UART_RX": "D0",
    "SPI_MOSI": "D11",
    "SPI_MISO": "D12",
    "SPI_CLK": "D13",
    "SPI_CS": "D10",
    "I2C_SDA": "A4",
    "I2C_SCL": "A5",
    "HX711_DOUT": "D2",
    "HX711_SCK": "D3",
    "PWM_SERVO": "D9",
    "HALL_SENSOR": "A0",
    "MOTOR_EN": "D7",
    "LED_STATUS": "D4",
}


# ─── Pad definition tables ───────────────────────────────────────────────────
# Each entry: list of (pad_name, rel_x_mm, rel_y_mm, pad_w_mm, pad_h_mm, shape)
# shape: "rect" | "oval" | "circle"
# Net hints: dict mapping pad_name -> default net (VCC/GND/signal)

def _dual_row_pads(
    n_per_side: int,
    pitch: float,
    row_spacing: float,
    pad_w: float = 1.5,
    pad_h: float = 0.4,
) -> list[tuple[str, float, float, float, float, str]]:
    """Generate dual-row SMD pads (e.g., SOIC, QFP one side)."""
    pads = []
    half = (n_per_side - 1) * pitch / 2.0
    for i in range(n_per_side):
        y = -half + i * pitch
        # Left row
        name_l = str(i + 1)
        shape = "rect" if i == 0 else "oval"
        pads.append((name_l, -row_spacing / 2.0, y, pad_w, pad_h, shape))
        # Right row
        name_r = str(2 * n_per_side - i)
        pads.append((name_r, row_spacing / 2.0, y, pad_w, pad_h, "oval"))
    return pads


def _esp32_wroom_pads() -> list[tuple[str, float, float, float, float, str]]:
    """ESP32-WROOM-32 module pads: 38 castellated pads + GND pad."""
    pads = []
    # Left side: pins 1-18, right side: pins 20-38, bottom GND pad 39
    pitch = 1.27
    n_left = 18
    n_right = 18
    half_left = (n_left - 1) * pitch / 2.0
    for i in range(n_left):
        y = -half_left + i * pitch
        shape = "rect" if i == 0 else "oval"
        pads.append((str(i + 1), -8.0, y, 1.5, 0.6, shape))
    half_right = (n_right - 1) * pitch / 2.0
    for i in range(n_right):
        y = -half_right + i * pitch
        pads.append((str(20 + i), 8.0, y, 1.5, 0.6, "oval"))
    # Central GND pad
    pads.append(("39", 0.0, 12.0, 6.0, 6.0, "rect"))
    return pads

ESP32_PAD_NETS: dict[str, str] = {
    "1": "GND",
    "2": "+3V3",
    "39": "GND",
    # EN pin
    "3": "EN",
    # GPIO mapping for common peripherals (by pin number)
    "24": "GPIO1",   # UART TX
    "25": "GPIO3",   # UART RX
    "37": "GPIO23",  # SPI MOSI
    "31": "GPIO19",  # SPI MISO
    "30": "GPIO18",  # SPI CLK
    "29": "GPIO5",   # SPI CS
    "33": "GPIO21",  # I2C SDA
    "36": "GPIO22",  # I2C SCL
    "26": "GPIO4",   # HX711 DOUT
    "27": "GPIO16",  # HX711 SCK
    "10": "GPIO25",  # PWM
    "6": "GPIO34",   # ADC (Hall)
    "12": "GPIO27",  # Motor EN
    "22": "GPIO2",   # LED
}


def _stm32_lqfp64_pads() -> list[tuple[str, float, float, float, float, str]]:
    """STM32 LQFP-64 pads: 16 per side, quad flat pack."""
    pads = []
    pitch = 0.5
    n_per_side = 16
    half = (n_per_side - 1) * pitch / 2.0
    row_offset = 5.5  # center-to-row distance
    for side in range(4):
        for i in range(n_per_side):
            pin_num = side * n_per_side + i + 1
            pos_along = -half + i * pitch
            shape = "rect" if pin_num == 1 else "oval"
            if side == 0:    # bottom
                pads.append((str(pin_num), pos_along, row_offset, 0.3, 1.2, shape))
            elif side == 1:  # right
                pads.append((str(pin_num), row_offset, -pos_along, 1.2, 0.3, shape))
            elif side == 2:  # top
                pads.append((str(pin_num), -pos_along, -row_offset, 0.3, 1.2, shape))
            elif side == 3:  # left
                pads.append((str(pin_num), -row_offset, pos_along, 1.2, 0.3, shape))
    # Exposed pad
    pads.append(("65", 0.0, 0.0, 5.0, 5.0, "rect"))
    return pads

STM32_PAD_NETS: dict[str, str] = {
    # VDD pins
    "17": "+3V3", "32": "+3V3", "48": "+3V3", "64": "+3V3",
    # VSS pins (GND)
    "16": "GND", "31": "GND", "47": "GND", "63": "GND",
    "65": "GND",  # exposed pad
    # VDDA / VSSA
    "13": "+3V3",  # VDDA
    "12": "GND",   # VSSA
}


def _sot223_pads() -> list[tuple[str, float, float, float, float, str]]:
    """SOT-223-3 pads (LDO regulator)."""
    return [
        ("1", -2.3, 1.6, 1.2, 0.7, "rect"),   # GND / Adjust
        ("2", 0.0, 1.6, 1.2, 0.7, "oval"),     # Vout
        ("3", 2.3, 1.6, 1.2, 0.7, "oval"),     # Vin
        ("4", 0.0, -1.6, 3.6, 1.6, "oval"),    # Tab (Vout)
    ]

AMS1117_PAD_NETS: dict[str, str] = {
    "1": "GND",
    "2": "+3V3",
    "3": "VIN",
    "4": "+3V3",
}


def _barrel_jack_pads() -> list[tuple[str, float, float, float, float, str]]:
    """Barrel jack horizontal pads (2.1mm)."""
    return [
        ("1", 0.0, 0.0, 2.0, 2.0, "circle"),       # +VIN center pin
        ("2", -3.0, 4.7, 2.0, 2.0, "circle"),       # GND barrel
        ("3", 3.0, 4.7, 2.0, 2.0, "circle"),        # GND switch
    ]

BARREL_JACK_PAD_NETS: dict[str, str] = {
    "1": "VIN",
    "2": "GND",
    "3": "GND",
}


def _usb_c_pads() -> list[tuple[str, float, float, float, float, str]]:
    """USB-C receptacle simplified pads (power + CC + D+/D-)."""
    pads = []
    # VBUS pins
    pads.append(("A4", -2.5, 0.0, 0.6, 1.2, "oval"))
    pads.append(("B9", 2.5, 0.0, 0.6, 1.2, "oval"))
    # GND pins
    pads.append(("A1", -3.25, 0.0, 0.6, 1.2, "rect"))
    pads.append(("A12", 3.25, 0.0, 0.6, 1.2, "oval"))
    # CC pins
    pads.append(("A5", -1.75, 0.0, 0.6, 1.2, "oval"))
    pads.append(("B5", 1.75, 0.0, 0.6, 1.2, "oval"))
    # D+/D-
    pads.append(("A6", -1.0, 0.0, 0.3, 1.2, "oval"))
    pads.append(("A7", -0.25, 0.0, 0.3, 1.2, "oval"))
    pads.append(("B6", 0.25, 0.0, 0.3, 1.2, "oval"))
    pads.append(("B7", 1.0, 0.0, 0.3, 1.2, "oval"))
    # Shield
    pads.append(("S1", -4.32, -1.5, 1.0, 1.6, "oval"))
    pads.append(("S2", 4.32, -1.5, 1.0, 1.6, "oval"))
    return pads

USB_C_PAD_NETS: dict[str, str] = {
    "A4": "VBUS",
    "B9": "VBUS",
    "A1": "GND",
    "A12": "GND",
    "S1": "GND",
    "S2": "GND",
}


def _jst_xh_pads(n_pins: int) -> list[tuple[str, float, float, float, float, str]]:
    """JST-XH through-hole connector pads."""
    pads = []
    pitch = 2.5
    half = (n_pins - 1) * pitch / 2.0
    for i in range(n_pins):
        x = -half + i * pitch
        shape = "rect" if i == 0 else "circle"
        pads.append((str(i + 1), x, 0.0, 1.0, 1.7, shape))
    return pads


def _soic_pads(n_pins: int) -> list[tuple[str, float, float, float, float, str]]:
    """SOIC package pads (e.g. SOIC-8, SOIC-16)."""
    n_per_side = n_pins // 2
    pitch = 1.27
    row_spacing = {8: 5.4, 16: 7.5}.get(n_pins, 5.4)
    return _dual_row_pads(n_per_side, pitch, row_spacing, pad_w=1.5, pad_h=0.6)


def _pin_header_pads(n_pins: int, pitch: float = 2.54) -> list[tuple[str, float, float, float, float, str]]:
    """Single-row through-hole pin header."""
    pads = []
    half = (n_pins - 1) * pitch / 2.0
    for i in range(n_pins):
        x = -half + i * pitch
        shape = "rect" if i == 0 else "circle"
        pads.append((str(i + 1), x, 0.0, 1.7, 1.7, shape))
    return pads


def _passive_0402_pads() -> list[tuple[str, float, float, float, float, str]]:
    """0402 imperial (1005 metric) 2-pad footprint."""
    return [
        ("1", -0.48, 0.0, 0.56, 0.62, "rect"),
        ("2", 0.48, 0.0, 0.56, 0.62, "oval"),
    ]


def _passive_0805_pads() -> list[tuple[str, float, float, float, float, str]]:
    """0805 imperial (2012 metric) 2-pad footprint."""
    return [
        ("1", -0.95, 0.0, 1.0, 1.45, "rect"),
        ("2", 0.95, 0.0, 1.0, 1.45, "oval"),
    ]


def _molex_minifit_6p_pads() -> list[tuple[str, float, float, float, float, str]]:
    """Molex Mini-Fit Jr 2x3 through-hole pads."""
    pads = []
    pitch = 4.2
    for row in range(2):
        for col in range(3):
            pin = row * 3 + col + 1
            x = (col - 1) * pitch
            y = (row - 0.5) * pitch
            shape = "rect" if pin == 1 else "circle"
            pads.append((str(pin), x, y, 2.0, 2.0, shape))
    return pads


def _to92_pads() -> list[tuple[str, float, float, float, float, str]]:
    """TO-92 through-hole 3-pin package."""
    return [
        ("1", -1.27, 0.0, 1.0, 1.0, "rect"),
        ("2", 0.0, 0.0, 1.0, 1.0, "circle"),
        ("3", 1.27, 0.0, 1.0, 1.0, "circle"),
    ]


def _lga24_pads() -> list[tuple[str, float, float, float, float, str]]:
    """LGA-24 4x4mm pads (MPU-6050 / similar IMU)."""
    pads = []
    pitch = 0.5
    # 6 pads per side
    n_per_side = 6
    half = (n_per_side - 1) * pitch / 2.0
    offset = 2.0
    pin = 1
    for i in range(n_per_side):  # bottom
        pads.append((str(pin), -half + i * pitch, offset, 0.3, 0.8, "rect" if pin == 1 else "oval"))
        pin += 1
    for i in range(n_per_side):  # right
        pads.append((str(pin), offset, half - i * pitch, 0.8, 0.3, "oval"))
        pin += 1
    for i in range(n_per_side):  # top
        pads.append((str(pin), half - i * pitch, -offset, 0.3, 0.8, "oval"))
        pin += 1
    for i in range(n_per_side):  # left
        pads.append((str(pin), -offset, -half + i * pitch, 0.8, 0.3, "oval"))
        pin += 1
    return pads


def _antenna_chip_pads() -> list[tuple[str, float, float, float, float, str]]:
    """Chip antenna 2-pad footprint."""
    return [
        ("1", -0.6, 0.0, 0.6, 0.8, "rect"),
        ("2", 0.6, 0.0, 0.6, 0.8, "oval"),
    ]


def _jst_ph_2p_pads() -> list[tuple[str, float, float, float, float, str]]:
    """JST-PH 2-pin horizontal connector."""
    return [
        ("1", -1.0, 0.0, 1.0, 1.7, "rect"),
        ("2", 1.0, 0.0, 1.0, 1.7, "circle"),
    ]


# ─── Peripheral net-assignment helpers ────────────────────────────────────────
# Map connector ref-types to which pad gets which net for UART, I2C, SPI, etc.

UART_CONNECTOR_NETS: dict[str, str] = {
    "1": "UART_TX",
    "2": "UART_RX",
    "3": "+3V3",
    "4": "GND",
}

I2C_CONNECTOR_NETS: dict[str, str] = {
    "1": "I2C_SDA",
    "2": "I2C_SCL",
    "3": "+3V3",
    "4": "GND",
}

SPI_CONNECTOR_NETS: dict[str, str] = {
    "1": "SPI_MOSI",
    "2": "SPI_MISO",
    "3": "SPI_CLK",
    "4": "SPI_CS",
    "5": "+3V3",
    "6": "GND",
}

HX711_PAD_NETS: dict[str, str] = {
    # SOIC-16 HX711 pin net assignments
    "1": "HX711_VSUP",    # VSUP
    "2": "HX711_BASE",    # BASE
    "3": "HX711_AVDD",    # AVDD
    "4": "HX711_INA-",    # INA-
    "5": "HX711_INA+",    # INA+
    "6": "HX711_INB-",    # INB-
    "7": "HX711_INB+",    # INB+
    "8": "GND",           # GND (AGND)
    "9": "GND",           # GND (DGND)
    "10": "HX711_XO",     # XO
    "11": "HX711_DOUT",   # DOUT -> MCU
    "12": "HX711_SCK",    # PD_SCK -> MCU
    "13": "+3V3",         # DVDD
    "14": "HX711_RATE",   # RATE
    "15": "+3V3",         # VFB
    "16": "+3V3",         # VBG
}

LOADCELL_CONNECTOR_NETS: dict[str, str] = {
    "1": "HX711_INA+",
    "2": "HX711_INA-",
    "3": "HX711_AVDD",
    "4": "GND",
}

HALL_PAD_NETS: dict[str, str] = {
    "1": "+3V3",
    "2": "GND",
    "3": "HALL_SENSOR",
}

IMU_PAD_NETS: dict[str, str] = {
    # MPU-6050 LGA-24 key pins
    "1": "GND",
    "8": "+3V3",
    "9": "GND",
    "18": "GND",
    "23": "I2C_SCL",
    "24": "I2C_SDA",
}

MOTOR_CONNECTOR_NETS: dict[str, str] = {
    "1": "MOTOR_A+",
    "2": "MOTOR_A-",
    "3": "MOTOR_B+",
    "4": "MOTOR_B-",
    "5": "VIN",
    "6": "GND",
}

SERVO_CONNECTOR_NETS: dict[str, str] = {
    "1": "GND",
    "2": "+5V",
    "3": "PWM_SERVO",
}

LIPO_CONNECTOR_NETS: dict[str, str] = {
    "1": "VBAT",
    "2": "GND",
}


# ─── Parser ──────────────────────────────────────────────────────────────────

def _slug(description: str) -> str:
    """Convert a board description to a filesystem-safe name."""
    txt = description.lower()
    txt = re.sub(r"[^a-z0-9]+", "_", txt)
    txt = txt.strip("_")
    # Truncate to 48 chars so paths stay sane
    return txt[:48]


def parse_board_dimensions(description: str) -> tuple[float, float]:
    """Extract WxH in mm from description. Returns (80.0, 60.0) if not found."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xX\u00d7]\s*(\d+(?:\.\d+)?)\s*mm", description)
    if m:
        return float(m.group(1)), float(m.group(2))
    return 80.0, 60.0


def _assign_pads_and_nets(comp: Component) -> None:
    """Assign pad geometry and default net mappings to a component based on its value/footprint."""
    val_lower = comp.value.lower()
    fp_lower = comp.footprint.lower()

    # --- MCU modules ---
    if "esp32" in val_lower:
        comp.pads = _esp32_wroom_pads()
        comp.net_map = dict(ESP32_PAD_NETS)
    elif "stm32" in val_lower:
        comp.pads = _stm32_lqfp64_pads()
        comp.net_map = dict(STM32_PAD_NETS)
    elif "arduino" in val_lower:
        # Arduino Nano: 30 pin DIP
        comp.pads = _pin_header_pads(15, 2.54)  # simplified single row
        comp.net_map = {"1": "VIN", "2": "GND", "15": "+5V"}
    # --- Power ---
    elif "barrel" in val_lower:
        comp.pads = _barrel_jack_pads()
        comp.net_map = dict(BARREL_JACK_PAD_NETS)
    elif "usb-c" in val_lower or "usb_c" in val_lower:
        comp.pads = _usb_c_pads()
        comp.net_map = dict(USB_C_PAD_NETS)
    elif "ams1117" in val_lower:
        comp.pads = _sot223_pads()
        comp.net_map = dict(AMS1117_PAD_NETS)
    elif "tp4056" in val_lower:
        comp.pads = _soic_pads(8)
        comp.net_map = {"1": "VBAT", "3": "GND", "8": "VBUS"}
    elif "jst-ph-2p" in val_lower or "jst_ph" in fp_lower:
        comp.pads = _jst_ph_2p_pads()
        comp.net_map = dict(LIPO_CONNECTOR_NETS)
    # --- Connectors ---
    elif "uart" in val_lower and "jst" in val_lower:
        comp.pads = _jst_xh_pads(4)
        comp.net_map = dict(UART_CONNECTOR_NETS)
    elif "i2c" in val_lower and "jst" in val_lower:
        comp.pads = _jst_xh_pads(4)
        comp.net_map = dict(I2C_CONNECTOR_NETS)
    elif "spi" in val_lower and "jst" in val_lower:
        comp.pads = _jst_xh_pads(6)
        comp.net_map = dict(SPI_CONNECTOR_NETS)
    elif "loadcell" in val_lower or "load" in val_lower.replace(" ", ""):
        comp.pads = _jst_xh_pads(4)
        comp.net_map = dict(LOADCELL_CONNECTOR_NETS)
    elif "jst" in val_lower and "xh" in val_lower:
        # Generic JST-XH: guess pin count from footprint
        m = re.search(r"(\d+)x(\d+)", fp_lower)
        n = int(m.group(2)) if m else 4
        comp.pads = _jst_xh_pads(n)
    elif "motor" in val_lower or "molex" in val_lower:
        comp.pads = _molex_minifit_6p_pads()
        comp.net_map = dict(MOTOR_CONNECTOR_NETS)
    elif "servo" in val_lower:
        comp.pads = _pin_header_pads(3)
        comp.net_map = dict(SERVO_CONNECTOR_NETS)
    # --- Sensors ---
    elif "hx711" in val_lower:
        comp.pads = _soic_pads(16)
        comp.net_map = dict(HX711_PAD_NETS)
    elif "hall" in val_lower or "ss49e" in val_lower:
        comp.pads = _to92_pads()
        comp.net_map = dict(HALL_PAD_NETS)
    elif "mpu" in val_lower or "imu" in val_lower:
        comp.pads = _lga24_pads()
        comp.net_map = dict(IMU_PAD_NETS)
    # --- Antenna ---
    elif "antenna" in val_lower:
        comp.pads = _antenna_chip_pads()
        comp.net_map = {"1": "RF_ANT", "2": "GND"}
    # --- Passives ---
    elif comp.ref.startswith(("C", "R", "D")):
        if "0805" in fp_lower or "10u" in val_lower:
            comp.pads = _passive_0805_pads()
        else:
            comp.pads = _passive_0402_pads()
        # Decoupling caps: pin 1 to VCC, pin 2 to GND
        if comp.ref.startswith("C"):
            comp.net_map = {"1": "+3V3", "2": "GND"}
        elif comp.ref.startswith("D"):
            comp.net_map = {"1": "LED_ANODE", "2": "LED_CATHODE"}
        elif comp.ref.startswith("R"):
            comp.net_map = {"1": "LED_ANODE", "2": "+3V3"}


def parse_components(description: str) -> List[Component]:
    """
    Regex + keyword scan of the board description.
    Returns a list of Component descriptors, deduplicated.
    Each ref is auto-incremented per component type prefix.
    """
    lower = description.lower()
    components: List[Component] = []
    ref_counters: dict[str, int] = {}

    def next_ref(prefix: str) -> str:
        ref_counters[prefix] = ref_counters.get(prefix, 0) + 1
        return f"{prefix}{ref_counters[prefix]}"

    # ── MCU ──────────────────────────────────────────────────────────────────
    if "esp32" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="ESP32-S3-WROOM-1",
            footprint="RF_Module:ESP32-S3-WROOM-1",
            width_mm=18.0, height_mm=31.0,
            description="ESP32-S3 WiFi/BLE module",
        ))
    if "stm32" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="STM32F405RGT6",
            footprint="Package_QFP:LQFP-64_10x10mm_P0.5mm",
            width_mm=10.0, height_mm=10.0,
            description="STM32 ARM Cortex-M4 MCU",
        ))
    if "arduino" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="Arduino-Nano",
            footprint="Module:Arduino_Nano",
            width_mm=18.0, height_mm=43.0,
            description="Arduino Nano module",
        ))

    # ── Power supply ──────────────────────────────────────────────────────────
    if re.search(r"\b12\s*v\b", lower) or re.search(r"\b24\s*v\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="Barrel-Jack-2.1mm",
            footprint="Connector_BarrelJack:BarrelJack_Horizontal",
            width_mm=9.0, height_mm=11.0,
            description="DC barrel jack (12-24 V input)",
        ))
        components.append(Component(
            ref=next_ref("U"),
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            width_mm=3.5, height_mm=6.5,
            description="3.3 V LDO regulator",
        ))
    if re.search(r"\b5\s*v\b", lower) and "usb" not in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="USB-C-Receptacle",
            footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            width_mm=9.0, height_mm=3.5,
            description="USB-C 5 V power / data",
        ))
        components.append(Component(
            ref=next_ref("U"),
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            width_mm=3.5, height_mm=6.5,
            description="3.3 V LDO regulator",
        ))
    if re.search(r"\bbattery\b|\blipo\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="JST-PH-2P",
            footprint="Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal",
            width_mm=6.0, height_mm=5.5,
            description="LiPo battery connector (JST-PH 2-pin)",
        ))
        components.append(Component(
            ref=next_ref("U"),
            value="TP4056",
            footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            width_mm=4.0, height_mm=5.0,
            description="LiPo BMS / charger IC",
        ))

    # ── Interface connectors ──────────────────────────────────────────────────
    if "uart" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-4P-UART",
            footprint="Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
            width_mm=10.0, height_mm=7.5,
            description="UART header (TX/RX/3V3/GND)",
        ))
    if "i2c" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-4P-I2C",
            footprint="Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
            width_mm=10.0, height_mm=7.5,
            description="I2C header (SDA/SCL/3V3/GND)",
        ))
    if "spi" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-6P-SPI",
            footprint="Connector_JST:JST_XH_B6B-XH-A_1x06_P2.50mm_Vertical",
            width_mm=15.0, height_mm=7.5,
            description="SPI header (MOSI/MISO/SCK/CS/3V3/GND)",
        ))
    if "usb" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="USB-C-Receptacle",
            footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            width_mm=9.0, height_mm=3.5,
            description="USB-C connector",
        ))

    # ── BLE / RF ──────────────────────────────────────────────────────────────
    if re.search(r"\bble\b|\bbluetooth\b|\bantenna\b", lower):
        components.append(Component(
            ref=next_ref("ANT"),
            value="BLE-Antenna-Keepout",
            footprint="RF_Antenna:Antenna_Chip_TDK_ANT1608AI_0603",
            width_mm=3.2, height_mm=1.6,
            description="BLE antenna keepout zone marker",
        ))

    # ── Sensors ───────────────────────────────────────────────────────────────
    if "hx711" in lower or "load cell" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="HX711",
            footprint="Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
            width_mm=4.0, height_mm=10.0,
            description="HX711 24-bit load cell ADC",
        ))
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-4P-LoadCell",
            footprint="Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
            width_mm=10.0, height_mm=7.5,
            description="Load cell connector (E+/E-/A+/A-)",
        ))
    if re.search(r"\bhall\b", lower):
        components.append(Component(
            ref=next_ref("U"),
            value="SS49E-Hall",
            footprint="Package_TO_SOT_THT:TO-92_Inline",
            width_mm=5.0, height_mm=5.0,
            description="Hall effect sensor",
        ))
    if re.search(r"\bimu\b|\bmpu6050\b", lower):
        components.append(Component(
            ref=next_ref("U"),
            value="MPU-6050",
            footprint="Package_LGA:LGA-24_4x4mm_P0.5mm",
            width_mm=4.0, height_mm=4.0,
            description="MPU-6050 6-axis IMU",
        ))

    # ── Motor / actuator connectors ───────────────────────────────────────────
    if re.search(r"\bvesc\b|\bmotor\b|\bstepper\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="Molex-6P-Motor",
            footprint="Connector_Molex:Molex_Mini-Fit_Jr_5566-06A2_2x03_P4.20mm_Vertical",
            width_mm=12.6, height_mm=10.0,
            description="Motor / VESC 6-pin power connector",
        ))
    if re.search(r"\bservo\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="Servo-3P-PWM",
            footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            width_mm=7.62, height_mm=2.54,
            description="Servo PWM 3-pin header",
        ))

    # ── Passives: always add decoupling caps and a power LED ─────────────────
    components.append(Component(
        ref=next_ref("C"),
        value="100nF",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        width_mm=1.0, height_mm=0.5,
        description="100 nF decoupling capacitor",
        qty=4,
    ))
    components.append(Component(
        ref=next_ref("C"),
        value="10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
        width_mm=2.0, height_mm=1.25,
        description="10 uF bulk decoupling capacitor",
        qty=2,
    ))
    components.append(Component(
        ref=next_ref("D"),
        value="LED-Green",
        footprint="LED_SMD:LED_0402_1005Metric",
        width_mm=1.0, height_mm=0.5,
        description="Power indicator LED",
    ))
    components.append(Component(
        ref=next_ref("R"),
        value="330R",
        footprint="Resistor_SMD:R_0402_1005Metric",
        width_mm=1.0, height_mm=0.5,
        description="LED current-limiting resistor",
    ))

    # Assign pads and net hints to every component
    for comp in components:
        _assign_pads_and_nets(comp)

    return components


# ─── LLM enrichment ──────────────────────────────────────────────────────────

def _llm_enrich_components(
    description: str,
    components: List[Component],
    repo_root: Path,
) -> List[Component]:
    """
    Use LLM (Ollama or cloud) to verify the component list and suggest
    missing parts or pin connectivity improvements.

    Falls back gracefully to the regex-only list on any error.
    """
    component_summary = ", ".join(f"{c.ref}={c.value}" for c in components)

    # Try Ollama first (local, fast)
    try:
        from aria_os.agents.base_agent import _call_ollama
        from aria_os.agents.ollama_config import AGENT_MODELS

        prompt = (
            f"You are a PCB design engineer reviewing a component list.\n"
            f"Board description: {description}\n"
            f"Components found by regex: {component_summary}\n\n"
            f"1. Are any critical components MISSING for this board to function?\n"
            f"   (e.g., level shifters for UART, pull-up resistors for I2C, "
            f"bypass caps near ICs, ESD protection on USB)\n"
            f"2. Are there any clearly WRONG components?\n\n"
            f"Reply with ONLY a JSON object:\n"
            f'{{"missing": ["component description", ...], "wrong": ["ref: reason", ...], "pin_notes": ["note", ...]}}\n'
            f"If everything looks correct, return empty lists."
        )
        response = _call_ollama(
            prompt,
            "You are a senior PCB design engineer. Be concise. Reply ONLY with valid JSON.",
            AGENT_MODELS.get("spec", "qwen2.5-coder:7b"),
            json_mode=True,
        )
        if response:
            suggestions = json.loads(response)
            missing = suggestions.get("missing", [])
            if missing:
                print(f"[ecad] LLM suggests {len(missing)} missing component(s):")
                for m in missing:
                    print(f"[ecad]   + {m}")
            pin_notes = suggestions.get("pin_notes", [])
            if pin_notes:
                print(f"[ecad] LLM pin notes:")
                for n in pin_notes:
                    print(f"[ecad]   * {n}")
            return components
    except Exception:
        pass

    # Try cloud LLM as fallback
    try:
        from aria_os.llm_client import call_llm, get_anthropic_key, get_google_key
        if get_anthropic_key(repo_root) or get_google_key(repo_root):
            prompt = (
                f"Board: {description}\n"
                f"Components: {component_summary}\n\n"
                f"Are any critical components missing? List only truly necessary "
                f"ones (level shifters, pull-ups, protection diodes). Be very brief."
            )
            response = call_llm(prompt, system="You are a PCB engineer. Be concise.", repo_root=repo_root)
            if response:
                print(f"[ecad] LLM review: {response[:200]}")
    except Exception:
        pass

    return components


# ─── Placer ──────────────────────────────────────────────────────────────────

def place_components(components: List[Component], board_w: float, board_h: float) -> None:
    """
    Simple grid placement.
    - First MCU ("U1") is centred on the board.
    - Power connectors go top-right.
    - All other connectors flow around the perimeter (left column then right).
    - Small passives fill rows below the MCU.
    Modifies Component.x_mm / .y_mm in-place.
    """
    MARGIN = 5.0
    GAP    = 2.0

    mcu        = [c for c in components if c.ref.startswith("U") and c.height_mm > 15]
    power_j    = [c for c in components if c.ref.startswith("J") and ("barrel" in c.description.lower() or "usb" in c.description.lower() or "lipo" in c.description.lower())]
    sig_j      = [c for c in components if c.ref.startswith("J") and c not in power_j]
    ics        = [c for c in components if c.ref.startswith("U") and c not in mcu]
    passives   = [c for c in components if c.ref[0] in ("C", "R", "D", "ANT")]

    # MCU centred
    for comp in mcu:
        comp.x_mm = (board_w - comp.width_mm) / 2.0
        comp.y_mm = (board_h - comp.height_mm) / 2.0

    # Power connectors: top-right corner, stacked vertically
    cur_x = board_w - MARGIN
    cur_y = MARGIN
    for comp in power_j:
        comp.x_mm = cur_x - comp.width_mm
        comp.y_mm = cur_y
        cur_y += comp.height_mm + GAP

    # Signal connectors: left edge, top-to-bottom
    cur_x = MARGIN
    cur_y = MARGIN
    for comp in sig_j:
        comp.x_mm = cur_x
        comp.y_mm = cur_y
        cur_y += comp.height_mm + GAP
        if cur_y + comp.height_mm > board_h - MARGIN:
            cur_y = MARGIN
            cur_x += comp.width_mm + GAP

    # ICs: below/beside MCU, right side
    cx = board_w * 0.65
    cy = MARGIN
    for comp in ics:
        comp.x_mm = min(cx, board_w - MARGIN - comp.width_mm)
        comp.y_mm = cy
        cy += comp.height_mm + GAP

    # Passives: bottom row
    px = MARGIN
    py = board_h - MARGIN - 4.0
    for comp in passives:
        comp.x_mm = px
        comp.y_mm = py
        px += comp.width_mm + GAP
        if px + comp.width_mm > board_w - MARGIN:
            px = MARGIN
            py -= 4.0


# ─── Net and trace computation ────────────────────────────────────────────────

def _collect_nets(components: List[Component]) -> dict[str, list[tuple[str, str, float, float]]]:
    """
    Collect all net assignments across components.

    Returns: {net_name: [(ref, pad_name, abs_x_mm, abs_y_mm), ...]}
    """
    nets: dict[str, list[tuple[str, str, float, float]]] = {}
    for comp in components:
        pad_positions = {p[0]: (p[1], p[2]) for p in comp.pads}
        for pad_name, net_name in comp.net_map.items():
            if pad_name in pad_positions:
                rel_x, rel_y = pad_positions[pad_name]
                abs_x = comp.x_mm + rel_x
                abs_y = comp.y_mm + rel_y
                nets.setdefault(net_name, []).append((comp.ref, pad_name, abs_x, abs_y))
    return nets


def _compute_mcu_peripheral_nets(
    components: List[Component],
    description: str,
) -> None:
    """
    Wire MCU GPIO pins to peripheral connector pins using the connectivity
    tables.  Modifies component net_map entries in-place to add signal net
    names that match between MCU pad and peripheral pad.
    """
    lower = description.lower()

    # Find MCU component and its connection table
    mcu_comp = None
    conn_table = None
    for comp in components:
        val = comp.value.lower()
        if "esp32" in val:
            mcu_comp = comp
            conn_table = ESP32_CONNECTIONS
            break
        elif "stm32" in val:
            mcu_comp = comp
            conn_table = STM32_CONNECTIONS
            break
        elif "arduino" in val:
            mcu_comp = comp
            conn_table = ARDUINO_CONNECTIONS
            break

    if not mcu_comp or not conn_table:
        return

    # Build reverse map: GPIO pin name -> MCU pad name
    # For ESP32: we mapped pin numbers to GPIO names in ESP32_PAD_NETS
    mcu_gpio_to_pad: dict[str, str] = {}
    for pad_name, net_name in mcu_comp.net_map.items():
        if net_name.startswith("GPIO") or net_name.startswith("PA") or net_name.startswith("PB") or net_name.startswith("PC") or net_name.startswith("D") or net_name.startswith("A"):
            mcu_gpio_to_pad[net_name] = pad_name

    # Wire UART
    if "uart" in lower:
        for comp in components:
            if "uart" in comp.value.lower() and comp.ref.startswith("J"):
                # Connect UART_TX and UART_RX through MCU
                if "UART_TX" in conn_table:
                    gpio = conn_table["UART_TX"]
                    if gpio in mcu_gpio_to_pad:
                        mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "UART_TX"

                if "UART_RX" in conn_table:
                    gpio = conn_table["UART_RX"]
                    if gpio in mcu_gpio_to_pad:
                        mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "UART_RX"
                break

    # Wire I2C
    if "i2c" in lower or "imu" in lower or "mpu" in lower:
        if "I2C_SDA" in conn_table:
            gpio = conn_table["I2C_SDA"]
            if gpio in mcu_gpio_to_pad:
                mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "I2C_SDA"
        if "I2C_SCL" in conn_table:
            gpio = conn_table["I2C_SCL"]
            if gpio in mcu_gpio_to_pad:
                mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "I2C_SCL"

    # Wire SPI
    if "spi" in lower:
        for sig in ("SPI_MOSI", "SPI_MISO", "SPI_CLK", "SPI_CS"):
            if sig in conn_table:
                gpio = conn_table[sig]
                if gpio in mcu_gpio_to_pad:
                    mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = sig

    # Wire HX711
    if "hx711" in lower or "load cell" in lower:
        for sig in ("HX711_DOUT", "HX711_SCK"):
            if sig in conn_table:
                gpio = conn_table[sig]
                if gpio in mcu_gpio_to_pad:
                    mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = sig

    # Wire servo
    if "servo" in lower:
        if "PWM_SERVO" in conn_table:
            gpio = conn_table["PWM_SERVO"]
            if gpio in mcu_gpio_to_pad:
                mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "PWM_SERVO"

    # Wire Hall sensor
    if re.search(r"\bhall\b", lower):
        if "HALL_SENSOR" in conn_table:
            gpio = conn_table["HALL_SENSOR"]
            if gpio in mcu_gpio_to_pad:
                mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "HALL_SENSOR"

    # Wire motor enable
    if re.search(r"\bvesc\b|\bmotor\b", lower):
        if "MOTOR_EN" in conn_table:
            gpio = conn_table["MOTOR_EN"]
            if gpio in mcu_gpio_to_pad:
                mcu_comp.net_map[mcu_gpio_to_pad[gpio]] = "MOTOR_EN"


def _select_trace_width(net_name: str) -> float:
    """Return trace width in mm based on net type."""
    power_nets = {"VIN", "VBUS", "+5V", "VBAT", "MOTOR_A+", "MOTOR_A-", "MOTOR_B+", "MOTOR_B-"}
    if net_name in power_nets:
        return 0.5   # power traces: wider
    if net_name == "GND" or net_name == "+3V3":
        return 0.4   # power rail
    return 0.25      # signal traces


# ─── pcbnew script generation ─────────────────────────────────────────────────

_PCBNEW_HEADER = '''\
"""
Auto-generated KiCad pcbnew script.
Generated by aria_os/ecad_generator.py

Board:  {board_name}
Size:   {board_w} x {board_h} mm

HOW TO USE:
  1. Open KiCad 7+, create or open a project.
  2. Open PCB Editor (pcbnew).
  3. Tools -> Scripting Console -> paste or exec this file:
         exec(open(r"{script_path}").read())
  4. The board outline, footprints with real pads, nets, and copper traces
     are added automatically.  Press Ctrl+Z to undo if needed.
"""
import pcbnew

BOARD_W_MM = {board_w}
BOARD_H_MM = {board_h}

board = pcbnew.GetBoard()

def mm(v):
    return pcbnew.FromMM(v)

def pt(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))

# ── Board outline (Edge.Cuts) ──────────────────────────────────────────────────
def add_outline(board, w, h):
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetStart(pt(x1, y1))
        seg.SetEnd(pt(x2, y2))
        seg.SetWidth(mm(0.05))
        board.Add(seg)

add_outline(board, BOARD_W_MM, BOARD_H_MM)

# ── Mounting holes (M3, 4 corners, 3 mm from edge) ────────────────────────────
HOLE_OFFSET = 3.0
HOLE_DIA    = 3.2   # M3 clearance

def add_mounting_hole(board, x, y):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("MH")
    fp.SetValue("MountingHole_3.2mm")
    fp.SetPosition(pt(x, y))

    pad = pcbnew.PAD(fp)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetSize(pcbnew.VECTOR2I(mm(HOLE_DIA), mm(HOLE_DIA)))
    pad.SetDrillSize(pcbnew.VECTOR2I(mm(HOLE_DIA), mm(HOLE_DIA)))
    pad.SetPosition(pt(x, y))
    fp.Add(pad)
    board.Add(fp)

add_mounting_hole(board, HOLE_OFFSET,             HOLE_OFFSET)
add_mounting_hole(board, BOARD_W_MM - HOLE_OFFSET, HOLE_OFFSET)
add_mounting_hole(board, HOLE_OFFSET,             BOARD_H_MM - HOLE_OFFSET)
add_mounting_hole(board, BOARD_W_MM - HOLE_OFFSET, BOARD_H_MM - HOLE_OFFSET)

# ── Helper: add text label on silkscreen ──────────────────────────────────────
def silk_label(board, x, y, text, size_mm=1.0):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(text)
    t.SetPosition(pt(x, y))
    t.SetLayer(pcbnew.F_SilkS)
    t.SetTextSize(pcbnew.VECTOR2I(mm(size_mm), mm(size_mm)))
    t.SetTextThickness(mm(0.15))
    board.Add(t)

# Board title label
silk_label(board, 2.0, BOARD_H_MM - 3.5, "{board_name}", size_mm=1.2)

# ── Net registry ──────────────────────────────────────────────────────────────
_net_cache = {{}}

def get_or_create_net(board, name):
    """Get existing net or create a new NETINFO_ITEM."""
    if name in _net_cache:
        return _net_cache[name]
    ni = pcbnew.NETINFO_ITEM(board, name)
    board.Add(ni)
    _net_cache[name] = ni
    return ni

# Pre-create standard power nets
net_gnd  = get_or_create_net(board, "GND")
net_3v3  = get_or_create_net(board, "+3V3")
net_vin  = get_or_create_net(board, "VIN")
net_vbus = get_or_create_net(board, "VBUS")

# ── Helper: add footprint with real pads ──────────────────────────────────────
_placed_pads = {{}}  # (ref, pad_name) -> (net_name, abs_x, abs_y) for trace routing

def add_fp(board, ref, value, fp_id, x, y, pads_def, net_map, desc=""):
    """
    Place a footprint with actual pads and net assignments.

    pads_def: list of (pad_name, rel_x, rel_y, pad_w, pad_h, shape_str)
    net_map:  dict {{pad_name: net_name}}
    """
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(value)
    fp.SetPosition(pt(x, y))
    fp.SetLayer(pcbnew.F_Cu)

    for pad_name, rel_x, rel_y, pad_w, pad_h, shape_str in pads_def:
        pad = pcbnew.PAD(fp)
        pad.SetName(str(pad_name))

        # Pad shape
        if shape_str == "rect":
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        elif shape_str == "circle":
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        else:
            pad.SetShape(pcbnew.PAD_SHAPE_OVAL)

        # Through-hole vs SMD
        if shape_str == "circle" and pad_w > 1.2:
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            drill = max(0.8, pad_w * 0.6)
            pad.SetDrillSize(pcbnew.VECTOR2I(mm(drill), mm(drill)))
            pad.SetLayerSet(pad.PTHMask())
        else:
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pad.SMDMask())

        pad.SetSize(pcbnew.VECTOR2I(mm(pad_w), mm(pad_h)))
        abs_px = x + rel_x
        abs_py = y + rel_y
        pad.SetPosition(pt(abs_px, abs_py))

        # Net assignment
        net_name = net_map.get(str(pad_name))
        if net_name:
            net = get_or_create_net(board, net_name)
            pad.SetNet(net)
            _placed_pads[(ref, str(pad_name))] = (net_name, abs_px, abs_py)

        fp.Add(pad)

    # Silkscreen reference
    silk_label(board, x, y - max(3.0, max((abs(p[2]) for p in pads_def), default=2.0) + 2.0), ref, size_mm=0.8)
    board.Add(fp)
    return fp

'''

_PCBNEW_TRACE_HEADER = '''\

# ── Copper traces ─────────────────────────────────────────────────────────────
# Auto-routed point-to-point connections (star topology per net).
# For production boards, re-route with KiCad's interactive router.

def add_trace(board, x1, y1, x2, y2, width_mm, net_name, layer=pcbnew.F_Cu):
    """Add a copper trace segment between two points."""
    net = get_or_create_net(board, net_name)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pt(x1, y1))
    track.SetEnd(pt(x2, y2))
    track.SetWidth(mm(width_mm))
    track.SetLayer(layer)
    track.SetNet(net)
    board.Add(track)

'''

_PCBNEW_GND_ZONE = '''\

# ── Ground pour (back copper) ─────────────────────────────────────────────────
# Adds a solid GND copper zone on B.Cu covering the full board area.
zone = pcbnew.ZONE(board)
zone.SetIsRuleArea(False)
zone.SetLayer(pcbnew.B_Cu)
zone.SetNet(get_or_create_net(board, "GND"))
zone_outline = zone.Outline()
zone_outline.NewOutline()
zone_outline.Append(mm(0), mm(0))
zone_outline.Append(mm(BOARD_W_MM), mm(0))
zone_outline.Append(mm(BOARD_W_MM), mm(BOARD_H_MM))
zone_outline.Append(mm(0), mm(BOARD_H_MM))
zone.SetMinThickness(mm(0.2))
zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
zone.SetThermalReliefGap(mm(0.3))
zone.SetThermalReliefSpokeWidth(mm(0.4))
board.Add(zone)

'''

_PCBNEW_FOOTER = '''\

# ── Fill zones and refresh ────────────────────────────────────────────────────
filler = pcbnew.ZONE_FILLER(board)
filler.Fill(board.Zones())
pcbnew.Refresh()
board.Save(r"{kicad_pcb_path}")
print("[ecad] Saved:", r"{kicad_pcb_path}")
'''


def _build_pcbnew_script(
    board_name: str,
    board_w: float,
    board_h: float,
    components: List[Component],
    script_path: str,
    kicad_pcb_path: str,
) -> str:
    lines: list[str] = []
    lines.append(_PCBNEW_HEADER.format(
        board_name=board_name,
        board_w=board_w,
        board_h=board_h,
        script_path=script_path.replace("\\", "\\\\"),
    ))

    lines.append("# ── Components ───────────────────────────────────────────────────────────────")
    for comp in components:
        # Serialize pads as a list of tuples
        pads_str = repr(comp.pads)
        nets_str = repr(comp.net_map)
        desc_esc = comp.description.replace('"', '\\"')
        lines.append(
            f'add_fp(board, "{comp.ref}", "{comp.value}", '
            f'"{comp.footprint}", {comp.x_mm:.2f}, {comp.y_mm:.2f}, '
            f'{pads_str}, {nets_str}, "{desc_esc}")'
        )

    # Compute traces
    nets = _collect_nets(components)
    trace_lines = []
    for net_name, pads in nets.items():
        if len(pads) < 2:
            continue
        width = _select_trace_width(net_name)
        # Star topology: connect all pads to the first pad in the net
        anchor = pads[0]
        for other in pads[1:]:
            # Use B.Cu for GND traces (will be covered by ground pour too)
            layer = "pcbnew.B_Cu" if net_name == "GND" else "pcbnew.F_Cu"
            trace_lines.append(
                f'add_trace(board, {anchor[2]:.2f}, {anchor[3]:.2f}, '
                f'{other[2]:.2f}, {other[3]:.2f}, {width}, "{net_name}", {layer})'
            )

    lines.append("")
    lines.append(_PCBNEW_TRACE_HEADER)

    if trace_lines:
        lines.append(f"# {len(trace_lines)} trace segment(s) across {len([n for n in nets if len(nets[n]) >= 2])} net(s)")
        lines.extend(trace_lines)
    else:
        lines.append("# No multi-pad nets to route")

    # Ground pour
    lines.append(_PCBNEW_GND_ZONE)

    lines.append(_PCBNEW_FOOTER.format(
        kicad_pcb_path=kicad_pcb_path.replace("\\", "\\\\"),
    ))

    return "\n".join(lines)


# ─── BOM generation ──────────────────────────────────────────────────────────

def build_bom(components: List[Component]) -> dict:
    return {
        "schema_version": ECAD_BOM_SCHEMA_VERSION,
        "components": [
            {
                "ref":       c.ref,
                "value":     c.value,
                "footprint": c.footprint,
                "description": c.description,
                "qty":       c.qty,
                "x_mm":      round(c.x_mm, 2),
                "y_mm":      round(c.y_mm, 2),
                "pad_count": len(c.pads),
                "nets":      list(set(c.net_map.values())) if c.net_map else [],
            }
            for c in components
        ]
    }


# ─── Public API ──────────────────────────────────────────────────────────────

def _build_firmware_pin_comment_block(pins: dict[str, str]) -> str:
    """
    Return a Python comment block listing firmware pin assignments, suitable
    for injection near the top of the generated pcbnew script.
    """
    if not pins:
        return "# Firmware pin assignments: none found (firmware files missing or no pins matched)\n"
    lines = [
        "# Firmware pin assignments (from aria_main.cpp / aria_esp32_firmware.ino):",
    ]
    for name, value in sorted(pins.items()):
        lines.append(f"# {name} = {value}")
    lines.append("")
    return "\n".join(lines)


def generate_ecad(description: str, out_dir: Path | None = None) -> tuple[Path, Path]:
    """
    Parse description, place components, wire nets, write pcbnew script + BOM JSON.

    Returns (pcbnew_script_path, bom_json_path).
    """
    board_name = _slug(description)
    board_w, board_h = parse_board_dimensions(description)
    components = parse_components(description)

    # LLM enrichment: verify component list and suggest missing parts
    components = _llm_enrich_components(description, components, ROOT)

    # Wire MCU GPIOs to peripheral connectors based on connectivity tables
    _compute_mcu_peripheral_nets(components, description)

    place_components(components, board_w, board_h)

    # Extract firmware pin definitions and report
    fw_pins = extract_firmware_pins(ROOT)
    print(f"[ecad] Firmware pins found: {len(fw_pins)}")

    out_dir = Path(out_dir or OUT_ECAD) / board_name
    out_dir.mkdir(parents=True, exist_ok=True)

    script_path   = out_dir / f"{board_name}_pcbnew.py"
    bom_path      = out_dir / f"{board_name}_bom.json"
    kicad_pcb     = str(out_dir / f"{board_name}.kicad_pcb")

    pcbnew_src = _build_pcbnew_script(
        board_name=board_name,
        board_w=board_w,
        board_h=board_h,
        components=components,
        script_path=str(script_path),
        kicad_pcb_path=kicad_pcb,
    )

    # Inject firmware pin comment block immediately after the opening docstring /
    # import block (before the board-outline helpers).  We insert it just before
    # the first "BOARD_W_MM" assignment so it is visible at the top of the script.
    pin_block = _build_firmware_pin_comment_block(fw_pins)
    pcbnew_src = pcbnew_src.replace(
        "BOARD_W_MM = ",
        pin_block + "BOARD_W_MM = ",
        1,
    )

    script_path.write_text(pcbnew_src, encoding="utf-8")

    bom = build_bom(components)
    bom["firmware_pins"] = fw_pins

    # Summarize nets
    nets = _collect_nets(components)
    bom["nets"] = {
        name: [{"ref": r, "pad": p} for r, p, _, _ in pads]
        for name, pads in nets.items()
    }
    bom["net_count"] = len(nets)
    bom["trace_count"] = sum(max(0, len(pads) - 1) for pads in nets.values())

    bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"[ecad] Board:      {board_name}  ({board_w} x {board_h} mm)")
    print(f"[ecad] Components: {len(components)}")
    for c in components:
        pad_info = f"  {len(c.pads)} pads" if c.pads else ""
        net_info = f"  {len(c.net_map)} nets" if c.net_map else ""
        print(f"[ecad]   {c.ref:6s} {c.value:<30s} @ ({c.x_mm:.1f}, {c.y_mm:.1f}) mm{pad_info}{net_info}")
    print(f"[ecad] Nets:       {len(nets)}")
    print(f"[ecad] Traces:     {sum(max(0, len(p) - 1) for p in nets.values())} segments")
    print(f"[ecad] Script:     {script_path}")
    print(f"[ecad] BOM:        {bom_path}")

    # ── Validation pipeline ────────────────────────────────────────────────────
    try:
        from .ecad_validator import run_full_check
        from .ecad_simulator import simulate
        from .ecad_pin_checker import check as pin_check

        # Convert Component dataclasses to dicts for the validators
        def _components_to_dicts(comp_list: List[Component]) -> list[dict]:
            return [
                {
                    "ref":         c.ref,
                    "value":       c.value,
                    "description": c.description,
                    "x_mm":        c.x_mm,
                    "y_mm":        c.y_mm,
                    "width_mm":    c.width_mm,
                    "height_mm":   c.height_mm,
                    "pins":        [p[0] for p in c.pads],
                    "nets":        list(set(c.net_map.values())) if c.net_map else [],
                }
                for c in comp_list
            ]

        comp_dicts = _components_to_dicts(components)

        # Run validation with optional retry on ERC/DRC errors (up to 2 retries)
        _retry_count = 0
        _MAX_RETRIES = 2
        while True:
            _erc_result = run_full_check(
                description, comp_dicts, fw_pins, board_w, board_h,
                out_dir=out_dir,
            )

            _has_errors = bool(_erc_result.get("errors"))

            # Attempt LLM-assisted fix if errors are present and retries remain
            if _has_errors and _retry_count < _MAX_RETRIES:
                _llm_available = False
                try:
                    from aria_os.llm_client import call_llm, get_anthropic_key, get_google_key
                    _llm_available = bool(get_anthropic_key(ROOT) or get_google_key(ROOT))
                except ImportError:
                    pass

                if _llm_available:
                    _retry_count += 1
                    previous_failures = _erc_result["errors"]
                    _failure_block = "\n".join(f"  - {e}" for e in previous_failures)
                    _retry_prompt = (
                        f"The following ERC/DRC errors were found in a KiCad board generated "
                        f"for this description:\n  {description}\n\n"
                        f"Errors (attempt {_retry_count}):\n{_failure_block}\n\n"
                        f"List the missing or incorrect components that must be added or changed "
                        f"to fix these errors. Reply with a concise bullet list only."
                    )
                    print(
                        f"[ECAD] Validation FAIL -- {len(previous_failures)} error(s); "
                        f"retrying with LLM guidance (attempt {_retry_count}/{_MAX_RETRIES}) ..."
                    )
                    try:
                        _llm_advice = call_llm(_retry_prompt, repo_root=ROOT)
                    except Exception:
                        _llm_advice = None

                    # Inject failure context into the description for re-parse so
                    # parse_components picks up any newly needed keywords.
                    if _llm_advice:
                        _augmented_desc = (
                            description
                            + f"\n[RETRY {_retry_count} -- previous failures: "
                            + "; ".join(previous_failures)
                            + f"\nLLM advice: {_llm_advice}]"
                        )
                    else:
                        _augmented_desc = (
                            description
                            + f"\n[RETRY {_retry_count} -- previous failures: "
                            + "; ".join(previous_failures) + "]"
                        )

                    # Re-parse components with augmented description and re-place
                    _retry_components = parse_components(_augmented_desc)
                    _compute_mcu_peripheral_nets(_retry_components, _augmented_desc)
                    place_components(_retry_components, board_w, board_h)
                    comp_dicts = _components_to_dicts(_retry_components)
                    # Update the live components list so BOM reflects the fix
                    components = _retry_components
                    continue  # re-run validation loop

            # No more retries or no errors -- exit loop
            break

        _sim_result = simulate(description, comp_dicts)
        _pin_result = pin_check(ROOT, comp_dicts)

        bom["validation"] = {
            "erc":           _erc_result,
            "simulation":    _sim_result,
            "pin_conflicts": _pin_result,
            "passed": (
                _erc_result["passed"]
                and _sim_result["passed"]
                and _pin_result["passed"]
            ),
        }

        # Re-write BOM with validation results included
        bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

        _n_errors = len(_erc_result.get("errors", []))
        _val_status = "PASS" if bom["validation"]["passed"] else "FAIL"
        print(f"[ECAD] Validation {_val_status} -- {_n_errors} error(s)")

    except ImportError as _ie:
        # Validation modules not yet available -- skip gracefully
        print(f"[ecad] Validation skipped (import error: {_ie})")

    # ──────────────────────────────────────────────────────────────────────────

    return script_path, bom_path


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a KiCad pcbnew script from a natural-language board description"
    )
    parser.add_argument("description", type=str,
                        help='Board description, e.g. "ARIA ESP32 board, 80x60mm, 12V, UART, BLE"')
    parser.add_argument("--out", type=Path, default=None,
                        help="Output parent directory (default: outputs/ecad/)")
    args = parser.parse_args()

    generate_ecad(args.description, out_dir=args.out)
