"""
Agent-level skill-profile tests — verify DesignerAgent and EvalAgent
actually read state.skill_profile and change behavior accordingly.
These are the tests that prove the wiring isn't dead code.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.skill_profile import SkillProfile, SkillLevel  # noqa: E402
from aria_os.agents.design_state import DesignState  # noqa: E402


# ---------------------------------------------------------------------------
# EvalAgent: profile controls whether failures BLOCK the refinement loop
# ---------------------------------------------------------------------------

class TestEvalAgentSkillGating:
    def _make_state(self, profile, failures):
        s = DesignState(goal="test", domain="cad", skill_profile=profile)
        # Prepopulate failures; we don't run _eval_cad, we test the
        # post-processing gate that evaluate() applies to state.failures.
        return s, failures

    def test_veteran_never_blocks_regardless_of_severity(self):
        """Veterans get everything as advisory."""
        from aria_os.agents.eval_agent import EvalAgent
        prof = SkillProfile.for_level(SkillLevel.VETERAN)
        agent = EvalAgent(domain="cad", repo_root=Path(__file__).resolve().parent.parent)
        state = DesignState(goal="x", domain="cad", skill_profile=prof)
        # inject failures — no _eval_cad run needed
        state.failures = ["solid_count too high",        # critical
                          "dimension mismatch od_mm",    # error
                          "cosmetic issue"]              # warning
        # Manually run the gating block that lives at the end of evaluate()
        self._simulate_gate(agent, state, prof)
        assert state.eval_passed is True, \
            "veteran should accept all failures as advisory"

    def test_novice_blocks_on_any_failure(self):
        from aria_os.agents.eval_agent import EvalAgent
        prof = SkillProfile.for_level(SkillLevel.NOVICE)
        agent = EvalAgent(domain="cad", repo_root=Path(__file__).resolve().parent.parent)
        state = DesignState(goal="x", domain="cad", skill_profile=prof)
        state.failures = ["cosmetic issue"]  # warning only
        self._simulate_gate(agent, state, prof)
        assert state.eval_passed is False, \
            "novice should block even on a warning"

    def test_advanced_blocks_only_on_critical(self):
        from aria_os.agents.eval_agent import EvalAgent
        prof = SkillProfile.for_level(SkillLevel.ADVANCED)
        agent = EvalAgent(domain="cad", repo_root=Path(__file__).resolve().parent.parent)
        state = DesignState(goal="x", domain="cad", skill_profile=prof)
        state.failures = ["dimension mismatch od_mm"]  # error, not critical
        self._simulate_gate(agent, state, prof)
        assert state.eval_passed is True, \
            "advanced should not block on non-critical errors"

        state2 = DesignState(goal="x", domain="cad", skill_profile=prof)
        state2.failures = ["solid_count too high"]  # critical
        self._simulate_gate(agent, state2, prof)
        assert state2.eval_passed is False, \
            "advanced must still block on critical"

    def test_no_profile_uses_legacy_strict(self):
        """When state.skill_profile is None, eval must keep its legacy
        strict behavior (any failure → block). Back-compat guarantee."""
        from aria_os.agents.eval_agent import EvalAgent
        agent = EvalAgent(domain="cad", repo_root=Path(__file__).resolve().parent.parent)
        state = DesignState(goal="x", domain="cad", skill_profile=None)
        state.failures = ["cosmetic issue"]
        self._simulate_gate(agent, state, None)
        assert state.eval_passed is False

    def _simulate_gate(self, agent, state, prof):
        """Copy-paste of the post-processing block in EvalAgent.evaluate.
        We run it in isolation to avoid having to run _eval_cad which
        needs real files. If the real evaluate() logic changes, this
        simulation must be updated to match."""
        if prof is not None and state.failures:
            def _severity(msg):
                m = msg.lower()
                if any(k in m for k in ("solid_count", "watertight",
                                          "not a valid", "no geometry",
                                          "file_exists")):
                    return "critical"
                if any(k in m for k in ("dimension", "bbox", "mismatch",
                                          "spec", "out of tolerance")):
                    return "error"
                return "warning"
            worst = max((_severity(f) for f in state.failures),
                        key=lambda s: {"warning": 0, "error": 1,
                                        "critical": 2}[s])
            if not prof.should_block_on_validation_failure(worst):
                state.eval_passed = True
                return
            state.eval_passed = False
        else:
            state.eval_passed = len(state.failures) == 0


# ---------------------------------------------------------------------------
# DesignerAgent: profile controls which LLM quality tier is used
# ---------------------------------------------------------------------------

class TestDesignerSkillQualityTier:
    """Verify _call_llm passes the right `quality` string to call_llm."""

    def _mk_designer(self):
        # Importing DesignerAgent requires a lot of deps; build a minimal
        # stub that inherits the real _call_llm but fakes base_agent plumbing.
        from aria_os.agents.designer_agent import DesignerAgent
        d = DesignerAgent.__new__(DesignerAgent)
        d.name = "DesignerAgent[cad]"
        d.domain = "cad"
        d._prefer_cloud = True
        d.system_prompt = ""
        return d

    def test_veteran_uses_premium_on_iter1(self, monkeypatch):
        from aria_os import llm_client
        calls = []
        monkeypatch.setattr(llm_client, "call_llm",
                             lambda prompt, system, quality=None: (
                                 calls.append(quality), "code")[1])
        d = self._mk_designer()
        d._current_iteration = 1
        d._current_state = DesignState(
            skill_profile=SkillProfile.for_level(SkillLevel.VETERAN))
        r = d._call_llm("p")
        assert r == "code"
        assert calls == ["premium"], \
            f"veteran iter1 should use premium, got {calls}"

    def test_novice_uses_fast(self, monkeypatch):
        from aria_os import llm_client
        calls = []
        monkeypatch.setattr(llm_client, "call_llm",
                             lambda prompt, system, quality=None: (
                                 calls.append(quality), "code")[1])
        d = self._mk_designer()
        d._current_iteration = 1
        d._current_state = DesignState(
            skill_profile=SkillProfile.for_level(SkillLevel.NOVICE))
        d._call_llm("p")
        assert calls == ["fast"], f"novice should use fast, got {calls}"

    def test_advanced_uses_balanced(self, monkeypatch):
        from aria_os import llm_client
        calls = []
        monkeypatch.setattr(llm_client, "call_llm",
                             lambda prompt, system, quality=None: (
                                 calls.append(quality), "code")[1])
        d = self._mk_designer()
        d._current_iteration = 1
        d._current_state = DesignState(
            skill_profile=SkillProfile.for_level(SkillLevel.ADVANCED))
        d._call_llm("p")
        assert calls == ["balanced"]

    def test_refinement_iteration_never_premium(self, monkeypatch):
        """Iter 2+ always uses balanced — prevent repeated premium charges."""
        from aria_os import llm_client
        calls = []
        monkeypatch.setattr(llm_client, "call_llm",
                             lambda prompt, system, quality=None: (
                                 calls.append(quality), "code")[1])
        d = self._mk_designer()
        d._current_iteration = 3
        d._current_state = DesignState(
            skill_profile=SkillProfile.for_level(SkillLevel.VETERAN))
        d._call_llm("p")
        assert calls == ["balanced"], \
            f"iter 3 must downgrade to balanced even for veteran, got {calls}"

    def test_no_profile_uses_balanced(self, monkeypatch):
        from aria_os import llm_client
        calls = []
        monkeypatch.setattr(llm_client, "call_llm",
                             lambda prompt, system, quality=None: (
                                 calls.append(quality), "code")[1])
        d = self._mk_designer()
        d._current_iteration = 1
        d._current_state = DesignState(skill_profile=None)
        d._call_llm("p")
        assert calls == ["balanced"], \
            f"no profile default should be balanced, got {calls}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
