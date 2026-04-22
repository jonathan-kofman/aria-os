"""Adversarial tests for aria_os/voice_commands.py — command parsing +
design-intent annotation sidecar."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.voice_commands import interpret_command, annotate_part  # noqa: E402


class TestInterpretCommand:
    def test_empty_is_noop(self):
        r = interpret_command("")
        assert r["action"] == "noop"

    def test_whitespace_is_noop(self):
        r = interpret_command("   \t\n  ")
        assert r["action"] == "noop"

    def test_regenerate_with_fillet_radius(self):
        r = interpret_command("regenerate the fillet with a 3mm radius")
        assert r["action"] == "regenerate"
        assert r["feature"] == "fillet"
        assert r["radius_mm"] == 3.0

    def test_run_dfm_catches_compound_phrases(self):
        for phrase in (
            "run the DFM agent",
            "run DFM",
            "rerun the DFM",
            "RUN DFM AGENT ON THIS PART",
        ):
            r = interpret_command(phrase)
            assert r["action"] == "run_dfm", phrase

    def test_export_formats(self):
        assert interpret_command("export STEP now")["action"] == "export_step"
        assert interpret_command("export to STL")["action"] == "export_stl"
        assert interpret_command("export DXF for the drawing")["action"] == "export_dxf"

    def test_run_fea_cfd_quote_cam(self):
        assert interpret_command("run FEA")["action"] == "run_fea"
        assert interpret_command("run CFD")["action"] == "run_cfd"
        assert interpret_command("run the quote")["action"] == "run_quote"
        assert interpret_command("run CAM")["action"] == "run_cam"

    def test_generate_drawing_synonyms(self):
        for phrase in ("generate a drawing", "make a drawing",
                       "create the drawing", "build a drawing"):
            assert interpret_command(phrase)["action"] == "generate_drawing", phrase

    def test_modify_with_dim(self):
        r = interpret_command("modify the bore to 12mm")
        assert r["action"] == "modify"
        assert r["feature"] == "bore"
        assert r["dimension_mm"] == 12.0

    def test_cancel_synonyms(self):
        for phrase in ("cancel", "stop everything", "abort"):
            assert interpret_command(phrase)["action"] == "cancel", phrase

    def test_view_open_show(self):
        for phrase in ("show the part", "open the file", "view the STEP"):
            assert interpret_command(phrase)["action"] == "view", phrase

    def test_unknown_pure_note(self):
        r = interpret_command("this face mates to the fixture, keep it flat")
        assert r["action"] == "unknown"
        assert "raw" in r

    def test_unicode_transcript(self):
        r = interpret_command("regenerate the fillet with a 3mm radius — urgent")
        assert r["action"] == "regenerate"

    def test_injection_like(self):
        # Hostile transcript — must not crash
        r = interpret_command("'; DROP TABLE parts; -- regenerate fillet 3mm")
        assert r["action"] == "regenerate"   # keyword still matches

    def test_raw_is_preserved(self):
        r = interpret_command("REGENERATE THE FILLET WITH A 3MM RADIUS")
        assert r["raw"] == "REGENERATE THE FILLET WITH A 3MM RADIUS"


class TestAnnotatePart:
    def test_creates_sidecar(self, tmp_path):
        part = tmp_path / "bracket.step"
        part.write_text("", encoding="utf-8")
        data = annotate_part(part, "test note", feature_ref="face_top")
        sidecar = tmp_path / "bracket.step.intent.json"
        assert sidecar.exists()
        assert data["notes"][-1]["text"] == "test note"
        assert data["notes"][-1]["feature_ref"] == "face_top"

    def test_appends_on_existing(self, tmp_path):
        part = tmp_path / "bracket.step"
        annotate_part(part, "note 1")
        annotate_part(part, "note 2")
        annotate_part(part, "note 3")
        sidecar = tmp_path / "bracket.step.intent.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert len(data["notes"]) == 3
        assert data["notes"][0]["text"] == "note 1"
        assert data["notes"][-1]["text"] == "note 3"

    def test_nonexistent_part_still_writes(self, tmp_path):
        # Sidecar is placed next to the expected path even if the part
        # hasn't been generated yet — design-intent-first workflows.
        part = tmp_path / "ghost.step"
        data = annotate_part(part, "design intent: mates to fixture")
        sidecar = tmp_path / "ghost.step.intent.json"
        assert sidecar.exists()

    def test_malformed_existing_sidecar_recovers(self, tmp_path):
        part = tmp_path / "bracket.step"
        sidecar = tmp_path / "bracket.step.intent.json"
        sidecar.write_text("not valid json {{", encoding="utf-8")
        # Must not raise — falls back to fresh structure
        data = annotate_part(part, "new note")
        assert data["notes"][-1]["text"] == "new note"

    def test_text_is_stripped(self, tmp_path):
        part = tmp_path / "x.step"
        data = annotate_part(part, "   padded   ")
        assert data["notes"][-1]["text"] == "padded"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
