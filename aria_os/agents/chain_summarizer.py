"""Chain summarizer — compresses agent conversation history to fit context windows.

Ported from PentAGI's csum package (backend/pkg/csum/).
Prevents context overflow for small local models (7b/14b) during long refinement loops.

PentAGI's approach:
  1. Parse conversation into sections (system+human header + AI+tool body pairs)
  2. Summarize old sections, preserve recent ones
  3. Three tiers: section summarization, last-section rotation, QA collapse

Simplified for ARIA's architecture:
  - No streaming, no tool call IDs, no provider-specific reasoning signatures
  - Works with DesignState history instead of message chains
  - Focuses on compressing iteration history into actionable context
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .base_agent import BaseAgent, _call_ollama
from .ollama_config import AGENT_MODELS


@dataclass
class SummarizerConfig:
    """Configuration for chain summarization."""
    max_context_chars: int = 16000      # ~4000 tokens for 7b models
    preserve_last_n: int = 2            # always keep last N iterations verbatim
    max_history_chars: int = 4000       # max chars for compressed history section
    summarize_after: int = 4            # start summarizing after N iterations


SUMMARIZER_SYSTEM_PROMPT = """\
You are a technical summarizer for a CAD design refinement pipeline.
Compress the given iteration history into a dense, actionable summary.

Preserve:
- What geometry approaches were tried
- Which specific failures occurred and their root causes
- What fixes worked vs. didn't work
- The current best approach and why

Output format: A single paragraph of dense technical notes. No headers, no bullets.
Max 200 words.
"""


class ChainSummarizer:
    """
    Compresses refinement loop history to fit within context windows.

    Modeled after PentAGI's three-tier summarization:
    - Tier 1: Summarize old iterations into a compressed block
    - Tier 2: Preserve recent iterations verbatim
    - Tier 3: If still too large, re-summarize the summary

    Integration: Called by the refinement loop between iterations to keep
    the designer/refiner agents' context windows manageable.
    """

    def __init__(self, config: SummarizerConfig | None = None):
        self.config = config or SummarizerConfig()
        self._cached_summary: str = ""
        self._summarized_up_to: int = 0  # last iteration index we've summarized

    def build_context(self, history: list[dict[str, Any]], current_iteration: int) -> str:
        """
        Build a compressed history context string for agent prompts.

        Args:
            history: List of iteration snapshots from DesignState.history
            current_iteration: Current iteration number

        Returns:
            Compressed history string that fits within max_context_chars.
        """
        if not history:
            return ""

        n_total = len(history)
        preserve_n = min(self.config.preserve_last_n, n_total)

        # If few enough iterations, return verbatim
        if n_total <= self.config.summarize_after:
            return self._format_iterations(history)

        # Split: old iterations to summarize, recent to preserve
        old = history[:n_total - preserve_n]
        recent = history[n_total - preserve_n:]

        # Tier 1: Summarize old iterations
        old_summary = self._summarize_old(old)

        # Tier 2: Format recent iterations verbatim
        recent_text = self._format_iterations(recent)

        # Combine
        combined = f"## Prior iterations (summarized):\n{old_summary}\n\n## Recent iterations:\n{recent_text}"

        # Tier 3: If still too large, truncate the summary
        if len(combined) > self.config.max_context_chars:
            available = self.config.max_context_chars - len(recent_text) - 100
            if available > 200:
                old_summary = old_summary[:available] + "..."
                combined = f"## Prior iterations (summarized):\n{old_summary}\n\n## Recent iterations:\n{recent_text}"
            else:
                # Last resort: only recent
                combined = f"## Recent iterations:\n{recent_text}"

        return combined

    def _summarize_old(self, iterations: list[dict[str, Any]]) -> str:
        """Summarize old iterations, using cached summary when possible."""
        if not iterations:
            return ""

        # Check if we can reuse cached summary
        last_idx = iterations[-1].get("iteration", 0)
        if last_idx <= self._summarized_up_to and self._cached_summary:
            return self._cached_summary

        # Build text to summarize
        raw = self._format_iterations(iterations)

        # If small enough, don't bother with LLM
        if len(raw) <= self.config.max_history_chars:
            return raw

        # Use LLM to compress
        prompt = f"Summarize this design iteration history:\n\n{raw}"
        summary = _call_ollama(
            prompt,
            SUMMARIZER_SYSTEM_PROMPT,
            AGENT_MODELS.get("refiner", "qwen2.5-coder:7b"),
        )

        if summary:
            self._cached_summary = summary
            self._summarized_up_to = last_idx
            return summary

        # Fallback: deterministic compression
        return self._deterministic_compress(iterations)

    def _format_iterations(self, iterations: list[dict[str, Any]]) -> str:
        """Format iteration snapshots as readable text."""
        lines = []
        for snap in iterations:
            it = snap.get("iteration", "?")
            passed = snap.get("eval_passed", False)
            failures = snap.get("failures", [])
            n_fail = snap.get("failure_count", len(failures))
            instructions = snap.get("refinement_instructions", "")
            gen_error = snap.get("generation_error", "")

            status = "PASS" if passed else f"{n_fail} failures"
            line = f"Iter {it}: {status}"

            if gen_error:
                line += f" | gen_error: {gen_error[:80]}"
            elif failures:
                line += f" | {'; '.join(f[:60] for f in failures[:3])}"
            if instructions:
                line += f" | fix: {instructions[:80]}"

            lines.append(line)
        return "\n".join(lines)

    def _deterministic_compress(self, iterations: list[dict[str, Any]]) -> str:
        """Compress without LLM — extract key patterns."""
        # Track failure frequency
        failure_counts: dict[str, int] = {}
        approaches_tried: list[str] = []

        for snap in iterations:
            for f in snap.get("failures", []):
                key = f[:40]
                failure_counts[key] = failure_counts.get(key, 0) + 1
            if snap.get("refinement_instructions"):
                approaches_tried.append(snap["refinement_instructions"][:60])

        # Build compressed summary
        parts = []
        if failure_counts:
            top_failures = sorted(failure_counts.items(), key=lambda x: -x[1])[:5]
            parts.append("Common failures: " + "; ".join(
                f"{k} (x{v})" for k, v in top_failures
            ))
        if approaches_tried:
            parts.append("Approaches tried: " + "; ".join(approaches_tried[-5:]))

        parts.append(f"Iterations summarized: {len(iterations)}")
        return " | ".join(parts)
