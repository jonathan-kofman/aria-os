"""SFT + DPO data export from feedback log.

`export_sft(repo_root, format='anthropic')` — emits one JSONL file
per accepted (goal, plan) pair, deduped by plan_hash.

`export_dpo(repo_root)` — emits paired preference data: when a
single goal has both accepts AND rejects, the accepted plan is
"chosen" and the rejected one is "rejected". Useful for DPO /
preference modeling.

The system prompt baked into each example is the ENGINEERING +
OP_SCHEMA prompt the live planner uses, so the trained model
inherits the same contract.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _build_system_prompt() -> str:
    """Reproduce the live system prompt the planner uses, so the
    fine-tuned model inherits the same contract."""
    try:
        from aria_os.native_planner.engineering_prompt import (
            ENGINEERING_PRACTICE_PROMPT)
        from aria_os.native_planner.llm_planner import (
            _OPS_SCHEMA, _FEW_SHOT_EXAMPLE)
        return (
            "You are a senior mechanical engineer writing CAD feature "
            "plans for ARIA. Output is a JSON array of feature ops "
            "ONLY — no prose, no markdown fences.\n\n"
            + ENGINEERING_PRACTICE_PROMPT + "\n\n"
            + _OPS_SCHEMA + "\n\n"
            + _FEW_SHOT_EXAMPLE)
    except Exception:
        # Minimal fallback so export still works in CI / sparse envs
        return (
            "You are a senior mechanical engineer writing CAD feature "
            "plans for ARIA. Output is a JSON array of feature ops "
            "ONLY — no prose, no markdown fences.")


def _user_message(goal: str, spec: dict | None) -> str:
    parts = [f"## Part description\n{goal.strip()}"]
    if spec:
        parts.append(
            f"## Parsed spec\n{json.dumps(spec, indent=2, default=str)}")
    parts.append("Produce the JSON feature-op array now.")
    return "\n\n".join(parts)


def _assistant_message(plan: list[dict]) -> str:
    """Plans serialize as JSON arrays — that's the planner's
    contract."""
    return json.dumps(plan, indent=2, default=str)


def _load_feedback(repo_root: Path) -> list[dict]:
    base = (repo_root or Path.cwd()) / "outputs" / "feedback"
    if not base.is_dir():
        return []
    out = []
    for f in sorted(base.glob("*.json")):
        if f.name == "INDEX.jsonl":
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def export_sft(
        repo_root: Path | None = None,
        *,
        format: str = "anthropic",
        out_path: Path | None = None) -> Path:
    """Emit SFT data in the requested format. Returns the output
    path. Dedupes by plan_hash so the same (goal, plan) pair never
    appears twice."""
    if format not in ("anthropic", "openai"):
        raise ValueError("format must be anthropic | openai")
    repo_root = repo_root or Path.cwd()
    entries = _load_feedback(repo_root)
    accepted = [e for e in entries if e.get("decision") == "accept"]

    seen: set[str] = set()
    rows: list[dict] = []
    system = _build_system_prompt()
    for e in accepted:
        h = e.get("plan_hash")
        if h and h in seen:
            continue
        seen.add(h or "")
        user_msg = _user_message(e.get("goal", ""), e.get("spec"))
        assistant_msg = _assistant_message(e.get("plan", []))
        # Both Anthropic + OpenAI use the same chat-format. The
        # difference is the upload tool — but the JSONL is identical
        # for our purposes (system / user / assistant turns).
        rows.append({
            "messages": [
                {"role": "system",    "content": system},
                {"role": "user",      "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ],
        })

    out_dir = repo_root / "outputs" / "training"
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"aria_sft_{format}_{ts}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")
    return out_path


def export_dpo(
        repo_root: Path | None = None,
        *,
        out_path: Path | None = None) -> Path | None:
    """Emit paired DPO data: for each goal that has BOTH an accept
    and a reject in the feedback log, emit
    {prompt, chosen, rejected}. Returns the output path, or None
    if no preference pairs were found."""
    repo_root = repo_root or Path.cwd()
    entries = _load_feedback(repo_root)
    by_goal: dict[str, dict[str, list[dict]]] = {}
    for e in entries:
        goal = e.get("goal", "")
        if not goal:
            continue
        decision = e.get("decision")
        if decision in ("accept", "reject"):
            by_goal.setdefault(goal, {"accept": [], "reject": []})[decision]\
                .append(e)

    pairs: list[dict] = []
    for goal, buckets in by_goal.items():
        if not buckets["accept"] or not buckets["reject"]:
            continue
        # One pair per (accept, reject) combination — usually 1×1
        for chosen in buckets["accept"]:
            for rejected in buckets["reject"]:
                pairs.append({
                    "prompt":   _user_message(goal, chosen.get("spec")),
                    "chosen":   _assistant_message(chosen.get("plan", [])),
                    "rejected": _assistant_message(rejected.get("plan", [])),
                    "metadata": {
                        "chosen_run_id":   chosen.get("run_id"),
                        "rejected_run_id": rejected.get("run_id"),
                        "rejected_reason": rejected.get("reason", ""),
                    },
                })

    if not pairs:
        return None

    out_dir = repo_root / "outputs" / "training"
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"aria_dpo_{ts}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, default=str) + "\n")
    return out_path


__all__ = ["export_sft", "export_dpo"]
