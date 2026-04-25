"""Structured LLM output — forces providers to return JSON matching a
schema instead of hoping free-text output parses cleanly.

Two backend paths, tried in order (the fast tier skips these because
local Ollama doesn't support tool use):

  1. Anthropic `tool_use` — we define a tool whose input_schema is the
     plan schema, then force `tool_choice={"type": "tool"}`. Claude's
     response is guaranteed to have a tool_use block with JSON matching
     the schema.

  2. Gemini `responseSchema` — same idea via Gemini's structured-output
     config. `response_mime_type="application/json"` + the schema.

Output: a list of op dicts, or None if no structured-output backend
is available (caller falls back to free-text LLM + JSON parsing).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..llm_client import (get_anthropic_key, get_google_key,
                            _gemini_model, _record_llm_call)


# --- Plan schema -------------------------------------------------------

# Op kinds and their required params. Kept minimal so the LLM has room
# to pass kind-specific params; the validator catches missing ones.
_OP_KINDS = [
    "beginPlan", "addParameter",
    "newSketch", "sketchCircle", "sketchRect",
    "extrude", "circularPattern", "fillet",
    # W1: extended sketch primitives
    "sketchSpline", "sketchPolyline", "sketchTangentArc",
    "sketchOffset", "sketchProjection", "sketchEquationCurve",
    # W1: extended solid features
    "revolve", "sweep", "loft", "helix", "coil",
    "rib", "shell", "draft", "boundarySurface", "thicken",
    # W1: standard hardware
    "threadFeature", "gearFeature",
    "asmBegin", "addComponent", "joint",
    "beginDrawing", "newSheet", "addView", "addTitleBlock",
    # ECAD (Fusion Electronics)
    "beginElectronics", "placeSymbol", "placeFootprint",
    "addConnection", "boardOutline",
    # KiCad server-side
    "beginBoard", "setStackup", "addNet", "addTrack", "addVia",
    "addZone", "routeBoard",
]

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "array",
            "description": "Ordered list of feature operations.",
            "items": {
                "type": "object",
                "properties": {
                    "kind":   {"type": "string", "enum": _OP_KINDS},
                    "params": {"type": "object",
                                 "description": "Kind-specific parameters."},
                    "label":  {"type": "string",
                                 "description": "Short human description."},
                },
                "required": ["kind", "params"],
            },
        },
    },
    "required": ["plan"],
}


# --- Anthropic (Claude) structured output ------------------------------

def _try_anthropic_structured(
        prompt: str, system: str,
        *, repo_root: Path | None = None,
        model_tier: str = "balanced") -> list[dict] | None:
    api_key = get_anthropic_key(repo_root)
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore
    except Exception:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        return None

    model_candidates = (
        ["claude-sonnet-4-6", "claude-3-5-sonnet-20241022"]
        if model_tier == "premium"
        else ["claude-sonnet-4-6", "claude-3-5-sonnet-20241022",
               "claude-haiku-4-5-20251001"])

    tools = [{
        "name": "emit_plan",
        "description": ("Emit the ordered CAD feature-op plan. "
                         "Call exactly once with the full array."),
        "input_schema": _PLAN_SCHEMA,
    }]

    for model in model_candidates:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                tools=tools,
                tool_choice={"type": "tool", "name": "emit_plan"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            if "not_found" in str(exc).lower() or "not found" in str(exc).lower():
                continue
            print(f"[LLM-structured] anthropic/{model} failed: {exc}")
            continue
        _record_llm_call("anthropic")
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                inp = getattr(block, "input", None)
                if isinstance(inp, dict) and isinstance(inp.get("plan"), list):
                    return inp["plan"]
        # Tool wasn't called — shouldn't happen with tool_choice forced
        return None
    return None


# --- Gemini structured output ------------------------------------------

def _try_gemini_structured(
        prompt: str, system: str,
        *, repo_root: Path | None = None) -> list[dict] | None:
    api_key = get_google_key(repo_root)
    if not api_key:
        return None
    try:
        import google.generativeai as genai  # type: ignore
    except Exception:
        return None
    model_name = _gemini_model(repo_root)
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system)
    except Exception:
        return None

    # Gemini's responseSchema format: similar shape, but property types
    # as strings, no $ref, no definitions.
    gemini_schema = {
        "type": "OBJECT",
        "properties": {
            "plan": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "kind":   {"type": "STRING", "enum": _OP_KINDS},
                        "params": {"type": "OBJECT"},
                        "label":  {"type": "STRING"},
                    },
                    "required": ["kind", "params"],
                },
            },
        },
        "required": ["plan"],
    }
    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "response_mime_type": "application/json",
                "response_schema": gemini_schema,
                "temperature": 0.2,
            },
        )
    except Exception as exc:
        print(f"[LLM-structured] gemini failed: {exc}")
        return None

    try:
        import json as _json
        data = _json.loads(response.text)
        _record_llm_call("gemini")
        if isinstance(data, dict) and isinstance(data.get("plan"), list):
            return data["plan"]
    except Exception:
        return None
    return None


# --- Unified entry point -----------------------------------------------

def plan_from_llm_structured(
        prompt: str, system: str,
        *, quality: str = "balanced",
        repo_root: Path | None = None) -> list[dict] | None:
    """Try structured-output backends in tier order. Returns None if
    no provider supports structured output (fast/Ollama tier will
    always hit this path → None → caller falls back to free-text)."""
    if quality == "premium":
        chain = [
            lambda: _try_anthropic_structured(
                prompt, system, repo_root=repo_root, model_tier="premium"),
            lambda: _try_gemini_structured(prompt, system, repo_root=repo_root),
        ]
    elif quality == "fast":
        # Fast tier uses Ollama which doesn't support tool use. Skip
        # structured output entirely; caller falls back to free-text
        # parse + tier escalation.
        return None
    else:  # balanced
        chain = [
            lambda: _try_gemini_structured(prompt, system, repo_root=repo_root),
            lambda: _try_anthropic_structured(
                prompt, system, repo_root=repo_root, model_tier="balanced"),
        ]
    for fn in chain:
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as exc:
            print(f"[LLM-structured] unexpected error: {exc}")
    return None
