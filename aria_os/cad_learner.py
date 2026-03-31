"""
aria_os/cad_learner.py — Record and retrieve CAD generation learning entries.

Every time a part is successfully generated and validated:
  - The goal, plan text, generated code, bbox, and validation result are saved
  - On future calls, the N most relevant successful examples are retrieved
    as few-shot context for Claude

Learning file: outputs/cad/learning_log.json (up to 500 entries)
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

LEARNING_FILE_REL = "outputs/cad/learning_log.json"
MAX_ENTRIES = 500
FEW_SHOT_N = 3


def _learning_path(repo_root: Optional[Path] = None) -> Path:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    p = repo_root / LEARNING_FILE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def record_attempt(
    goal: str,
    plan_text: str,
    part_id: str,
    code: str,
    passed: bool,
    bbox: Optional[dict] = None,
    error: Optional[str] = None,
    cem_snapshot: Optional[dict] = None,
    cem_passed: bool = False,
    feature_complete: bool = False,
    mesh_clean: bool = False,
    bbox_within_2pct: bool = False,
    tool_used: str = "",
    repo_root: Optional[Path] = None,
):
    """
    Save one generation attempt to the learning log.
    Call this after every validate() result in orchestrator.py — both passing and failing attempts.
    """
    path = _learning_path(repo_root)
    try:
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        entries = []

    quality_score = 0
    if passed:
        quality_score += 40
    if cem_passed:
        quality_score += 20
    if feature_complete:
        quality_score += 20
    if mesh_clean:
        quality_score += 10
    if bbox_within_2pct:
        quality_score += 10

    entry = {
        "timestamp": datetime.now().isoformat(),
        "goal": (goal or "")[:200],
        "part_id": part_id,
        "plan_text": (plan_text or "")[:600],
        "code": (code or "")[:4000],
        "passed": bool(passed),
        "tool_used": tool_used or "cadquery",
        "quality_score": int(quality_score),
        "cem_passed": bool(cem_passed),
        "feature_complete": bool(feature_complete),
        "mesh_clean": bool(mesh_clean),
        "bbox_within_2pct": bool(bbox_within_2pct),
        "bbox": bbox,
        "error": (error or "")[:400],
        "cem_snapshot": {k: v for k, v in (cem_snapshot or {}).items() if k.startswith("output_") or k.startswith("input_")},
    }
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]

    try:
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_few_shot_examples(goal: str, part_id: str = "", repo_root: Optional[Path] = None) -> list:
    """
    Return up to FEW_SHOT_N successful past examples most relevant to this goal/part_id.
    Relevance: exact part_id match first, then keyword overlap with goal.
    """
    path = _learning_path(repo_root)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not entries:
        return []

    goal_words = set(re.findall(r"\w+", (goal or "").lower()))

    def score(entry: dict) -> tuple:
        quality = int(entry.get("quality_score", 0))
        pid_match = 1 if part_id and (entry.get("part_id") == part_id) else 0
        e_words = set(re.findall(r"\w+", (entry.get("goal") or "").lower()))
        overlap = len(goal_words.intersection(e_words))
        recency = entry.get("timestamp", "")
        return (quality, pid_match, overlap, recency)

    ranked = sorted(entries, key=score, reverse=True)[:FEW_SHOT_N]
    return ranked


def format_few_shot_block(examples: list) -> str:
    """Format few-shot examples as a comment block for Claude system prompt injection."""
    if not examples:
        return ""

    lines = ["# === SUCCESSFUL PAST GENERATIONS (few-shot examples) ==="]
    for idx, ex in enumerate(examples, start=1):
        lines.append(f"# Example {idx}: goal='{ex.get('goal','')[:80]}'")
        if ex.get("bbox"):
            lines.append(f"#   bbox: {ex.get('bbox')}")
        lines.append(f"#   part_id: {ex.get('part_id','')}")
        lines.append("# --- Code ---")
        for cl in (ex.get("code", "").split("\n")[:30]):
            lines.append(f"#   {cl}")
        lines.append("# ---")
    lines.append("# === END FEW-SHOT ===")
    return "\n".join(lines)


def get_failure_patterns(part_id: str = "", repo_root: Optional[Path] = None) -> list:
    """
    Return recent failure error messages for a part_id.
    """
    path = _learning_path(repo_root)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    failures = [
        e for e in entries
        if not e.get("passed") and e.get("error")
        and (not part_id or e.get("part_id") == part_id)
    ]
    seen = set()
    unique_errors = []
    for f in reversed(failures):
        err = str(f.get("error", ""))[:200]
        if err and err not in seen:
            seen.add(err)
            unique_errors.append(err)
        if len(unique_errors) >= 5:
            break
    return unique_errors
