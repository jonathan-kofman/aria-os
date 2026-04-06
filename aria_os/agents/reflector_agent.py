"""Reflector agent — recovery when agents get stuck or produce no useful output.

Ported from PentAGI's performReflector pattern (backend/pkg/providers/performer.go).
When an agent fails to make progress, the Reflector analyzes what went wrong and
injects corrective guidance back into the agent's context.
"""
from __future__ import annotations

from .base_agent import BaseAgent
from .design_state import DesignState
from .ollama_config import AGENT_MODELS

MAX_REFLECTOR_CALLS = 3  # prevent infinite recursion


REFLECTOR_SYSTEM_PROMPT = """\
You are a senior engineering supervisor reviewing a design agent's work.
An agent in the design pipeline has gotten stuck or is producing poor results.

Your job:
1. Diagnose WHY the agent is failing (bad constraints? wrong approach? missing info?)
2. Provide SPECIFIC, ACTIONABLE guidance to get the agent unstuck
3. If the approach is fundamentally wrong, suggest an alternative strategy

Be concise. Give the agent a clear next step, not a lecture.
Do NOT generate code yourself — tell the agent what to change.
"""


class ReflectorAgent(BaseAgent):
    """
    Recovery agent invoked when design agents get stuck.

    Mirrors PentAGI's Reflector: analyzes failures, injects corrective
    guidance as a human message, then retries the original agent.
    """

    def __init__(self):
        super().__init__(
            name="reflector",
            system_prompt=REFLECTOR_SYSTEM_PROMPT,
            model=AGENT_MODELS.get("refiner", "qwen2.5-coder:7b"),
            max_context_tokens=4000,
        )

    def reflect(self, state: DesignState, agent_name: str, agent_output: str) -> str:
        """
        Analyze a stuck agent's output and return corrective guidance.

        Args:
            state: Current design state (for context)
            agent_name: Which agent is stuck (e.g., "designer", "refiner")
            agent_output: The agent's last response or error

        Returns:
            Corrective guidance string to inject into the agent's context.
        """
        context = self._build_context(state, agent_name, agent_output)
        advice = self._call_llm(context)
        if not advice:
            return self._fallback_advice(state, agent_name)
        return advice

    def _build_context(self, state: DesignState, agent_name: str, agent_output: str) -> str:
        parts = [
            f"## Stuck Agent: {agent_name}",
            f"## Goal: {state.goal}",
            f"## Domain: {state.domain}",
            f"## Iteration: {state.iteration}/{state.max_iterations}",
        ]

        if state.spec:
            parts.append(f"## Spec: {_truncate(str(state.spec), 500)}")

        if state.failures:
            parts.append(f"## Recent failures:\n" + "\n".join(f"  - {f}" for f in state.failures[:5]))

        if state.refinement_instructions:
            parts.append(f"## Last refinement instructions:\n{_truncate(state.refinement_instructions, 300)}")

        parts.append(f"## Agent output:\n{_truncate(agent_output, 1000)}")
        parts.append("\nWhat should the agent do differently? Be specific.")

        return "\n\n".join(parts)

    def _fallback_advice(self, state: DesignState, agent_name: str) -> str:
        """Deterministic fallback when LLM is unavailable."""
        if agent_name == "designer" and state.failures:
            return (
                "The design approach isn't working. Try a simpler geometry strategy: "
                "start with a basic box/cylinder, then add features one at a time. "
                "Focus on fixing: " + state.failures[0]
            )
        if agent_name == "refiner":
            return (
                "The refinement instructions aren't helping. Instead of incremental fixes, "
                "try regenerating the design from scratch with tighter constraints."
            )
        return "Try a fundamentally different approach. The current strategy has stalled."


class RepeatingDetector:
    """
    Detects when an agent is making the same failing attempt repeatedly.

    Ported from PentAGI's repeatingDetector (performer.go).
    Tracks agent outputs and detects when they stop changing meaningfully.
    """

    def __init__(self, soft_threshold: int = 3, hard_threshold: int = 7):
        self.soft_threshold = soft_threshold
        self.hard_threshold = hard_threshold
        self.history: list[str] = []

    def detect(self, signature: str) -> str | None:
        """
        Check if the given signature (normalized agent output) is repeating.

        Args:
            signature: A normalized representation of the agent's action
                       (e.g., hash of generated code, or failure pattern)

        Returns:
            None if not repeating, "soft" or "hard" if threshold exceeded.
        """
        if not self.history:
            self.history.append(signature)
            return None

        if self.history[-1] != signature:
            # Different output — reset
            self.history = [signature]
            return None

        # Same as last — accumulate
        self.history.append(signature)

        if len(self.history) >= self.hard_threshold:
            return "hard"
        if len(self.history) >= self.soft_threshold:
            return "soft"
        return None

    def reset(self):
        self.history.clear()


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
