"""Smoke tests for the self-extension orchestrator and its stages.
All run in dry-run mode (no Claude Code sub-agent calls)."""
from __future__ import annotations

import pytest

from aria_os.self_extend.orchestrator import (
    ExtensionRequest, Domain, Stage, run_extension_request,
)


def test_orchestrator_shortcircuits_on_template_hit():
    """A prompt that matches an existing CadQuery template should not
    invoke the discovery loop."""
    req = ExtensionRequest.new(
        goal="simple bracket 50mm wide 30mm tall 4mm thick 2 M4 bolts")
    result = run_extension_request(req, dry_run=True)
    assert result.success, result.error
    assert result.candidates_tried == 0, "no discovery should run"
    # Must have walked dispatch + template_match stages
    stages = {e.stage for e in result.events}
    assert Stage.DISPATCH in stages
    assert Stage.TEMPLATE_MATCH in stages
    # Should NOT have walked hypothesis
    assert Stage.HYPOTHESIS not in stages


def test_orchestrator_runs_discovery_loop_on_template_miss():
    """A prompt that no existing template covers should walk through
    dispatch → hypothesis → implement → contract → (fail)."""
    req = ExtensionRequest.new(
        goal="exotic auxetic metamaterial cube with negative poisson ratio "
             "chiral cellular structure")
    result = run_extension_request(req, dry_run=True)
    # Not expected to succeed in dry-run — stub candidate can't satisfy
    # the flange fixture. But the stages should all fire.
    stages = {e.stage for e in result.events}
    assert Stage.DISPATCH in stages
    assert Stage.TEMPLATE_MATCH in stages
    assert Stage.HYPOTHESIS in stages
    assert Stage.IMPLEMENT in stages
    assert Stage.CONTRACT in stages
    assert result.candidates_tried >= 1


def test_dispatcher_classifies_ecad_correctly():
    from aria_os.self_extend.dispatcher import classify_request
    req = ExtensionRequest.new(
        goal="design a KiCad PCB for an ESP32 flight controller")
    assert classify_request(req, dry_run=True) == Domain.ECAD


def test_dispatcher_classifies_lattice_correctly():
    from aria_os.self_extend.dispatcher import classify_request
    req = ExtensionRequest.new(
        goal="octet-truss infilled cube with stress-driven FGM density")
    assert classify_request(req, dry_run=True) == Domain.LATTICE


def test_contract_reports_fixture_failure_cleanly():
    """Verify that a candidate failing bbox tolerance produces a clear
    failure reason that can be fed back into Hypothesis."""
    from aria_os.self_extend.contracts import contract_failure_prompt
    report = {
        "kind": "cadquery",
        "fixtures_run": 2,
        "fixtures_pass": 1,
        "reason": "bbox out of tolerance: got (50,30,4), expected (60,60,5)",
        "per_fixture": [
            {"fixture": "bracket_standard", "passed": True, "reason": None},
            {"fixture": "flange_small", "passed": False,
             "reason": "bbox out of tolerance"},
        ],
    }
    prompt = contract_failure_prompt(report)
    assert "Contract suite failed: 1/2" in prompt
    assert "flange_small" in prompt
    assert "bbox" in prompt.lower()


def test_trust_register_and_check():
    """Newly-registered modules are QUARANTINED; first check_before_use
    denies until approve_module is called."""
    import os
    import tempfile
    store = tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=".json")
    store.write("{}")
    store.close()
    os.environ["ARIA_TRUST_STORE"] = store.name

    from aria_os.self_extend.trust import (
        register_new_module, check_before_use, approve_module, TrustState,
    )
    try:
        state = register_new_module(
            module_path="aria_os/generators/_cand_xyz.py",
            request_id="test-1", winner_metrics={"safety_factor": 2.4})
        assert state == TrustState.QUARANTINED.value

        verdict = check_before_use("aria_os/generators/_cand_xyz.py")
        assert not verdict.allowed
        assert verdict.state == TrustState.QUARANTINED

        approve_module("aria_os/generators/_cand_xyz.py")
        verdict2 = check_before_use("aria_os/generators/_cand_xyz.py")
        assert verdict2.allowed
        assert verdict2.state == TrustState.TRUSTED
    finally:
        os.environ.pop("ARIA_TRUST_STORE", None)
        os.unlink(store.name)


def test_pr_writer_dry_run_returns_fake_url():
    from aria_os.self_extend.pr_writer import write_pr
    req = ExtensionRequest.new(goal="test goal")
    best = {
        "candidate": {"name": "cand1", "kind": "cadquery",
                      "module_relpath": "aria_os/generators/_cand_1.py",
                      "rationale": "test", "parent_primitives": []},
        "verdict": {"passed": True, "score": 2.4, "metrics": {}},
        "sandbox_worktree": "/tmp/fake_worktree",
    }
    pr = write_pr(best, request=req, dry_run=True)
    assert pr["url"].startswith("https://example.com/pr/")
    assert pr["branch"].startswith("aria-agent/")
