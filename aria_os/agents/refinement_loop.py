"""Autonomous refinement loop — coordinates agents until design converges."""
from __future__ import annotations

from pathlib import Path

from .design_state import DesignState
from .research_agent import ResearchAgent
from .spec_agent import SpecAgent
from .designer_agent import DesignerAgent
from .eval_agent import EvalAgent
from .refiner_agent import RefinerAgent
from .domains import make_tools, detect_domain

STALL_LIMIT = 5  # stop after N iterations with no improvement


def run_agent_loop(state: DesignState) -> DesignState:
    """
    Run the autonomous design refinement loop.

    Flow:
        1. SpecAgent    -> extract constraints
        2. DesignerAgent -> generate code
        3. EvalAgent     -> validate
        4. RefinerAgent  -> propose fixes (if eval failed)
        5. Loop back to 2 (max state.max_iterations, stall after STALL_LIMIT)

    Returns the state with best attempt loaded.
    """
    from .. import event_bus

    # Auto-detect domain if not set
    if not state.domain:
        state.domain = detect_domain(state.goal)

    print(f"\n{'=' * 64}")
    print(f"  AGENT LOOP — domain: {state.domain}, max iterations: {state.max_iterations}")
    print(f"{'=' * 64}")

    # Build tools for this domain
    tools = make_tools(state.domain, state.repo_root)

    # Initialize agents
    researcher = ResearchAgent()
    spec_agent = SpecAgent(state.repo_root, tools=tools)
    designer   = DesignerAgent(state.domain, state.repo_root, tools=tools)
    evaluator  = EvalAgent(state.domain, state.repo_root)
    refiner    = RefinerAgent(state.repo_root)

    # Phase 0: Web research — gather real-world specs and design references
    print(f"\n  [research] Gathering reference information from the web...")
    try:
        researcher.research(state)
        event_bus.emit("agent", "ResearchAgent done", {
            "has_context": bool(state.plan.get("research_context"))})
    except Exception as _re:
        print(f"  [research] Skipped: {_re}")

    # Phase 1: Spec extraction (runs once)
    print(f"\n  [iter 0] SpecAgent extracting constraints...")
    spec_agent.extract(state)
    event_bus.emit("agent", "SpecAgent done", {"spec": state.spec})

    # Phase 2-N: Design → Eval → Refine loop
    for iteration in range(1, state.max_iterations + 1):
        state.iteration = iteration

        # Design
        print(f"\n  [iter {iteration}/{state.max_iterations}] DesignerAgent generating...")
        designer.generate(state)

        if state.generation_error and not state.output_path:
            print(f"  [iter {iteration}] Generation failed: {state.generation_error}")
            state.failures = [f"generation_error: {state.generation_error}"]
            state.eval_passed = False
            state.record_iteration()

            if state.stall_counter >= STALL_LIMIT:
                print(f"  [STALL] No improvement for {STALL_LIMIT} iterations — stopping")
                state.budget_exhausted = True
                break

            # Refine and retry
            refiner.refine(state)
            continue

        # Evaluate
        print(f"  [iter {iteration}] EvalAgent validating...")
        evaluator.evaluate(state)
        state.record_iteration()

        event_bus.emit("agent", f"Iteration {iteration}", {
            "passed": state.eval_passed,
            "failures": len(state.failures),
            "best": state.best_failure_count,
        })

        # Check convergence
        if state.converged:
            print(f"\n  [CONVERGED] All checks passed on iteration {iteration}")
            break

        if state.stall_counter >= STALL_LIMIT:
            print(f"\n  [STALL] No improvement for {STALL_LIMIT} iterations — stopping")
            state.budget_exhausted = True
            break

        # Refine for next iteration
        print(f"  [iter {iteration}] RefinerAgent analyzing {len(state.failures)} failures...")
        refiner.refine(state)

    # Print summary
    _print_summary(state)

    return state


def _print_summary(state: DesignState) -> None:
    """Print the agent loop summary."""
    print(f"\n{'=' * 64}")
    print(f"  AGENT LOOP SUMMARY — {state.domain}")
    print(f"{'=' * 64}")

    status = "CONVERGED" if state.converged else (
        "STALLED" if state.budget_exhausted and state.stall_counter >= STALL_LIMIT else
        "BUDGET EXHAUSTED"
    )
    print(f"  Status:     {status}")
    print(f"  Iterations: {state.iteration}/{state.max_iterations}")
    print(f"  Best iter:  {state.best_iteration} ({state.best_failure_count} failures)")

    if state.converged:
        print(f"  Result:     ALL CHECKS PASSED")
    else:
        print(f"  Remaining failures ({len(state.failures)}):")
        for f in state.failures:
            print(f"    - {f}")

    # History table
    if state.history:
        print(f"\n  Iteration history:")
        print(f"  {'iter':>4s}  {'failures':>8s}  {'notes'}")
        print(f"  {'----':>4s}  {'--------':>8s}  {'-----'}")
        for h in state.history:
            notes = "PASS" if h["eval_passed"] else (
                h.get("generation_error", "")[:40] or
                "; ".join(h.get("failures", [])[:2])[:60]
            )
            marker = " <-- best" if h["iteration"] == state.best_iteration else ""
            print(f"  {h['iteration']:4d}  {h['failure_count']:8d}  {notes}{marker}")

    print(f"{'=' * 64}\n")
