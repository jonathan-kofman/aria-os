"""Tests for UserProfile and MistakeDetector (Enhancement E and F)."""
import pytest
from pathlib import Path

from aria_os.teaching.user_profile import UserProfile, MistakeDetector


# ---------------------------------------------------------------------------
# UserProfile: basic recording
# ---------------------------------------------------------------------------

class TestUserProfileRecording:
    def test_record_teaching_increments_counter(self):
        profile = UserProfile()
        profile.record_teaching("dfm:od_mm")
        assert profile.topics_taught["dfm:od_mm"] == 1
        profile.record_teaching("dfm:od_mm")
        assert profile.topics_taught["dfm:od_mm"] == 2

    def test_record_teaching_multiple_topics(self):
        profile = UserProfile()
        profile.record_teaching("spec:n_bolts")
        profile.record_teaching("design:wall_mm")
        assert profile.topics_taught["spec:n_bolts"] == 1
        assert profile.topics_taught["design:wall_mm"] == 1

    def test_record_question_increments_counter(self):
        profile = UserProfile()
        profile.record_question("dfm")
        assert profile.topics_asked["dfm"] == 1
        profile.record_question("dfm")
        assert profile.topics_asked["dfm"] == 2

    def test_record_question_marks_struggling_at_threshold(self):
        profile = UserProfile()
        for _ in range(UserProfile._STRUGGLING_THRESHOLD - 1):
            profile.record_question("wall_thickness")
        assert "wall_thickness" not in profile.struggling_topics
        profile.record_question("wall_thickness")
        assert "wall_thickness" in profile.struggling_topics

    def test_record_mistake_increments_counter(self):
        profile = UserProfile()
        profile.record_mistake("thin_wall")
        assert profile.common_mistakes["thin_wall"] == 1
        profile.record_mistake("thin_wall")
        assert profile.common_mistakes["thin_wall"] == 2

    def test_record_session_increments_sessions_and_parts(self):
        profile = UserProfile()
        assert profile.sessions_completed == 0
        assert profile.parts_generated == 0
        profile.record_session(parts=3)
        assert profile.sessions_completed == 1
        assert profile.parts_generated == 3
        profile.record_session(parts=1)
        assert profile.sessions_completed == 2
        assert profile.parts_generated == 4


# ---------------------------------------------------------------------------
# UserProfile: should_skip_teaching
# ---------------------------------------------------------------------------

class TestShouldSkipTeaching:
    def test_new_topic_not_skipped(self):
        profile = UserProfile()
        assert profile.should_skip_teaching("bolt_patterns") is False

    def test_below_threshold_not_skipped(self):
        profile = UserProfile()
        for _ in range(UserProfile._TAUGHT_THRESHOLD - 1):
            profile.record_teaching("bolt_patterns")
        assert profile.should_skip_teaching("bolt_patterns") is False

    def test_at_threshold_with_no_questions_is_skipped(self):
        profile = UserProfile()
        topic = "dfm:od_mm"
        for _ in range(UserProfile._TAUGHT_THRESHOLD):
            profile.record_teaching(topic)
        # No questions asked, no recent session questions window
        assert profile.should_skip_teaching(topic) is True

    def test_at_threshold_but_recently_asked_not_skipped(self):
        profile = UserProfile()
        topic = "dfm:wall_mm"
        for _ in range(UserProfile._TAUGHT_THRESHOLD):
            profile.record_teaching(topic)
        # Inject topic into the recent session questions window
        profile._recent_session_questions = [[topic]]
        assert profile.should_skip_teaching(topic) is False

    def test_skip_requires_both_conditions(self):
        """Taught enough, but asked recently -> not skipped."""
        profile = UserProfile()
        topic = "spec:n_bolts"
        # Teach 5 times
        for _ in range(UserProfile._TAUGHT_THRESHOLD):
            profile.record_teaching(topic)
        # Simulate recent asking
        profile._recent_session_questions = [[topic], []]
        assert profile.should_skip_teaching(topic) is False


# ---------------------------------------------------------------------------
# UserProfile: get_focus_topics and suggest_difficulty
# ---------------------------------------------------------------------------

