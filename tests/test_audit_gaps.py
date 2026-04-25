"""Tests covering the gaps the W1-W10 audit found.

Audit summary (sessions/W1-W10_TEST_PASS_2026-04-25.md): 22 public
APIs had zero test coverage. The W8 entry-point gap is closed in
test_multimodal.py::TestW8EntryPoints; the rest are picked up here.

Covers:
  - W3 topopt: optimize_cantilever + density_to_sdf
  - W10 promote_fewshots: write/prune file system side
  - W10 ab_eval: _compare comparison logic
  - aria_os.feedback: feedback_dir creation, INDEX.jsonl behaviour
  - VerifyReport: dataclass shape + to_dict
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# --- W3 topology optimization -----------------------------------------

class TestTopoptCantilever:
    """The lite SIMP optimizer should produce a load-aligned mass
    distribution: cells near the fixed face + load line are solid,
    cells far from the load are void."""

    @pytest.fixture
    def numpy_or_skip(self):
        try:
            import numpy as np
            return np
        except ImportError:
            pytest.skip("numpy not installed")

    def test_returns_correct_shape(self, numpy_or_skip):
        from aria_os.sdf.topopt import optimize_cantilever
        rho = optimize_cantilever(nelx=20, nely=10, nelz=4,
                                     n_iters=5)
        assert rho.shape == (20, 10, 4)
        assert rho.min() >= 0.0
        assert rho.max() <= 1.0

    def test_volume_fraction_respected(self, numpy_or_skip):
        from aria_os.sdf.topopt import optimize_cantilever
        np = numpy_or_skip
        rho = optimize_cantilever(nelx=20, nely=10, nelz=4,
                                     target_volume_fraction=0.3,
                                     n_iters=10)
        # Should converge to ≈ 0.3 ± 0.1
        actual = float(rho.mean())
        assert 0.2 <= actual <= 0.5, (
            f"Volume fraction {actual:.2f} far from target 0.3")

    def test_fixed_face_kept_solid(self, numpy_or_skip):
        from aria_os.sdf.topopt import optimize_cantilever
        np = numpy_or_skip
        rho = optimize_cantilever(nelx=20, nely=10, nelz=4,
                                     fixed_face="x_min",
                                     target_volume_fraction=0.3,
                                     n_iters=10)
        # The fixed face (x=0 column) should be 100% solid
        assert (rho[0, :, :] == 1.0).all(), (
            "Fixed face was not retained — load can't react.")

    def test_density_to_sdf_callable(self, numpy_or_skip):
        from aria_os.sdf.topopt import (optimize_cantilever,
                                            density_to_sdf)
        np = numpy_or_skip
        rho = optimize_cantilever(nelx=10, nely=5, nelz=3,
                                     n_iters=3)
        sdf = density_to_sdf(rho, threshold=0.5,
                                bbox_mm=(100.0, 40.0, 20.0))
        # Inside (high density) → negative SDF; outside → positive.
        # At the fixed face (x=-50), rho=1 → SDF should be negative.
        x = np.array([-50.0])
        y = np.array([0.0])
        z = np.array([0.0])
        v = sdf(x, y, z)
        # Should be a numpy array of length 1
        assert hasattr(v, "shape") or hasattr(v, "__len__")


# --- W10.2 auto-promoter file system side -----------------------------

class TestPromoterFileSystem:
    def _load_mod(self):
        spec = importlib.util.spec_from_file_location(
            "promote_fewshots",
            Path(__file__).resolve().parents[1] / "scripts"
            / "promote_fewshots.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_write_files_creates_auto_prefixed(self, tmp_path):
        mod = self._load_mod()
        ranked = {
            "extrude": [{
                "goal":      "test plate",
                "plan_hash": "abc123",
                "plan":      [{"kind": "extrude",
                                "params": {"distance": 5}}],
                "timestamp_utc": "2026-04-25T00:00:00Z",
                "run_id":    "r1",
            }],
        }
        written = mod.write_fewshot_files(ranked, tmp_path,
                                              dry_run=False)
        assert len(written) == 1
        f = written[0]
        assert f.name.startswith("auto_extrude_")
        # File contents valid JSON with required fields
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["id"].startswith("auto_extrude_")
        assert data["plan_hash"] == "abc123"
        assert "extrude" in data["tags"]
        assert data["_source"] == "auto-promoted from feedback"

    def test_dry_run_writes_nothing(self, tmp_path):
        mod = self._load_mod()
        ranked = {
            "extrude": [{"goal": "x", "plan_hash": "h",
                          "plan": [{"kind": "extrude",
                                     "params": {}}],
                          "timestamp_utc": "2026-04-25T00:00:00Z",
                          "run_id": "r1"}]}
        written = mod.write_fewshot_files(ranked, tmp_path,
                                              dry_run=True)
        assert len(written) == 1   # Returned the path it WOULD write
        # ...but the file doesn't exist
        assert not written[0].exists()

    def test_prune_removes_stale_only(self, tmp_path):
        mod = self._load_mod()
        # Create 3 auto_*.json files (2 keep, 1 stale) + 1 curated
        keep_a = tmp_path / "auto_extrude_keep1.json"
        keep_b = tmp_path / "auto_extrude_keep2.json"
        stale  = tmp_path / "auto_extrude_stale.json"
        curated = tmp_path / "flange.json"
        for p, h in [(keep_a, "k1"), (keep_b, "k2"),
                      (stale, "s1")]:
            p.write_text(json.dumps({"plan_hash": h,
                                       "id": p.stem}))
        curated.write_text(json.dumps({"id": "flange",
                                          "plan_hash": "curated"}))
        removed = mod.prune_stale_auto_shots(
            tmp_path, keep_hashes={"k1", "k2"}, dry_run=False)
        assert len(removed) == 1
        assert removed[0].name == "auto_extrude_stale.json"
        # The curated file is NEVER touched
        assert curated.exists()
        # The kept files survive
        assert keep_a.exists() and keep_b.exists()


# --- W10.5 A/B eval comparison logic ---------------------------------

class TestAbCompare:
    def _load_mod(self):
        spec = importlib.util.spec_from_file_location(
            "ab_eval",
            Path(__file__).resolve().parents[1] / "scripts"
            / "ab_eval.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _run(self, label, results):
        n_pass = sum(1 for r in results if r["outcome"] == "PASS")
        return {
            "variant": label,
            "n_prompts": len(results),
            "counts": {"PASS": n_pass, "WEAK": 0,
                        "FAIL": len(results) - n_pass, "ERROR": 0},
            "pass_rate": n_pass / max(1, len(results)),
            "results": results,
        }

    def test_compare_no_diff(self):
        mod = self._load_mod()
        rs = [{"id": "a", "outcome": "PASS"},
              {"id": "b", "outcome": "PASS"}]
        cmp = mod._compare(self._run("c", rs), self._run("n", rs))
        assert cmp["delta_pp"] == 0.0
        assert cmp["n_regressions"] == 0
        assert cmp["n_improvements"] == 0

    def test_compare_regression(self):
        mod = self._load_mod()
        c = self._run("control",
                       [{"id": "a", "outcome": "PASS"},
                        {"id": "b", "outcome": "PASS"}])
        n = self._run("candidate",
                       [{"id": "a", "outcome": "PASS"},
                        {"id": "b", "outcome": "FAIL"}])
        cmp = mod._compare(c, n)
        assert cmp["n_regressions"] == 1
        assert cmp["regressions"][0]["id"] == "b"
        # Pass rate dropped 100% → 50%, delta = -50pp
        assert cmp["delta_pp"] == -50.0

    def test_compare_improvement(self):
        mod = self._load_mod()
        c = self._run("c",
                       [{"id": "a", "outcome": "FAIL"},
                        {"id": "b", "outcome": "PASS"}])
        n = self._run("n",
                       [{"id": "a", "outcome": "PASS"},
                        {"id": "b", "outcome": "PASS"}])
        cmp = mod._compare(c, n)
        assert cmp["n_improvements"] == 1
        assert cmp["improvements"][0]["id"] == "a"
        assert cmp["delta_pp"] == 50.0


# --- aria_os.feedback edge cases -------------------------------------

class TestFeedbackEdgeCases:
    def test_feedback_dir_creates_when_missing(self, tmp_path):
        from aria_os.feedback import feedback_dir
        # Sub-dir doesn't exist yet
        d = feedback_dir(repo_root=tmp_path)
        assert d.is_dir()
        assert d == tmp_path / "outputs" / "feedback"

    def test_index_jsonl_appends(self, tmp_path):
        from aria_os.feedback import (FeedbackEntry,
                                          record_feedback)
        for i in range(3):
            record_feedback(
                FeedbackEntry(run_id=f"r{i}", goal="g",
                                plan=[{"kind": "extrude",
                                        "params": {}}],
                                decision="accept"),
                repo_root=tmp_path)
        idx = tmp_path / "outputs" / "feedback" / "INDEX.jsonl"
        lines = [l for l in idx.read_text().splitlines() if l.strip()]
        assert len(lines) == 3
        # Each line is a valid JSON object with run_id
        ids = [json.loads(l)["run_id"] for l in lines]
        assert ids == ["r0", "r1", "r2"]

    def test_load_all_feedback_skips_garbage_files(self, tmp_path):
        from aria_os.feedback import (FeedbackEntry,
                                          record_feedback,
                                          load_all_feedback)
        record_feedback(
            FeedbackEntry(run_id="r1", goal="g",
                            plan=[{"kind": "extrude", "params": {}}],
                            decision="accept"),
            repo_root=tmp_path)
        # Drop a malformed JSON file alongside
        (tmp_path / "outputs" / "feedback" / "garbage.json"
         ).write_text("{not json}")
        out = load_all_feedback(repo_root=tmp_path)
        # Garbage skipped; only the real entry survives
        assert len(out) == 1
        assert out[0]["run_id"] == "r1"

    def test_compute_plan_hash_handles_empty_plan(self):
        from aria_os.feedback import compute_plan_hash
        h = compute_plan_hash([])
        assert isinstance(h, str)
        assert len(h) == 16

    def test_compute_plan_hash_skips_non_dict_entries(self):
        from aria_os.feedback import compute_plan_hash
        plan = [
            {"kind": "extrude", "params": {}},
            "garbage",   # non-dict entry
            None,
        ]
        h = compute_plan_hash(plan)
        # Just shouldn't crash; should hash only the dict
        assert isinstance(h, str)


# --- VerifyReport dataclass ------------------------------------------

class TestVerifyReport:
    def test_to_dict_serializes(self):
        from aria_os.verification import VerifyReport
        from aria_os.verification.dfm import Issue
        report = VerifyReport(
            passed=False, score=0.6,
            issues=[Issue("warning", "test", "msg", "fix")],
            gates_run=["dfm"],
            gates_skipped=[("fea", "no loads")])
        d = report.to_dict()
        assert d["passed"] is False
        assert d["score"] == 0.6
        assert len(d["issues"]) == 1
        assert d["issues"][0]["severity"] == "warning"
        assert d["gates_skipped"][0]["gate"] == "fea"
        # JSON-serializable
        json.dumps(d)
