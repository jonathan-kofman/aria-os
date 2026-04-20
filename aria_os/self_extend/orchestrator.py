"""
Top-level orchestrator for the self-extending engineering pipeline.

Entry point: `run_extension_request(request)` takes a structured request
(user prompt + optional GitHub issue id + target domain mcad/ecad) and
drives the full loop: dispatch → try existing templates → on template
miss, run design-discovery sub-loop → sandbox each candidate → contract
test → physics judge → reviewer → PR.

Every stage is observable via a stream of ExtensionEvent records so the
dashboard can render live progress, and the trust module can audit after
the fact.

Agent topology (ALL sub-agents are Claude Code CLI sub-agent calls; the
orchestrator itself is synchronous Python glue):

  Main (this module)
    ├─ Dispatcher      — classify issue → path (cadquery / sdf / ecad /
    │                    lattice-discovery / general)
    ├─ TemplateMatcher — existing library hit? if yes, route there, done.
    ├─ Hypothesis      — propose N candidate new primitives
    ├─ Implementer(s)  — ONE sub-agent per candidate, in parallel, each
    │                    writes a new module in its own sandboxed worktree
    ├─ ContractTester  — run the generator contract suite against each
    │                    candidate; reject the ones that fail
    ├─ PhysicsJudge    — FEA / DRC / CAMotics on each survivor; rank
    │                    against the stated spec
    ├─ Reviewer        — consolidate the diff + evidence; approve or
    │                    request retry (cap 2 retries per candidate)
    ├─ PRWriter        — package the survivor as a PR
    └─ Trust           — record the new module as QUARANTINED (HITL on
                         first real call)

The orchestrator is deliberately THIN — all reasoning happens in the
Claude Code sub-agents via prompt templates that live next to each stage.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# Request + event types
# --------------------------------------------------------------------------- #

class Domain(str, enum.Enum):
    MCAD = "mcad"
    ECAD = "ecad"
    LATTICE = "lattice"   # sub-domain of MCAD for the SDF kernel
    UNKNOWN = "unknown"


class Stage(str, enum.Enum):
    DISPATCH = "dispatch"
    TEMPLATE_MATCH = "template_match"
    HYPOTHESIS = "hypothesis"
    IMPLEMENT = "implement"
    CONTRACT = "contract"
    PHYSICS = "physics"
    REVIEW = "review"
    PR = "pr"
    TRUST = "trust"


@dataclasses.dataclass
class ExtensionRequest:
    """A single incoming design request — GitHub issue or NL prompt.

    Fields
    ------
    request_id      : unique; used for sandbox names, trust records, PR titles.
    goal            : the English prompt or issue body.
    domain          : MCAD / ECAD / LATTICE / UNKNOWN — dispatcher may upgrade.
    spec            : optional structured spec extracted upstream (dimensions,
                      materials, load case, target metrics).
    github_issue_id : if triggered by webhook, the issue id for PR backref.
    max_candidates  : how many novel candidates to generate if no template hits.
    max_iters       : per-candidate retry cap.
    """
    request_id: str
    goal: str
    domain: Domain = Domain.UNKNOWN
    spec: dict[str, Any] = dataclasses.field(default_factory=dict)
    github_issue_id: int | None = None
    max_candidates: int = 4
    max_iters: int = 2

    @classmethod
    def new(cls, goal: str, **kwargs) -> "ExtensionRequest":
        return cls(request_id=f"ext-{int(time.time())}-{uuid.uuid4().hex[:6]}",
                   goal=goal, **kwargs)


@dataclasses.dataclass
class ExtensionEvent:
    """A single observable step in the pipeline. Emitted to both stdout and
    the event stream consumed by the dashboard."""
    request_id: str
    stage: Stage
    status: str          # "start" / "done" / "fail" / "skip"
    elapsed_s: float
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"request_id": self.request_id,
                "stage": self.stage.value,
                "status": self.status,
                "elapsed_s": round(self.elapsed_s, 2),
                **self.extra}


@dataclasses.dataclass
class ExtensionResult:
    request_id: str
    success: bool
    merged_module: str | None = None      # path of the new module if accepted
    pr_url: str | None = None
    trust_state: str | None = None        # quarantined / review_required / trusted
    error: str | None = None
    events: list[ExtensionEvent] = dataclasses.field(default_factory=list)
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)
    candidates_tried: int = 0
    winner_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "success": self.success,
            "merged_module": self.merged_module,
            "pr_url": self.pr_url,
            "trust_state": self.trust_state,
            "error": self.error,
            "candidates_tried": self.candidates_tried,
            "winner_metrics": self.winner_metrics,
            "artifacts": self.artifacts,
            "n_events": len(self.events),
        }


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

EventHandler = Callable[[ExtensionEvent], None]


def _default_event_handler(event: ExtensionEvent) -> None:
    kw = " ".join(f"{k}={v}" for k, v in event.extra.items())
    print(f"[{event.status:4s}] {event.stage.value:14s} "
          f"{event.elapsed_s:6.1f}s {kw}")


def run_extension_request(req: ExtensionRequest,
                          *,
                          on_event: EventHandler | None = None,
                          dry_run: bool = False) -> ExtensionResult:
    """Top-level loop. Each stage below is its own module; this function
    is the synchronous dispatcher.

    dry_run: skip the Claude Code sub-agent calls (still walks the stages
             so tests can verify the pipeline shape).
    """
    on_event = on_event or _default_event_handler
    result = ExtensionResult(request_id=req.request_id, success=False)
    t0 = time.monotonic()

    def _emit(stage: Stage, status: str, **extra) -> None:
        ev = ExtensionEvent(
            request_id=req.request_id, stage=stage, status=status,
            elapsed_s=time.monotonic() - t0, extra=extra)
        result.events.append(ev)
        try:
            on_event(ev)
        except Exception:
            pass

    # ── Stage 1: dispatch — classify the request ─────────────────────────
    _emit(Stage.DISPATCH, "start")
    try:
        from .dispatcher import classify_request
        req.domain = classify_request(req, dry_run=dry_run)
        _emit(Stage.DISPATCH, "done", domain=req.domain.value)
    except Exception as exc:
        _emit(Stage.DISPATCH, "fail", error=str(exc))
        result.error = f"dispatch: {exc}"
        return result

    # ── Stage 2: template match — does the existing library cover this? ──
    _emit(Stage.TEMPLATE_MATCH, "start")
    try:
        from .dispatcher import try_existing_template
        hit = try_existing_template(req, dry_run=dry_run)
        _emit(Stage.TEMPLATE_MATCH,
              "done" if hit else "skip",
              hit=hit.get("template") if hit else None)
    except Exception as exc:
        _emit(Stage.TEMPLATE_MATCH, "fail", error=str(exc))
        hit = None

    if hit is not None:
        # Happy path: existing template covered the request. No new module
        # written; the downstream build_pipeline will handle rendering.
        result.success = True
        result.merged_module = hit.get("template")
        result.artifacts = hit.get("artifacts", {})
        result.trust_state = "trusted"
        return result

    # ── Stage 3: hypothesis — propose N novel candidates ─────────────────
    _emit(Stage.HYPOTHESIS, "start")
    try:
        from .hypothesis import propose_candidates
        candidates = propose_candidates(req, n=req.max_candidates,
                                        dry_run=dry_run)
        _emit(Stage.HYPOTHESIS, "done", n=len(candidates))
    except Exception as exc:
        _emit(Stage.HYPOTHESIS, "fail", error=str(exc))
        result.error = f"hypothesis: {exc}"
        return result

    result.candidates_tried = len(candidates)

    # ── Stage 4: implement — sandboxed writing of each candidate ─────────
    from .sandbox import Sandbox
    from .contracts import run_contract_suite
    from .physics_judge import judge_candidate

    survivors: list[dict] = []
    for cand in candidates:
        cand_name = cand.get("name") or f"cand-{uuid.uuid4().hex[:6]}"
        _emit(Stage.IMPLEMENT, "start", candidate=cand_name)
        try:
            with Sandbox.open(name=f"{req.request_id}/{cand_name}") as sbx:
                cand_path = sbx.write(cand["module_relpath"], cand["code"])
                _emit(Stage.IMPLEMENT, "done", candidate=cand_name,
                      path=str(cand_path))

                _emit(Stage.CONTRACT, "start", candidate=cand_name)
                contract_ok, contract_report = run_contract_suite(
                    sandbox=sbx, candidate=cand)
                if not contract_ok:
                    _emit(Stage.CONTRACT, "fail", candidate=cand_name,
                          reason=contract_report.get("reason"))
                    continue
                _emit(Stage.CONTRACT, "done", candidate=cand_name)

                _emit(Stage.PHYSICS, "start", candidate=cand_name)
                verdict = judge_candidate(sandbox=sbx, candidate=cand,
                                          spec=req.spec)
                if not verdict.get("passed"):
                    _emit(Stage.PHYSICS, "fail", candidate=cand_name,
                          reason=verdict.get("reason"),
                          score=verdict.get("score"))
                    continue
                _emit(Stage.PHYSICS, "done", candidate=cand_name,
                      score=verdict.get("score"),
                      metrics=verdict.get("metrics", {}))
                survivors.append({"candidate": cand, "verdict": verdict,
                                  "sandbox_worktree": str(sbx.worktree_dir)})
        except Exception as exc:
            _emit(Stage.IMPLEMENT, "fail", candidate=cand_name,
                  error=str(exc))

    if not survivors:
        result.error = "no candidate passed contract + physics gates"
        return result

    # ── Stage 5: review + pick the best ──────────────────────────────────
    _emit(Stage.REVIEW, "start", n_survivors=len(survivors))
    try:
        from .reviewer import pick_best
        best = pick_best(survivors, spec=req.spec, dry_run=dry_run)
        _emit(Stage.REVIEW, "done",
              winner=best["candidate"].get("name"),
              score=best["verdict"].get("score"))
    except Exception as exc:
        _emit(Stage.REVIEW, "fail", error=str(exc))
        result.error = f"review: {exc}"
        return result

    # ── Stage 6: PR — write the survivor back to main via PR ─────────────
    _emit(Stage.PR, "start")
    try:
        from .pr_writer import write_pr
        pr_info = write_pr(best, request=req, dry_run=dry_run)
        _emit(Stage.PR, "done", pr_url=pr_info.get("url"))
        result.pr_url = pr_info.get("url")
    except Exception as exc:
        _emit(Stage.PR, "fail", error=str(exc))
        result.error = f"pr: {exc}"
        return result

    # ── Stage 7: trust — record as QUARANTINED (HITL on first use) ───────
    _emit(Stage.TRUST, "start")
    try:
        from .trust import register_new_module
        trust_state = register_new_module(
            module_path=best["candidate"]["module_relpath"],
            request_id=req.request_id,
            winner_metrics=best["verdict"].get("metrics", {}))
        _emit(Stage.TRUST, "done", state=trust_state)
        result.trust_state = trust_state
    except Exception as exc:
        _emit(Stage.TRUST, "fail", error=str(exc))
        result.trust_state = "unknown"

    result.success = True
    result.merged_module = best["candidate"]["module_relpath"]
    result.winner_metrics = best["verdict"].get("metrics", {})
    return result


# --------------------------------------------------------------------------- #
# CLI entry — for the hackathon demo
# --------------------------------------------------------------------------- #

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Run the self-extending engineering agent on a single "
                    "prompt. Output is a PR if a novel primitive is needed, "
                    "or an immediate build if an existing template hits.")
    parser.add_argument("goal", help="NL description of the part or capability")
    parser.add_argument("--domain", default="unknown",
                        choices=[d.value for d in Domain])
    parser.add_argument("--max-candidates", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true",
                        help="Walk the stages without calling Claude Code")
    parser.add_argument("--json-events", action="store_true",
                        help="Emit events as JSON lines to stdout")
    args = parser.parse_args()

    req = ExtensionRequest.new(
        goal=args.goal, domain=Domain(args.domain),
        max_candidates=args.max_candidates)

    if args.json_events:
        def handler(ev: ExtensionEvent) -> None:
            print(json.dumps(ev.to_dict()), flush=True)
    else:
        handler = _default_event_handler

    result = run_extension_request(req, on_event=handler, dry_run=args.dry_run)
    print("\nRESULT:")
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
