"""W10 knowledge-loop tests.

Pin contracts for: feedback module, /api/feedback endpoint,
auto-promoter classification, failure miner clustering, SFT/DPO
exporters, A/B variant comparator, insights endpoints.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _client():
    from fastapi.testclient import TestClient
    from dashboard.aria_server import app
    return TestClient(app)


# --- W10.1 feedback module + endpoint ---------------------------------

class TestFeedbackModule:
    def test_plan_hash_stable_across_alias_renames(self):
        from aria_os.feedback import compute_plan_hash
        plan_a = [
            {"kind": "extrude",
             "params": {"sketch": "s", "distance": 5,
                          "operation": "new", "alias": "extrude_1"}},
        ]
        plan_b = [
            {"kind": "extrude",
             "params": {"sketch": "s", "distance": 5,
                          "operation": "new", "alias": "extrude_42"}},
        ]
        assert compute_plan_hash(plan_a) == compute_plan_hash(plan_b)

    def test_plan_hash_changes_on_real_diff(self):
        from aria_os.feedback import compute_plan_hash
        plan_a = [{"kind": "extrude",
                    "params": {"distance": 5, "operation": "new"}}]
        plan_b = [{"kind": "extrude",
                    "params": {"distance": 10, "operation": "new"}}]
        assert compute_plan_hash(plan_a) != compute_plan_hash(plan_b)

    def test_record_and_load(self, tmp_path):
        from aria_os.feedback import (FeedbackEntry, record_feedback,
                                          load_all_feedback)
        e = FeedbackEntry(run_id="abc",
                            goal="flange 100mm OD",
                            plan=[{"kind": "extrude",
                                    "params": {"distance": 6,
                                                "operation": "new"}}],
                            decision="accept", reason="looks good")
        path = record_feedback(e, repo_root=tmp_path)
        assert path.is_file()
        all_entries = load_all_feedback(repo_root=tmp_path)
        assert len(all_entries) == 1
        assert all_entries[0]["run_id"] == "abc"
        assert all_entries[0]["plan_hash"]

    def test_invalid_decision_rejected(self, tmp_path):
        from aria_os.feedback import FeedbackEntry, record_feedback
        e = FeedbackEntry(run_id="x", goal="g", plan=[],
                            decision="garbage")
        with pytest.raises(ValueError):
            record_feedback(e, repo_root=tmp_path)

    def test_stats_aggregation(self, tmp_path):
        from aria_os.feedback import (FeedbackEntry, record_feedback,
                                          stats)
        for i, decision in enumerate(["accept", "accept", "reject",
                                        "needs_revision"]):
            record_feedback(
                FeedbackEntry(run_id=f"r{i}", goal=f"goal {i}",
                                plan=[{"kind": "extrude", "params": {}}],
                                decision=decision,
                                failed_op_index=0 if decision == "reject"
                                else None),
                repo_root=tmp_path)
        s = stats(repo_root=tmp_path)
        assert s["n_total"] == 4
        assert s["counts"]["accept"] == 2
        assert s["counts"]["reject"] == 1
        assert "extrude" in s["by_failed_op"]


class TestFeedbackEndpoint:
    def test_post_accept(self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        c = _client()
        r = c.post("/api/feedback", json={
            "run_id": "test1", "goal": "x",
            "plan": [{"kind": "extrude",
                       "params": {"distance": 5}}],
            "decision": "accept",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"]
        assert body["plan_hash"]

    def test_post_invalid_decision_422(self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        c = _client()
        r = c.post("/api/feedback", json={
            "run_id": "x", "goal": "g", "plan": [],
            "decision": "wat"})
        assert r.status_code == 422

    def test_get_stats(self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        c = _client()
        # Empty stats
        r = c.get("/api/feedback/stats")
        assert r.status_code == 200
        assert r.json()["n_total"] == 0


# --- W10.2 auto-promoter classification ------------------------------

class TestPromoteFewShots:
    def test_classifier_picks_specific_family(self):
        # Need to import via runpy since scripts/ isn't a package
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "promote_fewshots",
            Path(__file__).resolve().parents[1] / "scripts"
            / "promote_fewshots.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Hardware family wins over generic extrude
        plan = [{"kind": "extrude", "params": {}},
                 {"kind": "threadFeature", "params": {}}]
        assert mod._classify_family(plan) == "hardware"
        # Sheet metal beats generic
        plan = [{"kind": "sheetMetalBase", "params": {}},
                 {"kind": "extrude", "params": {}}]
        assert mod._classify_family(plan) == "sheet_metal"
        # Falls back to extrude when nothing specific
        plan = [{"kind": "extrude", "params": {}}]
        assert mod._classify_family(plan) == "extrude"

    def test_ranks_by_op_coverage(self, tmp_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "promote_fewshots",
            Path(__file__).resolve().parents[1] / "scripts"
            / "promote_fewshots.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Two plans in the same family (both classify as "extrude"
        # because neither has more-specific ops). The richer one
        # should rank first.
        accepted = [
            {"goal": "simple", "plan_hash": "h_simple",
             "plan": [{"kind": "extrude", "params": {}}]},
            {"goal": "rich", "plan_hash": "h_rich",
             "plan": [{"kind": "extrude", "params": {}},
                       {"kind": "fillet",  "params": {}},
                       {"kind": "newSketch", "params": {}}]},
        ]
        ranked = mod.rank_per_family(accepted, top_n=10)
        assert "extrude" in ranked
        assert ranked["extrude"][0]["plan_hash"] == "h_rich"


# --- W10.3 failure miner clustering ----------------------------------

class TestFailureMiner:
    def _load_miner(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mine_failures",
            Path(__file__).resolve().parents[1] / "scripts"
            / "mine_failures.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_jaccard_basic(self):
        mod = self._load_miner()
        a = mod._tokens("the bolt holes are wrong")
        b = mod._tokens("bolt holes look wrong")
        assert mod._jaccard(a, b) > 0.5

    def test_clustering_groups_similar(self):
        mod = self._load_miner()
        items = [
            (0, "wall thickness too thin"),
            (1, "thickness wall is too thin"),
            (2, "wrong number of holes"),
        ]
        clusters = mod._cluster(items, threshold=0.4)
        # 0+1 cluster, 2 standalone
        assert len(clusters) == 2
        sizes = sorted(len(c) for c in clusters)
        assert sizes == [1, 2]


# --- W10.4 SFT / DPO export ------------------------------------------

class TestExportSft:
    def test_sft_dedupes_by_plan_hash(self, tmp_path):
        from aria_os.feedback import FeedbackEntry, record_feedback
        from aria_os.training.export_sft import export_sft
        # Two accepts with the same plan_hash → only one row out
        plan = [{"kind": "extrude",
                  "params": {"distance": 5, "operation": "new"}}]
        record_feedback(
            FeedbackEntry(run_id="a", goal="g", plan=plan,
                            decision="accept"), repo_root=tmp_path)
        record_feedback(
            FeedbackEntry(run_id="b", goal="g", plan=plan,
                            decision="accept"), repo_root=tmp_path)
        out_path = export_sft(tmp_path, format="anthropic")
        rows = [json.loads(line) for line in
                out_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        assert len(rows) == 1
        # Schema check
        msgs = rows[0]["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"

    def test_dpo_pairs_only_when_both_decisions_present(self, tmp_path):
        from aria_os.feedback import FeedbackEntry, record_feedback
        from aria_os.training.export_sft import export_dpo
        # Same goal, accept + reject → produces a pair
        accepted_plan = [{"kind": "extrude",
                            "params": {"distance": 5,
                                        "operation": "new"}}]
        rejected_plan = [{"kind": "extrude",
                            "params": {"distance": 0,
                                        "operation": "new"}}]
        record_feedback(
            FeedbackEntry(run_id="a", goal="flange 100mm",
                            plan=accepted_plan,
                            decision="accept"), repo_root=tmp_path)
        record_feedback(
            FeedbackEntry(run_id="b", goal="flange 100mm",
                            plan=rejected_plan, reason="zero distance",
                            decision="reject"), repo_root=tmp_path)
        out = export_dpo(tmp_path)
        assert out is not None
        rows = [json.loads(line) for line in
                out.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        assert len(rows) == 1
        assert "chosen" in rows[0] and "rejected" in rows[0]
        assert rows[0]["metadata"]["rejected_reason"] == "zero distance"

    def test_dpo_returns_none_when_no_pairs(self, tmp_path):
        from aria_os.training.export_sft import export_dpo
        # Empty feedback
        assert export_dpo(tmp_path) is None


# --- W10.6 insights endpoints ----------------------------------------

class TestInsightsEndpoints:
    def test_eval_history_empty(self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        c = _client()
        r = c.get("/api/insights/eval_history")
        assert r.status_code == 200
        assert r.json() == []

    def test_fewshots_count(self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        # Set up a fake fewshots dir
        fs = tmp_path / "aria_os" / "native_planner" / "fewshots"
        fs.mkdir(parents=True)
        (fs / "flange.json").write_text("{}")           # curated
        (fs / "auto_extrude_abc.json").write_text("{}")  # auto
        (fs / "auto_sheet_metal_xyz.json").write_text("{}")  # auto
        c = _client()
        r = c.get("/api/insights/fewshots")
        assert r.status_code == 200
        body = r.json()
        assert body["curated_count"] == 1
        assert body["auto_count"] == 2

    def test_ab_latest_empty(self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        c = _client()
        r = c.get("/api/insights/ab_latest")
        assert r.status_code == 200
        assert r.json() == {}

    def test_insights_view_serves_html(self):
        c = _client()
        r = c.get("/insights")
        # The dashboard file is in the repo, so this should 200
        assert r.status_code == 200
        assert "ARIA-OS Insights" in r.text