class TestProfileQueries:
    def test_get_focus_topics_empty(self):
        profile = UserProfile()
        assert profile.get_focus_topics() == []

    def test_get_focus_topics_sorted_by_frequency(self):
        profile = UserProfile()
        # Make "dfm" a struggling topic asked 5 times
        for _ in range(5):
            profile.record_question("dfm")
        # Make "wall_thickness" a struggling topic asked 3 times
        for _ in range(3):
            profile.record_question("wall_thickness")
        topics = profile.get_focus_topics()
        assert topics[0] == "dfm"
        assert topics[1] == "wall_thickness"

    def test_suggest_difficulty_new_user_is_beginner(self):
        profile = UserProfile()
        assert profile.suggest_difficulty() == "beginner"

    def test_suggest_difficulty_few_sessions_is_beginner(self):
        profile = UserProfile()
        profile.sessions_completed = 2
        for _ in range(10):
            profile.record_teaching("some_topic")
        assert profile.suggest_difficulty() == "beginner"

    def test_suggest_difficulty_intermediate(self):
        profile = UserProfile()
        profile.sessions_completed = 5
        for _ in range(10):
            profile.record_teaching("some_topic")
        assert profile.suggest_difficulty() == "intermediate"

    def test_suggest_difficulty_expert(self):
        profile = UserProfile()
        profile.sessions_completed = 10
        for _ in range(20):
            profile.record_teaching("some_topic")
        # Few struggling topics
        assert profile.suggest_difficulty() == "expert"

    def test_suggest_difficulty_expert_blocked_by_struggles(self):
        profile = UserProfile()
        profile.sessions_completed = 10
        for _ in range(20):
            profile.record_teaching("some_topic")
        # Inject many struggling topics to block expert classification
        for i in range(5):
            for _ in range(UserProfile._STRUGGLING_THRESHOLD):
                profile.record_question(f"topic_{i}")
        assert profile.suggest_difficulty() == "intermediate"


# ---------------------------------------------------------------------------
# UserProfile: serialization and persistence
# ---------------------------------------------------------------------------

