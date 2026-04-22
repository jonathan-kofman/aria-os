"""Native-plan dispatcher.

Given a goal + spec, return an ordered feature-op plan. Prefers hardcoded
planners for known part types (fast, deterministic, dimensionally stable)
and falls back to LLM generation for arbitrary parts.

Flow:
    1. Keyword-match against hardcoded planners (flange, …)
    2. If no match, call `llm_planner.plan_from_llm`
    3. Validate the plan structurally
    4. If invalid, retry the LLM with the validation issues as feedback
       (one retry only — keep credit use bounded)
    5. If still invalid, raise ValueError so the caller can surface the
       failure to the user instead of streaming a broken plan into Fusion
"""
from __future__ import annotations

from pathlib import Path

from .flange_planner import plan_flange
from .impeller_planner import plan_impeller
from .llm_planner import plan_from_llm
from .sheetmetal_planner import plan_simple_bracket
from .validator import validate_plan


# Keyword → planner fn. First match wins.
# Order matters: put multi-word matches BEFORE single-word ones.
_KEYWORD_TO_PLANNER = [
    (["impeller", "fan rotor", "centrifugal rotor",
      "turbine rotor", "blower wheel"],                       plan_impeller),
    (["flange"],                                              plan_flange),
    (["sheet metal bracket", "l-bracket", "formed bracket",
      "sheet-metal bracket"],                                 plan_simple_bracket),
]


def make_plan(goal: str, spec: dict | None = None,
              *, quality: str = "balanced",
              repo_root: Path | None = None,
              allow_llm: bool = True,
              host_context: dict | None = None,
              mode: str = "new",
              prefer_llm: bool = False) -> list[dict]:
    """Pick the right planner and return a validated ops list.

    Default behaviour: **hardcoded for known parts** (flange, impeller,
    sheet-metal bracket) because they're instant and reliable. The LLM
    handles the long tail — anything the hardcoded catalogue doesn't
    cover. Engineering conventions (ISO 273, material minima, etc.)
    live in the LLM system prompt so the LLM handles those cases at
    quality too.

    Set `prefer_llm=True` to force the LLM path even when a hardcoded
    planner would match (useful for evaluating LLM quality against
    known-good baselines).

    Args:
        goal: the user's prompt text
        spec: parsed spec dict (from `spec_extractor.extract_spec`)
        quality: LLM tier (fast|balanced|premium)
        repo_root: repo root (for .env loading)
        allow_llm: if False, hardcoded-only (raises on unsupported part)
        prefer_llm: force LLM path even when hardcoded matches
    """
    g = (goal or "").lower()
    spec = spec or {}

    import inspect as _inspect

    def _try_hardcoded():
        for keywords, fn in _KEYWORD_TO_PLANNER:
            if any(k in g for k in keywords):
                sig = _inspect.signature(fn)
                plan = fn(spec, goal=goal) if "goal" in sig.parameters else fn(spec)
                ok, issues = validate_plan(plan)
                if not ok:
                    raise ValueError(
                        f"Hardcoded planner emitted invalid plan: {issues}")
                return plan
        return None

    # --- Fast path: hardcoded planner for known parts ---
    if not prefer_llm:
        plan = _try_hardcoded()
        if plan is not None:
            return plan

    if not allow_llm:
        raise NotImplementedError(
            f"No hardcoded planner for goal: {goal!r} and allow_llm=False.")

    # --- LLM-FIRST path with TIER ESCALATION ---
    # The LLM knows engineering conventions via the system prompt and
    # handles arbitrary parts. On parse/validate failure, auto-escalate
    # through fast → balanced → premium. Hardcoded planners are only
    # used as a last-resort safety net.
    tier_order = ["fast", "balanced", "premium"]
    start_idx = {"fast": 0, "balanced": 1, "premium": 2}.get(quality, 1)
    last_issues: list[str] = []
    last_error: str | None = None

    for attempt, tier in enumerate(tier_order[start_idx:], start=1):
        try:
            # Feed the PREVIOUS attempt's issues as correction context on
            # retry. This is the same pattern the RefinerAgent uses for
            # visual-verify failures.
            attempt_goal = goal
            attempt_spec = dict(spec)
            if last_issues:
                attempt_goal = (
                    f"{goal}\n\n"
                    "## Previous attempt had issues — fix them:\n"
                    + "\n".join(f"  - {i}" for i in last_issues[:8])
                )
                attempt_spec["__previous_issues"] = last_issues[:8]
            elif last_error:
                attempt_goal = (
                    f"{goal}\n\n"
                    f"## Previous attempt failed to produce parseable JSON: {last_error}"
                )
            plan = plan_from_llm(
                attempt_goal, attempt_spec,
                quality=tier, repo_root=repo_root,
                host_context=host_context, mode=mode)
        except ValueError as exc:
            # LLM returned unparseable junk — escalate tier
            last_error = str(exc)[:200]
            last_issues = []
            continue

        ok, issues = validate_plan(plan)
        if ok:
            return plan
        last_issues = issues
        last_error = None

    # Exhausted all tiers — fall back to hardcoded as a safety net
    hard = _try_hardcoded()
    if hard is not None:
        return hard
    raise ValueError(
        f"LLM planner exhausted all tiers (fast/balanced/premium) and "
        f"no hardcoded planner matches goal {goal!r}. "
        f"Last issues: {last_issues[:3]}; last parse error: {last_error}")


def is_supported(goal: str) -> bool:
    """True if a hardcoded planner handles this goal. False means the
    LLM fallback will be used when `make_plan` is called."""
    g = (goal or "").lower()
    return any(any(k in g for k in keywords)
                for keywords, _ in _KEYWORD_TO_PLANNER)
