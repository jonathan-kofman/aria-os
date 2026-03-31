"""
tests/test_output_contracts.py

Validates that JSON outputs produced by cam_setup and ecad_generator conform
to their JSON Schema contracts in contracts/.

Each test:
  1. Generates a minimal sample output using the module's own public API
     (no real STEP/CAM files required — the modules handle missing files
     gracefully and return fallback data).
  2. Validates the output against the matching contract schema.

Requires: jsonschema >= 4.0
    pip install jsonschema
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"

# ── jsonschema availability ────────────────────────────────────────────────────

try:
    import jsonschema
    from jsonschema import validate, ValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

pytestmark = pytest.mark.skipif(
    not HAS_JSONSCHEMA,
    reason="jsonschema not installed — run: pip install jsonschema",
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_schema(filename: str) -> dict:
    path = CONTRACTS / filename
    assert path.exists(), f"Contract file not found: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_valid(instance: dict, schema: dict) -> None:
    try:
        validate(instance=instance, schema=schema)
    except ValidationError as exc:
        pytest.fail(f"JSON Schema validation failed:\n{exc.message}\nPath: {list(exc.absolute_path)}")


# ─────────────────────────────────────────────────────────────────────────────
# cam_setup contract tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCamSetupSchema:

    def _minimal_setup_sheet(self) -> dict:
        """Minimal valid setup_sheet document matching cam_setup_schema_v1."""
        from aria_os.cam_setup import CAM_SETUP_SCHEMA_VERSION
        return {
            "schema_version": CAM_SETUP_SCHEMA_VERSION,
            "part_id": "aria_housing",
            "machine_name": "Tormach 1100",
            "tools": [
                {"name": "6mm flat endmill", "dia_mm": 6.0, "flutes": 3}
            ],
            "stock_dims": {"x_mm": 86.0, "y_mm": 56.0, "z_mm": 24.0},
            "cycle_time_min_estimate": 12.5,
            "second_op_required": False,
            "work_offset_recommendation": "G54: bottom-left-top corner of stock",
            "fixturing_suggestion": "6-inch vise",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def test_schema_version_constant(self):
        from aria_os.cam_setup import CAM_SETUP_SCHEMA_VERSION
        assert CAM_SETUP_SCHEMA_VERSION == "1.0"

    def test_minimal_document_passes(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        _assert_valid(self._minimal_setup_sheet(), schema)

    def test_full_tools_list_passes(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        doc = self._minimal_setup_sheet()
        doc["tools"] = [
            {
                "name": "12mm flat endmill",
                "dia_mm": 12.0,
                "flutes": 4,
                "type": "flat_endmill",
                "material": "carbide",
                "max_doc_mm": 18.0,
                "sfm": 300,
            },
            {
                "name": "6mm ball endmill",
                "dia_mm": 6.0,
                "flutes": 2,
                "type": "ball_endmill",
                "material": "carbide",
            },
            {
                "name": "5mm drill",
                "dia_mm": 5.0,
                "flutes": 2,
                "type": "drill",
            },
        ]
        _assert_valid(doc, schema)

    def test_second_op_true_passes(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        doc = self._minimal_setup_sheet()
        doc["second_op_required"] = True
        _assert_valid(doc, schema)

    def test_missing_schema_version_fails(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        doc = self._minimal_setup_sheet()
        del doc["schema_version"]
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_wrong_schema_version_fails(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        doc = self._minimal_setup_sheet()
        doc["schema_version"] = "2.0"
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_negative_cycle_time_fails(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        doc = self._minimal_setup_sheet()
        doc["cycle_time_min_estimate"] = -1.0
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_zero_stock_dim_fails(self):
        schema = _load_schema("cam_setup_schema_v1.json")
        doc = self._minimal_setup_sheet()
        doc["stock_dims"]["x_mm"] = 0
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_generate_setup_sheet_produces_valid_json(self, tmp_path):
        """Integration: write_setup_sheet writes a schema-valid setup_sheet.json."""
        from aria_os.cam_setup import write_setup_sheet, CAM_SETUP_SCHEMA_VERSION

        # write_setup_sheet requires a CAM script file; create a minimal stub
        cam_script = tmp_path / "aria_housing_cam.py"
        cam_script.write_text(
            "# ARIA CAM\n"
            "# Material: aluminium_6061\n"
            "# Tool: 6mm flat endmill, dia=6mm, flutes=3\n"
            "# Operation: 3D Adaptive Clearing\n",
            encoding="utf-8",
        )

        out_dir = tmp_path / "cam_out"
        out_dir.mkdir()

        write_setup_sheet(
            step_path=str(tmp_path / "aria_housing.step"),  # does not exist — fallback expected
            cam_script_path=str(cam_script),
            material="aluminium_6061",
            out_dir=out_dir,
            part_id="aria_housing",
            machine_name="Tormach 1100",
        )

        json_path = out_dir / "setup_sheet.json"
        assert json_path.exists(), "write_setup_sheet did not produce setup_sheet.json"

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data.get("schema_version") == CAM_SETUP_SCHEMA_VERSION

        schema = _load_schema("cam_setup_schema_v1.json")
        _assert_valid(data, schema)


# ─────────────────────────────────────────────────────────────────────────────
# ecad_bom contract tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEcadBomSchema:

    def _minimal_bom(self) -> dict:
        """Minimal valid BOM document matching ecad_bom_schema_v1."""
        from aria_os.ecad_generator import ECAD_BOM_SCHEMA_VERSION
        return {
            "schema_version": ECAD_BOM_SCHEMA_VERSION,
            "components": [
                {
                    "ref": "U1",
                    "value": "ESP32-WROOM-32",
                    "footprint": "RF_Module:ESP32-WROOM-32",
                    "description": "ESP32 WiFi+BLE module",
                    "qty": 1,
                    "x_mm": 40.0,
                    "y_mm": 30.0,
                }
            ],
        }

    def test_schema_version_constant(self):
        from aria_os.ecad_generator import ECAD_BOM_SCHEMA_VERSION
        assert ECAD_BOM_SCHEMA_VERSION == "1.0"

    def test_minimal_bom_passes(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        _assert_valid(self._minimal_bom(), schema)

    def test_with_firmware_pins_passes(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        doc["firmware_pins"] = {"PIN_BRAKE": "PB13", "PIN_LOAD_CELL_DAT": "34"}
        _assert_valid(doc, schema)

    def test_with_validation_block_passes(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        doc["validation"] = {
            "passed": True,
            "erc_pass": True,
            "drc_pass": True,
            "erc": {"errors": [], "warnings": []},
        }
        _assert_valid(doc, schema)

    def test_empty_components_passes(self):
        """Empty component list is allowed — unusual but not a schema violation."""
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        doc["components"] = []
        _assert_valid(doc, schema)

    def test_missing_schema_version_fails(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        del doc["schema_version"]
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_wrong_schema_version_fails(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        doc["schema_version"] = "99.0"
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_component_missing_ref_fails(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        del doc["components"][0]["ref"]
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_component_qty_zero_fails(self):
        schema = _load_schema("ecad_bom_schema_v1.json")
        doc = self._minimal_bom()
        doc["components"][0]["qty"] = 0
        with pytest.raises(ValidationError):
            validate(instance=doc, schema=schema)

    def test_build_bom_produces_valid_output(self):
        """Integration: build_bom() returns a schema-valid dict."""
        from aria_os.ecad_generator import build_bom, parse_components, ECAD_BOM_SCHEMA_VERSION
        from aria_os.ecad_generator import place_components

        components = parse_components("ARIA ESP32 board, 80x60mm, 12V, UART, BLE")
        place_components(components, 80.0, 60.0)
        bom = build_bom(components)

        assert bom.get("schema_version") == ECAD_BOM_SCHEMA_VERSION

        schema = _load_schema("ecad_bom_schema_v1.json")
        _assert_valid(bom, schema)

    def test_generate_ecad_bom_file_valid(self, tmp_path):
        """Integration: generate_ecad() writes a schema-valid BOM JSON file."""
        from aria_os.ecad_generator import generate_ecad, ECAD_BOM_SCHEMA_VERSION

        _script_path, bom_path = generate_ecad(
            "test board, 80x60mm, 12V, UART",
            out_dir=tmp_path,
        )

        assert bom_path.exists(), "generate_ecad did not produce a BOM JSON"

        data = json.loads(bom_path.read_text(encoding="utf-8"))
        assert data.get("schema_version") == ECAD_BOM_SCHEMA_VERSION

        schema = _load_schema("ecad_bom_schema_v1.json")
        _assert_valid(data, schema)
