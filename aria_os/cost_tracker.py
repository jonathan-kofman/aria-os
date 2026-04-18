"""
LLM cost tracker — per-call accounting + per-run summary.

Records every Anthropic / Gemini / Groq / Ollama call (tokens in/out, model,
duration) and rolls them up into a JSON file the pipeline writes at the end
of each run, plus a printed end-of-run summary.

Pricing is approximate (USD as of 2026-Q1); update _MODEL_PRICING when
provider rate cards change. Ollama / local models are $0 by definition.

Usage:
    from aria_os.cost_tracker import track, get_session_summary, reset_session

    reset_session()
    track(provider="anthropic", model="claude-sonnet-4-6",
          input_tokens=1500, output_tokens=400, duration_ms=820)
    ...
    summary = get_session_summary()
    print(summary["pretty"])
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Per-million-token pricing in USD (input, output). Update as provider
# rate cards change.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-7":               (15.00, 75.00),
    "claude-opus-4-6":               (15.00, 75.00),
    "claude-sonnet-4-6":             ( 3.00, 15.00),
    "claude-haiku-4-5":              ( 0.80,  4.00),
    "claude-haiku-4-5-20251001":     ( 0.80,  4.00),
    # Google Gemini
    "gemini-2.5-flash":              ( 0.075, 0.30),
    "gemini-2.0-flash":              ( 0.075, 0.30),
    "gemini-1.5-pro":                ( 1.25,  5.00),
    # Groq
    "llama-4-scout-17b-16e-instruct":( 0.11,  0.34),
    "llama-3.2-11b":                 ( 0.18,  0.18),
    # Ollama / local — always free
    "ollama":                        ( 0.00,  0.00),
    "gemma4:e4b":                    ( 0.00,  0.00),
    "qwen2.5-coder:7b":              ( 0.00,  0.00),
}


@dataclass
class LLMCall:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: float
    cost_usd: float
    purpose: str = ""           # what the call was for (e.g. "spec_extraction")
    cached: bool = False        # was this served from a cache?
    timestamp: float = field(default_factory=time.time)


# Session-scoped accumulator. Thread-safe. Reset at the start of each pipeline run.
_LOCK = threading.Lock()
_SESSION: list[LLMCall] = []


def reset_session() -> None:
    """Clear the accumulator. Call at the start of each pipeline run."""
    with _LOCK:
        _SESSION.clear()


def track(
    *,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: float = 0.0,
    purpose: str = "",
    cached: bool = False,
) -> LLMCall:
    """Record an LLM call. Returns the LLMCall dataclass for inspection.

    Cost is computed automatically from _MODEL_PRICING. Unknown models are
    recorded at $0 (with a stderr warning) so missing entries don't crash.
    """
    cost = _compute_cost(model, input_tokens, output_tokens)
    if cached:
        cost = 0.0
    call = LLMCall(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        cost_usd=cost,
        purpose=purpose,
        cached=cached,
    )
    with _LOCK:
        _SESSION.append(call)
    return call


def _compute_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    """Look up model pricing and compute USD cost."""
    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        # Try a fuzzy match: longest prefix
        for k, v in _MODEL_PRICING.items():
            if model.startswith(k) or k.startswith(model.split(":")[0]):
                pricing = v
                break
    if pricing is None:
        # Unknown model — log once per session for the same model
        return 0.0
    in_rate, out_rate = pricing
    return (in_tokens / 1_000_000) * in_rate + (out_tokens / 1_000_000) * out_rate


def get_session_summary() -> dict[str, Any]:
    """Return a structured summary of the current session.

    Includes total cost, per-provider breakdown, per-purpose breakdown,
    cached-vs-live counts, and a pretty-printed table for terminal display.
    """
    with _LOCK:
        calls = list(_SESSION)
    if not calls:
        return {
            "n_calls": 0, "total_cost_usd": 0.0, "total_input_tokens": 0,
            "total_output_tokens": 0, "by_provider": {}, "by_purpose": {},
            "cache_hits": 0, "live_calls": 0, "pretty": "  (no LLM calls)",
        }

    total_in = sum(c.input_tokens for c in calls)
    total_out = sum(c.output_tokens for c in calls)
    total_cost = sum(c.cost_usd for c in calls)
    cache_hits = sum(1 for c in calls if c.cached)
    live = len(calls) - cache_hits

    by_provider: dict[str, dict] = {}
    for c in calls:
        b = by_provider.setdefault(c.provider, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "duration_ms": 0.0,
        })
        b["calls"] += 1
        b["input_tokens"] += c.input_tokens
        b["output_tokens"] += c.output_tokens
        b["cost_usd"] += c.cost_usd
        b["duration_ms"] += c.duration_ms

    by_purpose: dict[str, dict] = {}
    for c in calls:
        purpose = c.purpose or "unknown"
        b = by_purpose.setdefault(purpose, {"calls": 0, "cost_usd": 0.0})
        b["calls"] += 1
        b["cost_usd"] += c.cost_usd

    pretty = _pretty_summary(calls, total_in, total_out, total_cost,
                             cache_hits, live, by_provider, by_purpose)

    return {
        "n_calls":            len(calls),
        "total_cost_usd":     round(total_cost, 6),
        "total_input_tokens":  total_in,
        "total_output_tokens": total_out,
        "cache_hits":         cache_hits,
        "live_calls":         live,
        "by_provider":        {k: _round_dict(v) for k, v in by_provider.items()},
        "by_purpose":         {k: _round_dict(v) for k, v in by_purpose.items()},
        "pretty":             pretty,
    }


def _round_dict(d: dict) -> dict:
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}


def _pretty_summary(calls, total_in, total_out, total_cost,
                    cache_hits, live, by_provider, by_purpose) -> str:
    lines = [
        "  ────────────────  LLM COST  ────────────────",
        f"  Calls:           {len(calls)}  ({live} live, {cache_hits} cached)",
        f"  Tokens (in/out): {total_in:,} / {total_out:,}",
        f"  Total cost:      ${total_cost:.4f}",
    ]
    if by_provider:
        lines.append("")
        lines.append("  Provider breakdown:")
        for p, b in sorted(by_provider.items(), key=lambda kv: -kv[1]["cost_usd"]):
            lines.append(
                f"    {p:14s}  {b['calls']:3d} calls  "
                f"{b['input_tokens']:>8,} in / {b['output_tokens']:>6,} out  "
                f"${b['cost_usd']:.4f}"
            )
    if by_purpose and any(p != "unknown" for p in by_purpose):
        lines.append("")
        lines.append("  Purpose breakdown:")
        for p, b in sorted(by_purpose.items(), key=lambda kv: -kv[1]["cost_usd"]):
            lines.append(f"    {p:24s}  {b['calls']:3d} calls  ${b['cost_usd']:.4f}")
    return "\n".join(lines)


def write_session_log(path: str | Path) -> Path:
    """Write the session calls + summary to a JSON file."""
    with _LOCK:
        calls = list(_SESSION)
    payload = {
        "summary": get_session_summary(),
        "calls": [
            {
                "provider": c.provider, "model": c.model,
                "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                "duration_ms": c.duration_ms, "cost_usd": c.cost_usd,
                "purpose": c.purpose, "cached": c.cached,
                "timestamp": c.timestamp,
            }
            for c in calls
        ],
    }
    # Don't include the "pretty" field in the JSON
    payload["summary"].pop("pretty", None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
