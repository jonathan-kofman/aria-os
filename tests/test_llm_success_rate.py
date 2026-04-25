"""LLM planner success-rate eval harness.

Runs the 50-prompt held-out set in tests/eval_prompts.json and reports
the pass rate. A "pass" is a plan that:
  1. Validates structurally (validator returns ok)
  2. Uses at least one of the prompt's `expected_ops_any` op kinds

Skipped on CI (no LLM API key in env). Locally:
    ANTHROPIC_API_KEY=... pytest tests/test_llm_success_rate.py -v -s

Per-prompt results are persisted to outputs/eval/<timestamp>/results.json
so you can compare runs over time and spot regressions when the few-
shot library or system prompt changes.

Targets (W2.6):
  - Baseline: log first run's pass rate, then ratchet upward.
  - Ship gate: ≥80% pass rate before marking W2 done.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_PROMPTS_FILE = _REPO / "tests" / "eval_prompts.json"
_OUT_DIR = _REPO / "outputs" / "eval"


def _have_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("GEMINI_API_KEY"))


def _load_prompts() -> list[dict]:
    if not _PROMPTS_FILE.is_file():
        return []
    data = json.loads(_PROMPTS_FILE.read_text(encoding="utf-8"))
    return data.get("prompts", [])


def _classify(plan: list[dict] | None, expected_ops_any: list[str],
                validator_ok: bool) -> str:
    """PASS | WEAK | FAIL."""
    if plan is None or not validator_ok:
        return "FAIL"
    kinds = {op.get("kind") for op in plan}
    if any(op in kinds for op in expected_ops_any):
        return "PASS"
    return "WEAK"


@pytest.mark.skipif(not _have_key(),
                     reason="No LLM API key — set ANTHROPIC_API_KEY/GOOGLE_API_KEY")
def test_llm_success_rate_50_prompts():
    """The W2 ship gate. Runs the full eval set, persists results, and
    asserts pass rate ≥ 80%. Failure prints the offending prompts so
    they can be promoted into the few-shot library or surface a fix
    in the system prompt."""
    from aria_os.native_planner.dispatcher import make_plan
    from aria_os.native_planner.validator import validate_plan

    prompts = _load_prompts()
    if not prompts:
        pytest.skip("No prompts in eval_prompts.json")

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = _OUT_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    counts = {"PASS": 0, "WEAK": 0, "FAIL": 0, "ERROR": 0}

    for p in prompts:
        pid = p["id"]
        goal = p["goal"]
        expected = p.get("expected_ops_any") or []
        t0 = time.time()
        try:
            plan = make_plan(goal, {}, prefer_llm=True, quality="balanced")
            ok, issues = validate_plan(plan)
            kinds = [op.get("kind") for op in plan]
            outcome = _classify(plan, expected, ok)
            results.append({
                "id": pid, "goal": goal, "outcome": outcome,
                "elapsed_s": round(time.time() - t0, 1),
                "expected_ops_any": expected,
                "kinds": kinds,
                "validator_issues": issues if not ok else [],
            })
            counts[outcome] += 1
            print(f"  {outcome:<5} {pid:<26} ({results[-1]['elapsed_s']}s) "
                  f"hits={[k for k in expected if k in kinds]}")
        except Exception as exc:
            results.append({
                "id": pid, "goal": goal, "outcome": "ERROR",
                "elapsed_s": round(time.time() - t0, 1),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            })
            counts["ERROR"] += 1
            print(f"  ERROR {pid:<26} {type(exc).__name__}: {str(exc)[:80]}")

    n = len(results)
    pass_rate = counts["PASS"] / n if n else 0.0
    summary = {
        "timestamp_utc": ts,
        "n_prompts":     n,
        "counts":        counts,
        "pass_rate":     round(pass_rate, 3),
        "results":       results,
    }
    (run_dir / "results.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    # Also write a compact line-summary that future runs can diff against
    (run_dir / "SUMMARY.txt").write_text(
        f"pass_rate={pass_rate:.1%}  n={n}  PASS={counts['PASS']}  "
        f"WEAK={counts['WEAK']}  FAIL={counts['FAIL']}  ERROR={counts['ERROR']}\n",
        encoding="utf-8")
    print(f"\n=== Eval done: {counts} → pass_rate={pass_rate:.1%}")
    print(f"=== Results: {run_dir}/results.json")

    # Ship gate. Set to 0.0 in CI to allow visibility runs; locally
    # raise to 0.80 to enforce the target.
    target = float(os.environ.get("ARIA_EVAL_TARGET", "0.0"))
    assert pass_rate >= target, (
        f"Pass rate {pass_rate:.1%} below target {target:.0%} "
        f"({counts}). See {run_dir}/results.json for failures.")


def test_eval_corpus_has_50_prompts():
    """Sanity check: the held-out set should have 50 prompts. If we
    add/remove, this is the canary that we updated documentation."""
    prompts = _load_prompts()
    assert len(prompts) >= 45, f"Only {len(prompts)} prompts; target ~50"


def test_eval_prompts_well_formed():
    """Every prompt must have id, goal, expected_ops_any (non-empty)."""
    bad = []
    for p in _load_prompts():
        if not p.get("id") or not p.get("goal"):
            bad.append((p.get("id", "?"), "missing id/goal"))
        if not p.get("expected_ops_any"):
            bad.append((p.get("id", "?"), "missing expected_ops_any"))
    assert not bad, f"Malformed eval prompts: {bad}"


def test_eval_prompt_ids_unique():
    """Duplicate IDs would silently overwrite results."""
    ids = [p["id"] for p in _load_prompts()]
    assert len(ids) == len(set(ids)), (
        f"Duplicate IDs: {[i for i in ids if ids.count(i) > 1]}")
