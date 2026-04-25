"""Feedback capture — the canary input to the W10 knowledge loop.

Every generated plan can be marked accept/reject by the user. We
persist the decision alongside the run artifacts and expose helpers
the auto-promoter (W10.2), failure miner (W10.3), and SFT exporter
(W10.4) all consume.

Storage layout:
    outputs/feedback/<run_id>.json   — one file per run
    outputs/feedback/INDEX.jsonl     — append-only event log

Schema (feedback file):
    {
        run_id:          str,
        timestamp_utc:   str (ISO 8601),
        goal:            str,
        spec:            dict,
        plan:            list[dict],
        plan_hash:       str (sha256 of canonical-JSON plan),
        decision:        "accept" | "reject" | "needs_revision",
        reason:          str (free-text),
        failed_op_index: int | null,
        user_id:         str | null,
        host:            "fusion" | "rhino" | "onshape" | "dashboard",
    }

The schema is open: extra keys are preserved as-is so future
analytics can attach (e.g. retrieved few-shot IDs, tier landed at,
verifyPart score).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class FeedbackEntry:
    run_id:          str
    goal:            str
    plan:            list[dict]
    decision:        str  # accept | reject | needs_revision
    reason:          str = ""
    spec:            dict = field(default_factory=dict)
    failed_op_index: int | None = None
    user_id:         str | None = None
    host:            str = "dashboard"
    timestamp_utc:   str = ""
    plan_hash:       str = ""
    extras:          dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp_utc:
            self.timestamp_utc = datetime.now(timezone.utc).isoformat()
        if not self.plan_hash:
            self.plan_hash = compute_plan_hash(self.plan)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.extras:
            d.update(self.extras)
            d.pop("extras", None)
        return d


def compute_plan_hash(plan: list[dict]) -> str:
    """Stable hash of the plan ops — used to dedupe across runs.

    Drops cosmetic fields (label, alias auto-generated names) so two
    runs that produced effectively-the-same geometry hash the same."""
    canonical = []
    for op in plan or []:
        if not isinstance(op, dict):
            continue
        params = {k: v for k, v in (op.get("params") or {}).items()
                  if not (isinstance(k, str) and k.startswith("_"))}
        # Drop alias suffixes — auto-numbered names like extrude_3
        # shouldn't break dedup if the underlying geometry is the same.
        params.pop("alias", None)
        canonical.append({"kind": op.get("kind"), "params": params})
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def feedback_dir(repo_root: Path | None = None) -> Path:
    """Resolve the feedback directory; ensure it exists."""
    base = (Path(repo_root) if repo_root else Path.cwd()) \
        / "outputs" / "feedback"
    base.mkdir(parents=True, exist_ok=True)
    return base


def record_feedback(entry: FeedbackEntry,
                      repo_root: Path | None = None) -> Path:
    """Persist a feedback entry. Writes the per-run file AND appends
    to INDEX.jsonl. Returns the per-run file path."""
    if entry.decision not in ("accept", "reject", "needs_revision"):
        raise ValueError(
            f"decision must be accept|reject|needs_revision, "
            f"got {entry.decision!r}")
    base = feedback_dir(repo_root)
    f = base / f"{entry.run_id}.json"
    payload = entry.to_dict()
    f.write_text(json.dumps(payload, indent=2, default=str),
                  encoding="utf-8")
    # Append to index for streaming analytics
    idx = base / "INDEX.jsonl"
    with idx.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "run_id":    entry.run_id,
            "timestamp_utc": entry.timestamp_utc,
            "decision":  entry.decision,
            "plan_hash": entry.plan_hash,
            "host":      entry.host,
        }, default=str) + "\n")
    return f


def load_all_feedback(
        repo_root: Path | None = None) -> list[dict]:
    """Read every <run_id>.json. Returns list-of-dicts (raw, not
    FeedbackEntry — callers may need extra keys)."""
    base = feedback_dir(repo_root)
    out = []
    for f in sorted(base.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def stats(repo_root: Path | None = None) -> dict:
    """Quick aggregate over the feedback log — used by the insights
    dashboard (W10.6) and the weekly audit."""
    entries = load_all_feedback(repo_root)
    counts = {"accept": 0, "reject": 0, "needs_revision": 0}
    by_host = {}
    by_op_failed = {}
    for e in entries:
        d = e.get("decision")
        if d in counts:
            counts[d] += 1
        h = e.get("host") or "unknown"
        by_host[h] = by_host.get(h, 0) + 1
        if e.get("failed_op_index") is not None:
            plan = e.get("plan") or []
            idx = e["failed_op_index"]
            if 0 <= idx < len(plan):
                kind = plan[idx].get("kind") or "?"
                by_op_failed[kind] = by_op_failed.get(kind, 0) + 1
    n = max(1, len(entries))
    return {
        "n_total":      len(entries),
        "counts":       counts,
        "accept_rate":  round(counts["accept"] / n, 3),
        "reject_rate":  round(counts["reject"] / n, 3),
        "by_host":      by_host,
        "by_failed_op": dict(sorted(by_op_failed.items(),
                                       key=lambda kv: -kv[1])[:10]),
    }


__all__ = [
    "FeedbackEntry", "compute_plan_hash",
    "record_feedback", "load_all_feedback", "stats", "feedback_dir",
]
