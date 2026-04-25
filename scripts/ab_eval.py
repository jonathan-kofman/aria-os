"""A/B prompt-variant evaluation harness.

Run the same 50-prompt set (tests/eval_prompts.json) against TWO
prompt configurations and report the per-prompt + aggregate delta.

Variants are selected by env var:
    ARIA_PROMPT_VARIANT=control | candidate

Each variant maps to a `(env vars to set, name)` tuple — typically:
    control   = production prompt (no env overrides)
    candidate = lean=True OR a custom system_prompt path

Usage:
    python scripts/ab_eval.py [--prompts N] [--out outputs/eval/ab/]

Output: one results.json per variant + a comparison.json that
lists per-prompt deltas + an aggregate pass-rate change. The CI
gate (W11.6) blocks merges when candidate underperforms control
by > 5%.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Variant definitions: (label, env_overrides). Add more variants by
# extending this list — the harness will iterate over each.
VARIANTS = [
    ("control",   {}),  # Whatever the production planner does
    ("candidate", {"ARIA_LEAN_PROMPT": "1"}),
]


def _run_one_variant(variant_label: str, env_overrides: dict,
                       prompts: list[dict],
                       *, pace_sec: float,
                       out_dir: Path) -> dict:
    """Run the planner against every prompt with `env_overrides`
    set. Returns the same shape as test_llm_success_rate.py
    (counts + per-prompt results)."""
    from aria_os.native_planner.dispatcher import make_plan
    from aria_os.native_planner.validator import validate_plan

    # Apply env overrides for this variant only
    saved_env = {}
    for k, v in env_overrides.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        results = []
        counts = {"PASS": 0, "WEAK": 0, "FAIL": 0, "ERROR": 0}
        last_t = None
        for i, p in enumerate(prompts, 1):
            if last_t is not None:
                wait = pace_sec - (time.time() - last_t)
                if wait > 0:
                    time.sleep(wait)
            last_t = time.time()
            t0 = time.time()
            try:
                plan = make_plan(p["goal"], {}, prefer_llm=True,
                                   quality="balanced")
                ok, issues = validate_plan(plan)
                kinds = [op.get("kind") for op in plan]
                hit = [k for k in (p.get("expected_ops_any") or [])
                        if k in kinds]
                outcome = "PASS" if (ok and hit) else (
                    "WEAK" if ok else "FAIL")
                counts[outcome] += 1
                results.append({
                    "id": p["id"], "outcome": outcome,
                    "elapsed_s": round(time.time() - t0, 1),
                    "kinds": kinds[:14], "hits": hit,
                    "issues": issues[:3] if not ok else [],
                })
            except Exception as exc:
                counts["ERROR"] += 1
                results.append({
                    "id": p["id"], "outcome": "ERROR",
                    "elapsed_s": round(time.time() - t0, 1),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                })
            print(f"[{variant_label}][{i:2d}/{len(prompts)}] "
                   f"{results[-1]['outcome']} {p['id']}", flush=True)
    finally:
        # Restore env
        for k, prev in saved_env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev

    n = max(1, len(results))
    pass_rate = counts["PASS"] / n
    summary = {
        "variant": variant_label,
        "env_overrides": env_overrides,
        "n_prompts": n,
        "counts": counts,
        "pass_rate": round(pass_rate, 3),
        "results": results,
    }
    out_path = out_dir / f"{variant_label}.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str),
                          encoding="utf-8")
    return summary


def _compare(control: dict, candidate: dict) -> dict:
    """Per-prompt + aggregate diff between two variants."""
    by_id_c = {r["id"]: r for r in control["results"]}
    by_id_n = {r["id"]: r for r in candidate["results"]}
    regressions = []
    improvements = []
    for pid, c in by_id_c.items():
        n = by_id_n.get(pid)
        if not n:
            continue
        if c["outcome"] == "PASS" and n["outcome"] != "PASS":
            regressions.append({
                "id": pid,
                "control": c["outcome"],
                "candidate": n["outcome"],
                "candidate_issues":
                    n.get("issues") or n.get("error", "")[:120],
            })
        elif c["outcome"] != "PASS" and n["outcome"] == "PASS":
            improvements.append({
                "id": pid,
                "control": c["outcome"],
                "candidate": n["outcome"],
            })
    delta = candidate["pass_rate"] - control["pass_rate"]
    return {
        "control_label":   control["variant"],
        "candidate_label": candidate["variant"],
        "control_pass_rate":   control["pass_rate"],
        "candidate_pass_rate": candidate["pass_rate"],
        "delta_pp":            round(delta * 100, 1),
        "n_regressions":       len(regressions),
        "n_improvements":      len(improvements),
        "regressions":         regressions,
        "improvements":        improvements,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prompts", type=int, default=0,
                    help="Limit prompt count (0=all). Useful for quick smoke runs.")
    p.add_argument("--pace", type=float, default=2.5,
                    help="Seconds between requests (Groq RPM compliance).")
    p.add_argument("--out", default="outputs/eval/ab")
    args = p.parse_args()

    eval_set = json.loads(
        (REPO_ROOT / "tests" / "eval_prompts.json").read_text())
    prompts = eval_set["prompts"]
    if args.prompts:
        prompts = prompts[:args.prompts]
    print(f"Running A/B over {len(prompts)} prompts × "
           f"{len(VARIANTS)} variants...")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = REPO_ROOT / args.out / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for label, overrides in VARIANTS:
        results[label] = _run_one_variant(
            label, overrides, prompts,
            pace_sec=args.pace, out_dir=out_dir)

    if "control" in results and "candidate" in results:
        comparison = _compare(results["control"], results["candidate"])
        (out_dir / "comparison.json").write_text(
            json.dumps(comparison, indent=2, default=str),
            encoding="utf-8")
        print()
        print(f"=== A/B summary ({ts}) ===")
        print(f"  control  : {comparison['control_pass_rate']:.1%}")
        print(f"  candidate: {comparison['candidate_pass_rate']:.1%}")
        print(f"  delta    : {comparison['delta_pp']:+.1f} pp")
        print(f"  regressions:  {comparison['n_regressions']}")
        print(f"  improvements: {comparison['n_improvements']}")
        if comparison["regressions"]:
            print()
            print("  Regressions:")
            for r in comparison["regressions"][:5]:
                print(f"    - {r['id']}: {r['control']} → {r['candidate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
