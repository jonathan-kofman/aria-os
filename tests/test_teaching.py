"""Tests for the ARIA-OS teaching layer."""
import pytest

from aria_os.teaching.engine import TeachingEngine, Teaching, DifficultyLevel
from aria_os.teaching.mixin import TeachingMixin


# ---------------------------------------------------------------------------
# TeachingEngine basics
# ---------------------------------------------------------------------------

class TestTeachingEngine:
    def test_init_defaults(self):
        engine = TeachingEngine()
        assert engine.difficulty == DifficultyLevel.INTERMEDIATE
        assert engine.teachings == []
        assert engine.context == {}
        assert engine.conversation == []

    def test_teach_records_teaching(self):
        engine = TeachingEngine()
        engine.teach_simple("SpecAgent", "spec", "Extracted od_mm=80")
        assert len(engine.teachings) == 1
        assert engine.teachings[0].agent == "SpecAgent"
        assert engine.teachings[0].phase == "spec"
        assert engine.teachings[0].message == "Extracted od_mm=80"

    def test_teach_filters_by_difficulty(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.BEGINNER)
        # Beginner engine should accept beginner teachings
        engine.teach_simple("Test", "spec", "beginner msg", level=DifficultyLevel.BEGINNER)
        assert len(engine.teachings) == 1
        # But should filter out expert teachings
        engine.teach_simple("Test", "spec", "expert msg", level=DifficultyLevel.EXPERT)
        assert len(engine.teachings) == 1  # still 1

    def test_intermediate_sees_beginner_and_intermediate(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.INTERMEDIATE)
        engine.teach_simple("T", "s", "beginner", level=DifficultyLevel.BEGINNER)
        engine.teach_simple("T", "s", "intermediate", level=DifficultyLevel.INTERMEDIATE)
        engine.teach_simple("T", "s", "expert", level=DifficultyLevel.EXPERT)
        assert len(engine.teachings) == 2

    def test_expert_sees_all(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.EXPERT)
        engine.teach_simple("T", "s", "beginner", level=DifficultyLevel.BEGINNER)
        engine.teach_simple("T", "s", "intermediate", level=DifficultyLevel.INTERMEDIATE)
        engine.teach_simple("T", "s", "expert", level=DifficultyLevel.EXPERT)
        assert len(engine.teachings) == 3

    def test_update_context(self):
        engine = TeachingEngine()
        engine.update_context("goal", "flange 80mm OD")
        engine.update_context("material", "aluminium_6061")
        assert engine.context["goal"] == "flange 80mm OD"
        assert engine.context["material"] == "aluminium_6061"

    def test_update_context_dict(self):
        engine = TeachingEngine()
        engine.update_context_dict({"goal": "bracket", "spec": {"width_mm": 50}})
        assert engine.context["goal"] == "bracket"
        assert engine.context["spec"]["width_mm"] == 50

    def test_get_teachings_no_filter(self):
        engine = TeachingEngine()
        engine.teach_simple("A", "spec", "msg1")
        engine.teach_simple("B", "design", "msg2")
        result = engine.get_teachings()
        assert len(result) == 2
        assert result[0]["agent"] == "A"

    def test_get_teachings_filter_by_phase(self):
        engine = TeachingEngine()
        engine.teach_simple("A", "spec", "spec msg")
        engine.teach_simple("B", "design", "design msg")
        engine.teach_simple("C", "spec", "another spec")
        result = engine.get_teachings(phase="spec")
        assert len(result) == 2
        assert all(t["phase"] == "spec" for t in result)

    def test_get_teachings_limit(self):
        engine = TeachingEngine()
        for i in range(10):
            engine.teach_simple("A", "spec", f"msg {i}")
        result = engine.get_teachings(limit=3)
        assert len(result) == 3
        # Should return the last 3
        assert result[0]["message"] == "msg 7"

    def test_session_summary(self):
        engine = TeachingEngine()
        engine.teach_simple("Spec", "spec", "extracted dims")
        engine.teach_simple("Design", "design", "used template")
        engine.teach_simple("Eval", "eval", "all checks passed")
        summary = engine.get_session_summary()
        assert summary["total_teachings"] == 3
        assert summary["by_phase"]["spec"] == 1
        assert summary["by_phase"]["design"] == 1
        assert summary["by_phase"]["eval"] == 1

    def test_disabled_engine(self):
        engine = TeachingEngine()
        engine._enabled = False
        engine.teach_simple("A", "spec", "should not record")
        assert len(engine.teachings) == 0


# ---------------------------------------------------------------------------
# Teaching dataclass
# ---------------------------------------------------------------------------

class TestTeaching:
    def test_to_dict(self):
        t = Teaching(
            agent="SpecAgent",
            phase="spec",
            message="Extracted od_mm=80",
            reasoning="User specified 80mm outer diameter",
            level=DifficultyLevel.INTERMEDIATE,
            related_param="od_mm",
            tags=["geometry"],
        )
        d = t.to_dict()
        assert d["agent"] == "SpecAgent"
        assert d["phase"] == "spec"
        assert d["level"] == "intermediate"
        assert d["related_param"] == "od_mm"
        assert "geometry" in d["tags"]
        assert "timestamp" in d

    def test_default_values(self):
        t = Teaching(agent="A", phase="p", message="m")
        assert t.level == DifficultyLevel.INTERMEDIATE
        assert t.reasoning == ""
        assert t.related_param == ""
        assert t.tags == []


