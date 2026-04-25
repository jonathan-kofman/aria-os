"""Sketch-to-plan agent.

Takes a hand-drawn or iPad-sketched PNG (optionally with stroke
metadata) and turns it into an ARIA goal string + spec dict that
the existing planner consumes.

Two modes:
    "rough"        — free-form sketch; vision LLM identifies the
                     intent ("looks like a U-bracket with two
                     mounting holes") and returns a goal string.
    "engineering"  — orthographic sketch with dim labels; vision
                     LLM extracts each labeled dim into the spec
                     dict and returns the complete goal string +
                     spec.

Public API:
    from aria_os.agents.sketch_agent import sketch_to_plan
    plan = sketch_to_plan(
        sketch_path="user_sketch.png",
        mode="rough",
        repo_root=Path("."))

Returns:
    {
        "goal":  str,
        "spec":  dict,
        "plan":  list[dict],   # output of dispatcher.make_plan()
        "raw_response": str,
    }

Falls through to dispatcher.make_plan() so the entire downstream
chain (validator, retrieval, escalation, verification) runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..llm_client import (
    _try_anthropic_vision, _try_gemini_vision, _IMAGE_MIMETYPES,
)


_SKETCH_ROUGH_SYSTEM = """\
You are a CAD design assistant looking at a hand-drawn or iPad sketch.
The user is communicating their design INTENT, not a precise spec.

Your job: produce a single-paragraph goal string that an engineer
could hand to a CAD tool. Make reasonable assumptions for any dim
that isn't labeled (use round numbers — 50mm, 100mm — and call out
the assumption in parentheses).

Rules:
  - Identify the part type (bracket, plate, housing, flange, etc.).
  - List visible features: holes, slots, fillets, bends, ribs.
  - Estimate proportions from the drawing's aspect ratio.
  - Material defaults to AL 6061-T6 unless a finish suggests otherwise.
  - Output ONLY the goal string — no preamble, no JSON, no markdown.

Example output: "L-bracket 80x60mm legs, 5mm thick AL 6061-T6, with
4x M6 mounting holes (2 on each leg, ~15mm in from the corners)"
"""

_SKETCH_ENGINEERING_SYSTEM = """\
You are a CAD design assistant looking at an engineering sketch with
dimensional labels (numbers next to features, often with arrows).

Your job: extract every labeled dimension into a structured JSON
output. Produce ALL of:
  - goal: single-paragraph natural-language description
  - spec: object with dim keys like od_mm, bore_mm, thickness_mm,
    width_mm, height_mm, depth_mm, n_holes, hole_dia_mm,
    bolt_circle_r_mm, etc.
  - confidence: 0..1 — how legible the sketch was.

Rules:
  - Read every dim label; treat ⌀/Ø as diameter.
  - Match dim arrows to features.
  - Material from a callout if present, else AL 6061-T6.
  - PCD = pitch circle DIAMETER → store bolt_circle_r_mm = PCD/2.

Output ONLY the JSON object. No prose, no markdown fences.

Example:
  {
    "goal": "100mm OD flange with 4 M6 bolt holes on 80mm PCD, 6mm thick",
    "spec": {"od_mm": 100, "n_bolts": 4, "bolt_dia_mm": 6,
             "bolt_circle_r_mm": 40, "thickness_mm": 6,
             "material": "AL 6061-T6"},
    "confidence": 0.85
  }
"""


def _vision_call(image_bytes: bytes, media_type: str,
                  system_prompt: str,
                  repo_root: Path | None) -> str | None:
    """Try Gemini → Anthropic for a vision call. Returns the raw
    text response or None."""
    user_prompt = system_prompt + "\n\nProcess this sketch."
    try:
        r = _try_gemini_vision(image_bytes, media_type, user_prompt,
                                 repo_root)
        if r:
            return r
    except Exception as exc:
        print(f"[SKETCH] gemini failed: {exc}")
    try:
        r = _try_anthropic_vision(image_bytes, media_type, user_prompt,
                                    repo_root)
        if r:
            return r
    except Exception as exc:
        print(f"[SKETCH] anthropic failed: {exc}")
    return None


def _parse_engineering_response(raw: str) -> dict:
    """Pull a JSON object out of the engineering-mode response.
    Tolerates markdown fences, trailing prose, etc."""
    s = (raw or "").strip()
    # Strip markdown fences if present
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        # Find the first balanced { ... }
        start = s.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(s)):
                if s[i] == "{":
                    depth += 1
                elif s[i] == "}":
                    depth -= 1
                    if depth == 0:
                        s = s[start:i + 1]
                        break
    # Strip trailing commas (LLM habit)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        data = json.loads(s)
        if not isinstance(data, dict):
            raise ValueError("Not a JSON object")
        return data
    except Exception as exc:
        raise ValueError(
            f"Could not parse engineering-sketch response: {exc}") from None


def sketch_to_plan(
        sketch_path: str | Path,
        *,
        mode: str = "rough",
        repo_root: Path | None = None,
        prefer_llm: bool = True,
        quality: str = "balanced") -> dict:
    """Turn a sketch image into a validated ARIA plan.

    Args:
        sketch_path: PNG/JPG of the sketch
        mode: "rough" (free-form intent) or "engineering" (labeled
              dims; we extract a structured spec)
        repo_root: for .env / API key lookup
        prefer_llm: pass-through to dispatcher (default True so the
                     LLM planner gets first shot)
        quality: LLM tier for the planner call
    """
    sketch_path = Path(sketch_path)
    if not sketch_path.is_file():
        raise FileNotFoundError(sketch_path)
    media_type = _IMAGE_MIMETYPES.get(sketch_path.suffix.lower(),
                                          "image/png")
    image_bytes = sketch_path.read_bytes()

    system = (_SKETCH_ROUGH_SYSTEM if mode == "rough"
                else _SKETCH_ENGINEERING_SYSTEM)
    raw = _vision_call(image_bytes, media_type, system, repo_root)
    if raw is None:
        raise RuntimeError(
            "Vision LLM unavailable — set GOOGLE_API_KEY / ANTHROPIC_API_KEY")

    if mode == "rough":
        goal = raw.strip()
        spec: dict = {}
    else:
        parsed = _parse_engineering_response(raw)
        goal = parsed.get("goal", "")
        spec = parsed.get("spec", {}) or {}

    if not goal:
        raise ValueError(
            f"Vision LLM returned no usable goal (raw: {raw[:200]!r})")

    # Hand off to the existing planner
    from ..native_planner.dispatcher import make_plan
    plan = make_plan(goal, spec, prefer_llm=prefer_llm,
                       quality=quality, repo_root=repo_root)

    return {
        "goal": goal,
        "spec": spec,
        "plan": plan,
        "raw_response": raw,
        "mode": mode,
    }


__all__ = ["sketch_to_plan", "_parse_engineering_response"]