class TestUserProfilePersistence:
    def test_to_dict_roundtrip(self):
        profile = UserProfile()
        profile.record_teaching("dfm:od_mm")
        profile.record_question("wall_thickness")
        profile.record_question("wall_thickness")
        profile.record_question("wall_thickness")
        profile.record_mistake("thin_wall")
        profile.sessions_completed = 2
        profile.parts_generated = 5
        profile.difficulty_history = ["intermediate", "expert"]

        d = profile.to_dict()
        restored = UserProfile.from_dict(d)

        assert restored.topics_taught == profile.topics_taught
        assert restored.topics_asked == profile.topics_asked
        assert restored.common_mistakes == profile.common_mistakes
        assert restored.sessions_completed == profile.sessions_completed
        assert restored.parts_generated == profile.parts_generated
        assert restored.difficulty_history == profile.difficulty_history
        assert restored.struggling_topics == profile.struggling_topics

    def test_save_and_load_roundtrip(self, tmp_path):
        profile_path = tmp_path / "user_profile.json"
        profile = UserProfile()
        profile.record_teaching("spec:n_bolts")
        profile.record_teaching("spec:n_bolts")
        profile.record_question("dfm")
        profile.record_mistake("draft_angle")
        profile.sessions_completed = 3
        profile.parts_generated = 7

        profile.save(path=profile_path)
        assert profile_path.is_file()

        loaded = UserProfile.load(path=profile_path)
        assert loaded.topics_taught == {"spec:n_bolts": 2}
        assert loaded.topics_asked == {"dfm": 1}
        assert loaded.common_mistakes == {"draft_angle": 1}
        assert loaded.sessions_completed == 3
        assert loaded.parts_generated == 7

    def test_load_missing_file_returns_fresh_profile(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        profile = UserProfile.load(path=missing)
        assert profile.sessions_completed == 0
        assert profile.topics_taught == {}

    def test_load_corrupted_file_returns_fresh_profile(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{", encoding="utf-8")
        profile = UserProfile.load(path=bad_file)
        assert profile.sessions_completed == 0

    def test_save_creates_parent_directory(self, tmp_path):
        nested_path = tmp_path / "nested" / "dir" / "profile.json"
        profile = UserProfile()
        profile.save(path=nested_path)
        assert nested_path.is_file()


# ---------------------------------------------------------------------------
# MistakeDetector
# ---------------------------------------------------------------------------

class TestMistakeDetector:
    def test_check_recurring_below_threshold_returns_none(self):
        profile = UserProfile()
        profile.record_mistake("thin_wall")
        profile.record_mistake("thin_wall")
        detector = MistakeDetector(profile)
        assert detector.check_recurring("thin_wall", threshold=3) is None

    def test_check_recurring_at_threshold_returns_message(self):
        profile = UserProfile()
        for _ in range(3):
            profile.record_mistake("thin_wall")
        detector = MistakeDetector(profile)
        msg = detector.check_recurring("thin_wall", threshold=3)
        assert msg is not None
        assert "thin wall" in msg.lower()
        assert "3" in msg

    def test_check_recurring_above_threshold_returns_message(self):
        profile = UserProfile()
        for _ in range(5):
            profile.record_mistake("draft_angle")
        detector = MistakeDetector(profile)
        msg = detector.check_recurring("draft_angle", threshold=3)
        assert msg is not None
        assert "5" in msg

    def test_check_recurring_message_contains_rule_of_thumb(self):
        profile = UserProfile()
        for _ in range(3):
            profile.record_mistake("thin_wall")
        detector = MistakeDetector(profile)
        msg = detector.check_recurring("thin_wall")
        # Should include a concrete manufacturing tip, not generic advice
        assert any(kw in msg.lower() for kw in ["cnc", "fdm", "injection", "mm", "diameter"])

    def test_check_recurring_unknown_issue_type_uses_fallback(self):
        profile = UserProfile()
        for _ in range(3):
            profile.record_mistake("mystery_issue_xyz")
        detector = MistakeDetector(profile)
        msg = detector.check_recurring("mystery_issue_xyz")
        assert msg is not None
        assert len(msg) > 20  # has real content, not empty

    def test_check_recurring_custom_threshold(self):
        profile = UserProfile()
        for _ in range(5):
            profile.record_mistake("undercut")
        detector = MistakeDetector(profile)
        # Below custom threshold of 10
        assert detector.check_recurring("undercut", threshold=10) is None
        # At default threshold of 3
        assert detector.check_recurring("undercut", threshold=3) is not None

    def test_get_pattern_report_empty_for_clean_profile(self):
        profile = UserProfile()
        detector = MistakeDetector(profile)
        assert detector.get_pattern_report() == []

    def test_get_pattern_report_below_threshold_still_empty(self):
        profile = UserProfile()
        profile.record_mistake("thin_wall")
        profile.record_mistake("thin_wall")
        detector = MistakeDetector(profile)
        assert detector.get_pattern_report() == []

    def test_get_pattern_report_returns_recurring_issues(self):
        profile = UserProfile()
        for _ in range(4):
            profile.record_mistake("thin_wall")
        for _ in range(3):
            profile.record_mistake("draft_angle")
        detector = MistakeDetector(profile)
        report = detector.get_pattern_report()
        assert len(report) == 2
        # Sorted by count descending
        assert report[0]["issue_type"] == "thin_wall"
        assert report[0]["count"] == 4
        assert report[1]["issue_type"] == "draft_angle"
        assert report[1]["count"] == 3

    def test_get_pattern_report_entry_structure(self):
        profile = UserProfile()
        for _ in range(3):
            profile.record_mistake("no_fillet")
        detector = MistakeDetector(profile)
        report = detector.get_pattern_report()
        assert len(report) == 1
        entry = report[0]
        assert "issue_type" in entry
        assert "count" in entry
        assert "suggestion" in entry
        assert entry["issue_type"] == "no_fillet"
        assert entry["count"] == 3
        assert len(entry["suggestion"]) > 20

    def test_all_known_issue_types_have_specific_tips(self):
        """Every entry in _ISSUE_TIPS should produce a tip, not the generic fallback."""
        from aria_os.teaching.user_profile import _ISSUE_TIPS, _GENERIC_TIP
        for issue_type in _ISSUE_TIPS:
            profile = UserProfile()
            for _ in range(3):
                profile.record_mistake(issue_type)
            detector = MistakeDetector(profile)
            msg = detector.check_recurring(issue_type)
            assert msg is not None
            assert _GENERIC_TIP not in msg, f"{issue_type} fell through to generic tip"


# ---------------------------------------------------------------------------
# TeachingEngine integration with UserProfile
# ---------------------------------------------------------------------------

class TestTeachingEngineWithProfile:
    def test_teach_records_in_profile(self):
        from aria_os.teaching.engine import TeachingEngine
        profile = UserProfile()
        engine = TeachingEngine(user_profile=profile)
        engine.teach_simple("SpecAgent", "spec", "Chose od_mm=80", related_param="od_mm")
        assert profile.topics_taught.get("spec:od_mm") == 1

    def test_teach_without_related_param_not_recorded(self):
        from aria_os.teaching.engine import TeachingEngine
        profile = UserProfile()
        engine = TeachingEngine(user_profile=profile)
        engine.teach_simple("SpecAgent", "spec", "Some teaching", related_param="")
        assert profile.topics_taught == {}

    def test_check_mistakes_records_and_returns_warnings(self):
        from aria_os.teaching.engine import TeachingEngine
        profile = UserProfile()
        # Pre-seed the profile so we're at the threshold
        for _ in range(2):
            profile.record_mistake("thin_wall")
        engine = TeachingEngine(user_profile=profile)
        # This call adds 1 more -> crosses threshold of 3
        warnings = engine.check_mistakes([{"issue_type": "thin_wall"}])
        assert len(warnings) == 1
        assert "thin wall" in warnings[0].lower()

    def test_check_mistakes_below_threshold_no_warnings(self):
        from aria_os.teaching.engine import TeachingEngine
        profile = UserProfile()
        engine = TeachingEngine(user_profile=profile)
        warnings = engine.check_mistakes([{"issue_type": "draft_angle"}])
        assert warnings == []

    def test_check_mistakes_no_profile_returns_empty(self):
        from aria_os.teaching.engine import TeachingEngine
        engine = TeachingEngine(user_profile=None)
        warnings = engine.check_mistakes([{"issue_type": "thin_wall"}])
        assert warnings == []

    def test_check_mistakes_accepts_type_key_alias(self):
        from aria_os.teaching.engine import TeachingEngine
        profile = UserProfile()
        for _ in range(2):
            profile.record_mistake("undercut")
        engine = TeachingEngine(user_profile=profile)
        warnings = engine.check_mistakes([{"type": "undercut"}])
        assert profile.common_mistakes["undercut"] == 3
