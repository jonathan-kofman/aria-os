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

from ..llm_client import (get_anthropic_key, get_google_key, get_groq_key,
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
    # W3: implicit / SDF ops (mesh-into-CAD bridge)
    "implicitInfill", "implicitChannel", "implicitLattice",
    "implicitField", "implicitBoolean",
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


def _rescue_groq_failed_generation(exc: Exception) -> list[dict] | None:
    """When Groq's llama mis-formats tool_use as text (`<function=emit_plan>
    [...]`), the JSON array is still in the failed_generation payload.
    Extract it.

    The Groq SDK exposes the error body in two shapes depending on
    version: `exc.body` may be a dict (newer SDK) or a JSON string
    (older), and the actual `failed_generation` is nested under
    `error.failed_generation`. We extract the failed_generation
    string FIRST (so we don't need to navigate nested Python repr
    edge-cases), then balance brackets inside that one field."""
    import json as _json
    import re as _re

    # Prefer the structured body — saves us from parsing the str(exc)
    # which Python-repr-escapes the failed_generation JSON.
    failed_gen: str | None = None
    body_attr = getattr(exc, "body", None)
    if isinstance(body_attr, dict):
        err = body_attr.get("error") or {}
        if isinstance(err, dict):
            failed_gen = err.get("failed_generation") or None
    if failed_gen is None and isinstance(body_attr, str):
        try:
            d = _json.loads(body_attr)
            if isinstance(d, dict):
                err = d.get("error") or {}
                failed_gen = (err.get("failed_generation")
                                if isinstance(err, dict) else None)
        except Exception:
            pass
    if failed_gen is None:
        # Last resort: regex-extract from str(exc). The repr embeds the
        # failed_generation between single quotes after the key. Match
        # 'failed_generation': '...' allowing nested escaped quotes.
        s = str(exc)
        m = _re.search(r"'failed_generation':\s*'((?:[^'\\]|\\.)*)'", s,
                        _re.DOTALL)
        if m:
            failed_gen = m.group(1)
            # Reverse Python's repr-escaping (literal backslash sequences)
            failed_gen = (failed_gen
                            .replace("\\n", "\n")
                            .replace("\\t", "\t")
                            .replace("\\'", "'")
                            .replace('\\"', '"')
                            .replace("\\\\", "\\"))
    if not failed_gen:
        return None

    # Now find the JSON array inside the failed_gen wrapper.
    start = failed_gen.find("[")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(failed_gen)):
        c = failed_gen[i]
        if c == "[": depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                snippet = failed_gen[start:i + 1]
                # Strip trailing commas (LLMs love these)
                snippet = _re.sub(r",\s*([}\]])", r"\1", snippet)
                try:
                    data = _json.loads(snippet)
                    if isinstance(data, list):
                        return data
                except Exception:
                    return None
                break
    return None


# --- Groq structured output (OpenAI-compatible tool_use) ---------------
#
# Groq's free tier is generous: 30 RPM and 14,400/day on the largest
# llama-3.3-70b-versatile model. They expose an OpenAI-compatible
# /chat/completions endpoint with full `tools` + `tool_choice` support,
# which we use the same way we use Anthropic's tool_use: force the
# model to call `emit_plan` with a JSON-schema-validated argument.
#
# Why prefer Groq for cost-constrained eval runs:
#   - Sub-second inference (vs. 1-3s Anthropic, 0.5-2s Gemini)
#   - Free quota survives a full 50-prompt eval ~5×/day
#   - Structurally sound output (tool_use, no parse failures)
#
# Why not for production: 70b is below Sonnet/Gemini-2.5 on long-tail
# parts. Use it as a baseline backstop, not the primary planner.

def _try_groq_structured(
        prompt: str, system: str,
        *, repo_root: Path | None = None) -> list[dict] | None:
    api_key = get_groq_key(repo_root)
    if not api_key:
        return None
    try:
        from groq import Groq  # type: ignore
    except ImportError:
        return None
    try:
        client = Groq(api_key=api_key)
    except Exception:
        return None

    tools = [{
        "type": "function",
        "function": {
            "name": "emit_plan",
            "description": ("Emit the ordered CAD feature-op plan. "
                              "Call exactly once with the full array."),
            "parameters": _PLAN_SCHEMA,
        },
    }]
    # llama-3.3-70b-versatile supports tool_use; llama-3.1 also works.
    # Try the larger model first; fall back to 8b if rate-limited.
    for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                tools=tools,
                tool_choice={"type": "function",
                              "function": {"name": "emit_plan"}},
                max_completion_tokens=4096,
                temperature=0.2,
            )
        except Exception as exc:
            err_msg = str(exc).lower()
            # Rate limit on this model — try the next one
            if "rate" in err_msg or "429" in err_msg or "quota" in err_msg:
                print(f"[LLM-structured] groq/{model} rate-limited; "
                       "trying next model")
                continue
            if "decommissioned" in err_msg or "not_found" in err_msg \
                    or "not found" in err_msg:
                continue
            # Groq's llama models sometimes return tool_use as text in
            # the response body rather than via the proper tool_calls
            # path ("tool_use_failed"). The 400 error includes the
            # failed_generation as text — extract the JSON array if
            # we can find one.
            if "tool_use_failed" in err_msg or "failed to call" in err_msg:
                rescued = _rescue_groq_failed_generation(exc)
                if rescued is not None:
                    _record_llm_call("groq")
                    return rescued
            print(f"[LLM-structured] groq/{model} failed: {exc}")
            return None
        _record_llm_call("groq")
        msg = response.choices[0].message
        for call in (msg.tool_calls or []):
            try:
                import json as _json
                args = _json.loads(call.function.arguments)
                if isinstance(args, dict) and isinstance(args.get("plan"), list):
                    return args["plan"]
            except Exception:
                continue
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
        # Premium: quality first. Anthropic Sonnet → Gemini → Groq.
        chain = [
            lambda: _try_anthropic_structured(
                prompt, system, repo_root=repo_root, model_tier="premium"),
            lambda: _try_gemini_structured(prompt, system, repo_root=repo_root),
            lambda: _try_groq_structured(prompt, system, repo_root=repo_root),
        ]
    elif quality == "fast":
        # Fast tier: Groq's llama-3.3-70b is fast + free + supports
        # tool_use, which is structurally better than the legacy
        # Ollama fallback. Try Groq first; fall through to free-text
        # parsing if no key.
        chain = [
            lambda: _try_groq_structured(prompt, system, repo_root=repo_root),
        ]
    else:  # balanced
        # Balanced: Groq first (sub-second + free), then Gemini (free
        # tier with rate limits), then Anthropic. The order is chosen
        # so a 50-prompt eval can run on free tiers without burning
        # paid credits.
        chain = [
            lambda: _try_groq_structured(prompt, system, repo_root=repo_root),
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