# ---------------------------------------------------------------------------
# TeachingMixin
# ---------------------------------------------------------------------------

class TestTeachingMixin:
    def test_explain_without_engine_is_noop(self):
        """explain() should silently do nothing if no engine is attached."""
        mixin = TeachingMixin()
        mixin.explain("spec", "some message")  # should not raise

    def test_explain_with_engine(self):
        engine = TeachingEngine()
        mixin = TeachingMixin()
        mixin.name = "TestAgent"
        mixin.set_teaching_engine(engine)
        mixin.explain("spec", "Extracted dimensions", tags=["geometry"])
        assert len(engine.teachings) == 1
        assert engine.teachings[0].agent == "TestAgent"

    def test_explain_decision(self):
        engine = TeachingEngine()
        mixin = TeachingMixin()
        mixin.name = "Designer"
        mixin.set_teaching_engine(engine)
        mixin.explain_decision(
            "design", "Using flange template", "Best match for part type"
        )
        assert len(engine.teachings) == 1
        assert engine.teachings[0].reasoning == "Best match for part type"

    def test_explain_beginner(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.EXPERT)
        mixin = TeachingMixin()
        mixin.name = "Test"
        mixin.set_teaching_engine(engine)
        mixin.explain_beginner("spec", "A flange is a flat disc with bolt holes")
        assert engine.teachings[0].level == DifficultyLevel.BEGINNER

    def test_explain_expert(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.EXPERT)
        mixin = TeachingMixin()
        mixin.name = "Test"
        mixin.set_teaching_engine(engine)
        mixin.explain_expert("eval", "Kt=2.3 per Peterson's stress concentration tables")
        assert engine.teachings[0].level == DifficultyLevel.EXPERT


# ---------------------------------------------------------------------------
# BaseAgent integration
# ---------------------------------------------------------------------------

class TestBaseAgentTeaching:
    def test_base_agent_has_teaching_mixin(self):
        """BaseAgent should inherit TeachingMixin methods."""
        from aria_os.agents.base_agent import BaseAgent
        agent = BaseAgent(name="TestAgent", system_prompt="test")
        assert hasattr(agent, "explain")
        assert hasattr(agent, "set_teaching_engine")
        assert hasattr(agent, "explain_decision")

    def test_base_agent_explain_without_engine(self):
        """explain() should be a safe no-op without an engine."""
        from aria_os.agents.base_agent import BaseAgent
        agent = BaseAgent(name="TestAgent", system_prompt="test")
        agent.explain("spec", "test message")  # should not raise

    def test_base_agent_explain_with_engine(self):
        """explain() should emit to the attached engine."""
        from aria_os.agents.base_agent import BaseAgent
        engine = TeachingEngine()
        agent = BaseAgent(name="TestAgent", system_prompt="test")
        agent.set_teaching_engine(engine)
        agent.explain("spec", "Extracted params", tags=["geometry"])
        assert len(engine.teachings) == 1
        assert engine.teachings[0].agent == "TestAgent"


# ---------------------------------------------------------------------------
# DesignState integration
# ---------------------------------------------------------------------------

class TestDesignStateTeaching:
    def test_design_state_accepts_teaching_engine(self):
        from aria_os.agents.design_state import DesignState
        engine = TeachingEngine()
        state = DesignState(goal="test", teaching_engine=engine)
        assert state.teaching_engine is engine

    def test_design_state_default_no_engine(self):
        from aria_os.agents.design_state import DesignState
        state = DesignState(goal="test")
        assert state.teaching_engine is None


# ---------------------------------------------------------------------------
# Q&A context building
# ---------------------------------------------------------------------------

class TestQAContext:
    def test_build_qa_system_prompt_beginner(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.BEGINNER)
        prompt = engine._build_qa_system_prompt()
        assert "beginner" in prompt.lower()
        assert "simple analogies" in prompt.lower()

    def test_build_qa_system_prompt_expert(self):
        engine = TeachingEngine(difficulty=DifficultyLevel.EXPERT)
        prompt = engine._build_qa_system_prompt()
        assert "experienced" in prompt.lower()
        assert "ASME" in prompt

    def test_build_qa_user_prompt_includes_context(self):
        engine = TeachingEngine()
        engine.update_context("goal", "flange 80mm OD 6 bolts")
        engine.update_context("material", "aluminium_6061")
        prompt = engine._build_qa_user_prompt("Why 6 bolts?")
        assert "flange 80mm OD 6 bolts" in prompt
        assert "aluminium_6061" in prompt
        assert "Why 6 bolts?" in prompt

    def test_build_qa_user_prompt_includes_teachings(self):
        engine = TeachingEngine()
        engine.teach_simple("Spec", "spec", "Extracted od_mm=80")
        engine.teach_simple("Design", "design", "Using flange template")
        prompt = engine._build_qa_user_prompt("What template?")
        assert "Extracted od_mm=80" in prompt
        assert "Using flange template" in prompt

    def test_format_context_summary_empty(self):
        engine = TeachingEngine()
        summary = engine._format_context_summary()
        assert "No pipeline context" in summary

    def test_format_context_summary_with_data(self):
        engine = TeachingEngine()
        engine.update_context("goal", "bracket 80x60x5mm")
        engine.update_context("material", "steel")
        summary = engine._format_context_summary()
        assert "bracket 80x60x5mm" in summary
        assert "steel" in summary
