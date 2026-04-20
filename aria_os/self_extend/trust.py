"""
Guardrail 4 — Trust tier / human-in-the-loop.

Every module written by the self-extension agent enters the library in
the QUARANTINED state. The first N times it's invoked on a real (non-test)
job, the orchestrator halts and flags it for human review. After N
successful runs with acceptable sim-to-measurement agreement, the module
is auto-promoted to TRUSTED and invoked without halting.

State is persisted to `outputs/.trust/module_trust.json` so it survives
across runs (and across demo / judging sessions).

API:
    register_new_module(module_path, request_id, winner_metrics)
        # called by orchestrator after a fresh PR is opened
    check_before_use(module_path) -> TrustVerdict
        # called by build_pipeline before invoking a newly-discovered module
    record_run(module_path, *, outcome, measured_metrics=None)
        # called after each real-world invocation
    promote_if_eligible(module_path)
        # called after record_run — bumps to TRUSTED when criteria met
"""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import time
from pathlib import Path


class TrustState(str, enum.Enum):
    QUARANTINED     = "quarantined"      # fresh from agent — no real-world uses
    REVIEW_REQUIRED = "review_required"  # at least one real call queued for human
    TRUSTED         = "trusted"          # auto-callable


@dataclasses.dataclass
class TrustRecord:
    module_path: str
    state: TrustState
    request_id: str
    winner_metrics: dict
    successful_runs: int = 0
    failed_runs: int = 0
    history: list[dict] = dataclasses.field(default_factory=list)
    created_at: float = dataclasses.field(default_factory=time.time)
    last_updated_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TrustRecord":
        d = dict(d)
        d["state"] = TrustState(d["state"])
        return cls(**d)


@dataclasses.dataclass
class TrustVerdict:
    allowed: bool
    state: TrustState
    reason: str
    record: TrustRecord | None = None


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_STORE = _REPO_ROOT / "outputs" / ".trust" / "module_trust.json"

# Minimum consecutive successful runs (with sim-to-measurement agreement)
# required to auto-promote QUARANTINED → TRUSTED.
_PROMOTE_THRESHOLD = int(os.environ.get("ARIA_TRUST_PROMOTE_N", "3"))


def _store_path() -> Path:
    override = os.environ.get("ARIA_TRUST_STORE")
    return Path(override) if override else _DEFAULT_STORE


def _load_store() -> dict[str, TrustRecord]:
    p = _store_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {k: TrustRecord.from_dict(v) for k, v in raw.items()}


def _save_store(store: dict[str, TrustRecord]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v.to_dict() for k, v in store.items()}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def register_new_module(*, module_path: str, request_id: str,
                         winner_metrics: dict) -> str:
    """Record a newly-written module as QUARANTINED. Returns the state."""
    store = _load_store()
    store[module_path] = TrustRecord(
        module_path=module_path, state=TrustState.QUARANTINED,
        request_id=request_id, winner_metrics=winner_metrics)
    _save_store(store)
    return TrustState.QUARANTINED.value


def check_before_use(module_path: str) -> TrustVerdict:
    """Consult the trust store before letting the build_pipeline call a
    newly-discovered module. A TRUSTED module runs freely; QUARANTINED
    requires approval (orchestrator halts and asks for human).
    """
    store = _load_store()
    rec = store.get(module_path)
    if rec is None:
        # Not in the store → not self-extension-generated; allow freely.
        return TrustVerdict(
            allowed=True, state=TrustState.TRUSTED,
            reason="module not in trust registry (probably hand-authored)")
    if rec.state == TrustState.TRUSTED:
        return TrustVerdict(allowed=True, state=rec.state,
                             reason="trusted", record=rec)
    return TrustVerdict(
        allowed=False, state=rec.state,
        reason=(f"module {module_path} is {rec.state.value}; "
                f"human review required before first real invocation"),
        record=rec)


def record_run(module_path: str, *, outcome: str,
                measured_metrics: dict | None = None) -> TrustRecord | None:
    """Log a real-world invocation. outcome in {'success', 'fail', 'skip'}.

    measured_metrics is the post-hoc sim-vs-measurement comparison;
    downstream promotion criteria read it.
    """
    store = _load_store()
    rec = store.get(module_path)
    if rec is None:
        return None
    entry = {"ts": time.time(), "outcome": outcome,
             "measured_metrics": measured_metrics or {}}
    rec.history.append(entry)
    if outcome == "success":
        rec.successful_runs += 1
    elif outcome == "fail":
        rec.failed_runs += 1
    rec.last_updated_at = time.time()
    store[module_path] = rec
    _save_store(store)
    return rec


def promote_if_eligible(module_path: str) -> TrustState:
    """Check promotion criteria and bump state if met. Returns new state."""
    store = _load_store()
    rec = store.get(module_path)
    if rec is None:
        return TrustState.TRUSTED
    if rec.state == TrustState.TRUSTED:
        return rec.state
    if rec.successful_runs >= _PROMOTE_THRESHOLD and rec.failed_runs == 0:
        rec.state = TrustState.TRUSTED
    elif rec.successful_runs > 0:
        rec.state = TrustState.REVIEW_REQUIRED
    rec.last_updated_at = time.time()
    store[module_path] = rec
    _save_store(store)
    return rec.state


def approve_module(module_path: str) -> TrustState:
    """Human override — promote to TRUSTED regardless of run history.
    Used by the HITL dashboard button."""
    store = _load_store()
    rec = store.get(module_path)
    if rec is None:
        return TrustState.TRUSTED
    rec.state = TrustState.TRUSTED
    rec.last_updated_at = time.time()
    store[module_path] = rec
    _save_store(store)
    return rec.state


def list_all() -> list[TrustRecord]:
    return list(_load_store().values())
