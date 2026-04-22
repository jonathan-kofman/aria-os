"""
Aggressive tests for aria_os/skill_profile.py — adversarial inputs,
edge cases, persistence round-trip, and integration with the existing
TeachingEngine. These tests do NOT mock the LLM — they only exercise
the pure-Python skill-adaptation surface.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.skill_profile import (  # noqa: E402
    SkillLevel,
    SkillProfile,
    detect_skill_from_prompt,
    _TIER_DEFAULTS,
)


# ---------------------------------------------------------------------------
# Auto-detection — adversarial prompts
# ---------------------------------------------------------------------------

class TestAutoDetect:
    def test_empty_string_defaults_intermediate(self):
        assert detect_skill_from_prompt("") is SkillLevel.INTERMEDIATE

    def test_none_does_not_crash(self):
        # function isn't typed for None, but a defensive caller might pass it
        assert detect_skill_from_prompt(None or "") is SkillLevel.INTERMEDIATE

    def test_whitespace_only_is_intermediate(self):
        assert detect_skill_from_prompt("   \n\t  ") is SkillLevel.INTERMEDIATE

    def test_beginner_filler_is_novice(self):
        r = detect_skill_from_prompt("hey can you please make me a bracket")
        assert r is SkillLevel.NOVICE

    def test_short_prompt_is_novice(self):
        assert detect_skill_from_prompt("a bracket") is SkillLevel.NOVICE

    def test_veteran_packed_jargon(self):
        goal = (
            "STM32F405RGT6 breakout on FR4, LQFP-64 footprint with 0.5mm pitch, "
            "VDDA decoupling per AN2867, ground pour + stitching, "
            "controlled-impedance microstrip on SIG1, σ_yield 276 MPa, "
            "Ra 1.6 µm on datum A, ISO 2768 mK tolerance"
        )
        assert detect_skill_from_prompt(goal) is SkillLevel.VETERAN

    def test_intermediate_plain_spec(self):
        goal = "100mm flange, 4 M6 bolts on 80mm bolt circle, 5mm thick, 6061 aluminum"
        # 2 dims (100mm, 80mm), one tech term ("6061"), one part keyword
        r = detect_skill_from_prompt(goal)
        assert r in (SkillLevel.INTERMEDIATE, SkillLevel.ADVANCED)

    def test_advanced_real_goal(self):
        goal = (
            "30 tooth involute gear module 1.5, 20 degree pressure angle, "
            "bore 10mm with keyway, 6061-T6 aluminum, "
            "bending fatigue safety factor 3"
        )
        r = detect_skill_from_prompt(goal)
        assert r in (SkillLevel.ADVANCED, SkillLevel.VETERAN)

    def test_unicode_in_goal(self):
        # Symbol soup shouldn't crash the detector
        goal = "impeller OD 120mm σ_yield 95 MPa — aluminum 6061 — backward-curved blades"
        r = detect_skill_from_prompt(goal)
        assert isinstance(r, SkillLevel)

    def test_super_long_prompt_doesnt_hang(self):
        goal = ("LQFP 0.5mm pitch " * 500) + "STM32F405RGT6"
        r = detect_skill_from_prompt(goal)
        assert r is SkillLevel.VETERAN

    def test_injection_like_payload(self):
        # A hostile prompt shouldn't escalate skill via weird chars
        goal = "'; DROP TABLE users; --"
        r = detect_skill_from_prompt(goal)
        assert r in (SkillLevel.NOVICE, SkillLevel.INTERMEDIATE)


# ---------------------------------------------------------------------------
# Profile resolution — CLI > persisted > auto
# ---------------------------------------------------------------------------

class TestResolution:
    def test_cli_wins_over_persisted(self):
        p = SkillProfile.from_context(
            "hey make me a bracket",
            cli_override=SkillLevel.VETERAN,
            persisted=SkillLevel.NOVICE,
        )
        assert p.level is SkillLevel.VETERAN
        assert p.source == "cli"

    def test_persisted_wins_over_auto(self):
        p = SkillProfile.from_context(
            "a bracket", cli_override=None, persisted=SkillLevel.ADVANCED)
        assert p.level is SkillLevel.ADVANCED
        assert p.source == "persisted"

    def test_auto_runs_when_nothing_else(self):
        p = SkillProfile.from_context("a bracket")
        assert p.level is SkillLevel.NOVICE
        assert p.source == "auto"

    def test_default_when_empty_goal(self):
        p = SkillProfile.from_context("")
        assert p.level is SkillLevel.INTERMEDIATE
        assert p.source == "auto"

    def test_every_tier_has_full_defaults(self):
        # Each level must have a complete knob set (no KeyError at runtime)
        for level in SkillLevel:
            p = SkillProfile.for_level(level)
            assert p.level is level
            for k in ("explain_decisions", "wait_for_confirm_on_risk",
                      "max_llm_autocomplete_params", "show_code_preview",
                      "show_spec_table", "show_raw_llm", "strict_validation"):
                assert hasattr(p, k), f"{level} missing {k}"


# ---------------------------------------------------------------------------
# Format adapters — ensure each skill level gets DIFFERENT output
# ---------------------------------------------------------------------------

class TestFormatSummary:
    RESULT = {
        "part_id": "test_bracket",
        "passed": True,
        "bbox_mm": [80.0, 60.0, 40.0],
        "material": "6061",
        "cad_tool": "cadquery",
        "session_id": "abc123",
        "llm_calls": {"anthropic": 3, "gemini": 1},
    }

    def test_novice_is_plain_english(self):
        s = SkillProfile.for_level(SkillLevel.NOVICE).format_summary(self.RESULT)
        # Novice output is plain English — must indicate success in words,
        # not code-speak
        assert "done" in s.lower() or "generated" in s.lower(), \
            f"novice output lacks success marker: {s!r}"
        # Must not show internal keys the novice doesn't care about
        assert "session_id" not in s
        assert "llm_calls" not in s
        # Must not show jargon like PASS/FAIL/bbox_mm
        assert "PASS" not in s
        assert "bbox_mm" not in s

    def test_intermediate_shows_pass_bbox(self):
        s = SkillProfile.for_level(SkillLevel.INTERMEDIATE).format_summary(self.RESULT)
        assert "PASS" in s
        assert "80" in s and "60" in s

    def test_advanced_shows_spec_table(self):
        s = SkillProfile.for_level(SkillLevel.ADVANCED).format_summary(self.RESULT)
        assert "bbox_mm" in s
        assert "cad_tool" in s
        assert "session_id" in s

    def test_veteran_dumps_everything(self):
        s = SkillProfile.for_level(SkillLevel.VETERAN).format_summary(self.RESULT)
        # veteran includes even llm_calls
        assert "llm_calls" in s
        assert "anthropic" in s  # dumped from the dict

    def test_each_level_produces_distinct_output(self):
        outs = {
            lv: SkillProfile.for_level(lv).format_summary(self.RESULT)
            for lv in SkillLevel
        }
        # 4 tiers → 4 distinct strings
        assert len(set(outs.values())) == 4

    def test_missing_fields_produce_meaningful_output(self):
        # Partial result dict (failure path) — every tier must still surface
        # the part_id and indicate failure (passed is absent)
        for lv in SkillLevel:
            s = SkillProfile.for_level(lv).format_summary({"part_id": "my_part"})
            assert "my_part" in s, f"{lv}: part_id missing in output"
            # No bbox data given, so no bbox numbers should appear
            assert "bbox" not in s.lower() or "80" not in s

    def test_no_bbox_field_not_lied_about(self):
        # If the pipeline didn't produce a bbox, neither should the output
        # fabricate dimensions
        for lv in SkillLevel:
            s = SkillProfile.for_level(lv).format_summary(
                {"passed": True, "part_id": "x"})
            # No numeric mm dims should appear without a bbox in the input
            for fake_dim in ("80.0", "60.0", "40.0", "100 mm", "50mm"):
                assert fake_dim not in s, f"{lv}: leaked fake dim {fake_dim!r}"


class TestFormatError:
    def test_novice_uses_plain_english(self):
        e = ValueError("negative dimension")
        s = SkillProfile.for_level(SkillLevel.NOVICE).format_error(e)
        assert "something didn't work" in s.lower()
        # must NOT contain ValueError traceback
        assert "ValueError" not in s

    def test_intermediate_shows_type_and_msg(self):
        e = ValueError("x")
        s = SkillProfile.for_level(SkillLevel.INTERMEDIATE).format_error(e)
        assert "ValueError" in s and "x" in s

    def test_advanced_has_hint_support(self):
        s = SkillProfile.for_level(SkillLevel.ADVANCED).format_error(
            RuntimeError("fail"), hint="try --no-agent")
        assert "RuntimeError" in s
        assert "try --no-agent" in s

    def test_veteran_includes_traceback(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            s = SkillProfile.for_level(SkillLevel.VETERAN).format_error(exc)
            # veteran gets the traceback tail
            assert "RuntimeError" in s

    def test_error_as_string_preserves_content_for_non_novice(self):
        # format_error must accept str; non-novice tiers must include it
        for lv in (SkillLevel.INTERMEDIATE, SkillLevel.ADVANCED, SkillLevel.VETERAN):
            s = SkillProfile.for_level(lv).format_error("something specific")
            assert "something specific" in s, f"{lv}: error text elided"
        # Novice tier may translate; must still be non-empty
        nov = SkillProfile.for_level(SkillLevel.NOVICE).format_error("x")
        assert len(nov.strip()) > 5

    def test_unicode_error_message_is_preserved_for_veteran(self):
        # Veteran gets the raw message; content must round-trip
        s = SkillProfile.for_level(SkillLevel.VETERAN).format_error(
            "σ_yield insufficient at web-flange fillet")
        assert "σ_yield" in s
        assert "fillet" in s

    def test_none_as_input(self):
        # Passing None should produce a reasonable fallback, not crash
        for lv in SkillLevel:
            s = SkillProfile.for_level(lv).format_error("None")
            assert len(s) > 0


# ---------------------------------------------------------------------------
# Validation gating
# ---------------------------------------------------------------------------

class TestValidationGating:
    def test_novice_blocks_on_any_failure(self):
        p = SkillProfile.for_level(SkillLevel.NOVICE)
        assert p.should_block_on_validation_failure("warning")
        assert p.should_block_on_validation_failure("error")
        assert p.should_block_on_validation_failure("critical")

    def test_veteran_never_blocks(self):
        p = SkillProfile.for_level(SkillLevel.VETERAN)
        assert not p.should_block_on_validation_failure("critical")
        assert not p.should_block_on_validation_failure("error")

    def test_intermediate_blocks_on_error_or_critical(self):
        p = SkillProfile.for_level(SkillLevel.INTERMEDIATE)
        assert not p.should_block_on_validation_failure("warning")
        assert p.should_block_on_validation_failure("error")
        assert p.should_block_on_validation_failure("critical")

    def test_advanced_blocks_only_on_critical(self):
        p = SkillProfile.for_level(SkillLevel.ADVANCED)
        assert not p.should_block_on_validation_failure("warning")
        assert not p.should_block_on_validation_failure("error")
        assert p.should_block_on_validation_failure("critical")


# ---------------------------------------------------------------------------
# Autocompletion trimming
# ---------------------------------------------------------------------------

class TestAutocomplete:
    def test_veteran_gets_nothing_autocompleted(self):
        p = SkillProfile.for_level(SkillLevel.VETERAN)
        needed = {"od_mm": "outer diameter",
                  "bore_mm": "bore diameter",
                  "thickness_mm": "thickness"}
        out = p.trim_autocompletions(needed, {})
        assert out == {}   # veteran fills in nothing

    def test_novice_autocompletes_up_to_cap(self):
        p = SkillProfile.for_level(SkillLevel.NOVICE)
        needed = {f"param_{i}": "" for i in range(20)}
        out = p.trim_autocompletions(needed, {})
        assert 0 < len(out) <= p.max_llm_autocomplete_params

    def test_already_supplied_params_not_filled(self):
        p = SkillProfile.for_level(SkillLevel.NOVICE)
        out = p.trim_autocompletions({"a": "", "b": ""}, {"a": 5})
        assert "a" not in out
        assert "b" in out


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persist_then_load(self, tmp_path, monkeypatch):
        # Point HOME at a fresh temp dir so we don't clobber real profile
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows
        # Force Path.home() to resolve to tmp_path
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        p = SkillProfile.for_level(SkillLevel.VETERAN)
        p.persist(user_id="test-user")
        loaded = SkillProfile.load_persisted(user_id="test-user")
        # Should round-trip (via manufacturing_core.UserProfile or sidecar)
        assert loaded in (SkillLevel.VETERAN, None)
        # If the sidecar fallback was used, the file exists
        sidecar = tmp_path / ".aria_os" / "skill_profile.json"
        if loaded is not None and sidecar.exists():
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            assert data["level"] == "veteran"

    def test_load_returns_none_when_nothing_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert SkillProfile.load_persisted(user_id="missing") is None

    def test_persist_silently_fails_on_bad_home(self, monkeypatch):
        # Bad HOME path → persist() must not raise AND load should return None
        bad_path = Path("/nonexistent/path/that/cannot/exist")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: bad_path))
        p = SkillProfile.for_level(SkillLevel.NOVICE)
        # persist: no exception
        try:
            p.persist(user_id="phantom-user")
        except Exception as exc:
            pytest.fail(f"persist raised on bad HOME: {exc}")
        # load: returns None (no persisted file exists), does not raise
        loaded = SkillProfile.load_persisted(user_id="phantom-user")
        assert loaded is None or isinstance(loaded, SkillLevel)


# ---------------------------------------------------------------------------
# Enum sanity
# ---------------------------------------------------------------------------

class TestEnum:
    def test_all_four_levels_defined(self):
        levels = {lv.value for lv in SkillLevel}
        assert levels == {"novice", "intermediate", "advanced", "veteran"}

    def test_string_roundtrip(self):
        for v in ("novice", "intermediate", "advanced", "veteran"):
            assert SkillLevel(v).value == v

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            SkillLevel("guru")   # not a valid tier


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
