"""Tests for TeachingEngine interactive_pause (Enhancement D)."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import pytest

from aria_os.teaching.engine import TeachingEngine, DifficultyLevel


# ---------------------------------------------------------------------------
# interactive flag default
# ---------------------------------------------------------------------------

class TestInteractiveFlagDefault:
    def test_interactive_defaults_to_false(self):
        engine = TeachingEngine()
        assert engine.interactive is False

    def test_interactive_can_be_set_true(self):
        engine = TeachingEngine(interactive=True)
        assert engine.interactive is True

    def test_interactive_does_not_affect_difficulty(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.BEGINNER, interactive=True)
        assert engine.difficulty == DifficultyLevel.BEGINNER
        assert engine.interactive is True


# ---------------------------------------------------------------------------
# interactive_pause no-op cases
# ---------------------------------------------------------------------------

class TestInteractivePauseNoop:
    def test_noop_when_interactive_false(self):
        """interactive_pause must be a no-op when interactive=False."""
        engine = TeachingEngine(interactive=False)
        # stdin would be a tty in some test environments, so we patch it to be
        # certain — the check should short-circuit before reaching isatty().
        with patch("builtins.input") as mock_input:
            engine.interactive_pause("spec", "summary")
        mock_input.assert_not_called()

    def test_noop_when_stdin_not_a_tty(self):
        """interactive_pause must be a no-op when stdin is not a tty (e.g. CI/pipe)."""
        engine = TeachingEngine(interactive=True)
        with patch.object(sys.stdin, "isatty", return_value=False):
            with patch("builtins.input") as mock_input:
                engine.interactive_pause("spec", "summary")
        mock_input.assert_not_called()

    def test_noop_when_interactive_false_and_not_a_tty(self):
        """Belt-and-suspenders: both conditions False."""
        engine = TeachingEngine(interactive=False)
        with patch.object(sys.stdin, "isatty", return_value=False):
            with patch("builtins.input") as mock_input:
                engine.interactive_pause("spec", "summary")
        mock_input.assert_not_called()


# ---------------------------------------------------------------------------
# interactive_pause active cases
# ---------------------------------------------------------------------------

class TestInteractivePauseActive:
    def _make_active_engine(self) -> TeachingEngine:
        """Return an engine in interactive mode with stdin mocked as a tty."""
        engine = TeachingEngine(interactive=True)
        return engine

    def test_enter_with_no_input_exits_loop(self):
        """Pressing Enter (empty string) should exit the Q&A loop."""
        engine = self._make_active_engine()
        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", return_value=""):
                # Should complete without calling ask()
                engine.interactive_pause("spec", "Spec done")

    def test_question_triggers_ask(self):
        """Typing a question should call engine.ask() and then loop back."""
        engine = self._make_active_engine()
        engine.ask = MagicMock(return_value="Because of material constraints.")

        # First call: question; second call: empty string (Enter to continue)
        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", side_effect=["Why aluminium?", ""]):
                engine.interactive_pause("spec", "Spec done")

        engine.ask.assert_called_once_with("Why aluminium?")

    def test_multiple_questions_each_triggers_ask(self):
        """Multiple questions before Enter should all call ask()."""
        engine = self._make_active_engine()
        engine.ask = MagicMock(return_value="Answer.")

        questions = ["Why 6 bolts?", "What is PCD?", ""]
        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", side_effect=questions):
                engine.interactive_pause("design", "Design done")

        assert engine.ask.call_count == 2
        engine.ask.assert_any_call("Why 6 bolts?")
        engine.ask.assert_any_call("What is PCD?")

    def test_ctrl_c_exits_gracefully(self):
        """KeyboardInterrupt should be caught and not propagate."""
        engine = self._make_active_engine()
        engine.ask = MagicMock()

        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", side_effect=KeyboardInterrupt):
                # Should not raise
                engine.interactive_pause("dfm", "DFM done")

        engine.ask.assert_not_called()

    def test_eof_exits_gracefully(self):
        """EOFError (piped input exhausted) should exit the loop cleanly."""
        engine = self._make_active_engine()
        engine.ask = MagicMock()

        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", side_effect=EOFError):
                engine.interactive_pause("quote", "Quote done")

        engine.ask.assert_not_called()

    def test_recent_teachings_printed_for_phase(self, capsys):
        """Teachings for the matching phase should appear in the interactive_pause output.
        Only teachings matching the phase are listed in the 'Recent insights' block.
        """
        engine = self._make_active_engine()
        engine.teach_simple("Spec", "spec", "Extracted od_mm=80")
        engine.teach_simple("Design", "design", "Used flange template")

        # Discard the proactive print() output from teach_simple calls above
        capsys.readouterr()

        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", return_value=""):
                engine.interactive_pause("spec", "Spec complete")

        captured = capsys.readouterr()
        # The spec teaching should appear in the "Recent insights" block
        assert "od_mm=80" in captured.out
        # The design teaching should NOT be listed under spec-phase insights
        assert "flange template" not in captured.out

    def test_summary_is_printed(self, capsys):
        """The summary string should appear in the printed output."""
        engine = self._make_active_engine()
        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", return_value=""):
                engine.interactive_pause("dfm", "DFM score 85/100, process: CNC milling")

        captured = capsys.readouterr()
        assert "DFM score 85/100" in captured.out

    def test_ask_answer_printed(self, capsys):
        """The answer from ask() should be printed to stdout."""
        engine = self._make_active_engine()
        engine.ask = MagicMock(return_value="PCD stands for Pitch Circle Diameter.")

        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", side_effect=["What is PCD?", ""]):
                engine.interactive_pause("spec", "Spec done")

        captured = capsys.readouterr()
        assert "PCD stands for Pitch Circle Diameter." in captured.out


# ---------------------------------------------------------------------------
# Integration: DesignState.teaching_engine.interactive flag
# ---------------------------------------------------------------------------

class TestDesignStateInteractiveIntegration:
    def test_design_state_engine_interactive_flag_propagates(self):
        """When TeachingEngine is created with interactive=True and set on DesignState,
        the flag is accessible via state.teaching_engine.interactive."""
        from aria_os.agents.design_state import DesignState
        engine = TeachingEngine(interactive=True)
        state = DesignState(goal="test flange", teaching_engine=engine)
        assert state.teaching_engine.interactive is True

    def test_design_state_engine_non_interactive_default(self):
        """Default TeachingEngine attached to DesignState should be non-interactive."""
        from aria_os.agents.design_state import DesignState
        engine = TeachingEngine()
        state = DesignState(goal="test bracket", teaching_engine=engine)
        assert state.teaching_engine.interactive is False
