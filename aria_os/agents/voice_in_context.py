"""Voice-in-context agent.

The user is hovering over a feature in Fusion / Rhino / Onshape and
says something like "make this hole 2mm bigger" or "add a fillet
here". This module:
  1. Transcribes the audio (Whisper / Groq / Gemini STT)
  2. Resolves "this", "that", "here" against the host_context's
     `selection` payload + the active body's user parameters
  3. Routes the command through the existing delta_detector to
     decide modify vs extend vs new
  4. Hands off to the dispatcher with the resolved goal + spec

Public API:
    from aria_os.agents.voice_in_context import voice_to_plan
    out = voice_to_plan(
        audio_wav_path="utterance.wav",
        host_context={
            "selection": [
                {"type": "edge",  "id": "...", "feature": "bolt_hole_3"},
            ],
            "user_parameters": [
                {"name": "flange_bolt_dia", "expression": "6 mm"},
            ],
            "feature_tree": {"features": ["body", "bolt_hole_3", ...]},
        })

Returns:
    {goal, spec, plan, transcription, resolved_target}
"""
from __future__ import annotations

import re
from pathlib import Path


_DEMONSTRATIVES = (
    "this", "that", "these", "those", "the highlighted", "selected",
    "current", "active", "this one", "that one", "the one",
)


def _transcribe(wav_path: Path) -> str:
    """Try existing Whisper / Groq / Gemini paths."""
    try:
        from ..speech_to_text import transcribe
    except ImportError:
        raise RuntimeError("speech_to_text module unavailable")
    text = transcribe(wav_path)
    if not text:
        raise RuntimeError("transcription failed (no STT backend)")
    return text.strip()


def _resolve_target(text: str, host_context: dict) -> dict | None:
    """When the utterance contains 'this/that/here', resolve to the
    first selected entity in the host_context. Returns a dict with
    {kind, id, feature_alias} or None if the utterance isn't
    demonstrative."""
    t = text.lower()
    if not any(d in t for d in _DEMONSTRATIVES):
        return None
    selection = (host_context or {}).get("selection") or []
    if not selection:
        return None
    sel = selection[0]
    return {
        "kind":          sel.get("type"),
        "id":            sel.get("id"),
        "feature_alias": sel.get("feature") or sel.get("alias"),
    }


def _classify_intent(text: str) -> str:
    """Cheap intent classifier — modify | extend | query.

    Modify  = change a dim of an existing feature ("make X bigger")
    Extend  = add a new feature ("add a fillet")
    Query   = ask about something ("what's the wall thickness")"""
    t = text.lower()
    modify_kws = ("bigger", "smaller", "thicker", "thinner", "taller",
                   "shorter", "wider", "narrower", "deeper", "longer",
                   "increase", "decrease", "change", "set", "update",
                   "make.*bigger", "make.*smaller", "raise to", "lower to")
    extend_kws = ("add", "put", "drill", "fillet", "chamfer", "mirror",
                   "pocket", "extrude", "another")
    query_kws = ("what's", "what is", "how big", "how thick", "how many",
                  "show me")
    if any(re.search(k, t) for k in modify_kws):
        return "modify"
    if any(re.search(rf"\b{k}\b", t) for k in extend_kws):
        return "extend"
    if any(re.search(k, t) for k in query_kws):
        return "query"
    return "modify"   # default — most in-context commands are modify


def _build_goal_with_target(text: str, target: dict | None) -> str:
    """Stitch the transcribed text + the resolved target so the
    planner has unambiguous context. Falls back to plain transcription
    if no target was resolved."""
    if not target:
        return text
    feat = target.get("feature_alias") or target.get("id") or "selected_feature"
    # Replace the FIRST demonstrative with the feature name so the
    # planner sees an unambiguous reference.
    out = text
    for d in _DEMONSTRATIVES:
        if d in out.lower():
            # Case-insensitive single-replace
            idx = out.lower().find(d)
            out = out[:idx] + feat + out[idx + len(d):]
            break
    return out


def voice_to_plan(
        audio_wav_path: str | Path,
        *,
        host_context: dict | None = None,
        repo_root: Path | None = None,
        prefer_llm: bool = True,
        quality: str = "balanced") -> dict:
    """Voice utterance + host_context → validated ARIA plan.

    Args:
        audio_wav_path: WAV file (16-bit PCM, 16kHz preferred)
        host_context:   the same payload the dashboard receives from
                        Fusion/Rhino/Onshape — contains selection,
                        user_parameters, feature_tree.
    """
    audio_wav_path = Path(audio_wav_path)
    if not audio_wav_path.is_file():
        raise FileNotFoundError(audio_wav_path)
    host_context = host_context or {}

    text = _transcribe(audio_wav_path)
    target = _resolve_target(text, host_context)
    intent = _classify_intent(text)
    resolved_goal = _build_goal_with_target(text, target)

    # delta_detector decides new/modify/extend mode for the planner.
    # Voice in-context with a resolved target is almost always
    # modify or extend, never new.
    mode = "modify" if intent == "modify" else (
        "extend" if intent == "extend" else "new")

    # Hand off
    from ..native_planner.dispatcher import make_plan
    spec: dict = {}
    if target:
        spec["target_feature"] = target.get("feature_alias") \
            or target.get("id")
    plan = make_plan(
        resolved_goal, spec,
        prefer_llm=prefer_llm, quality=quality,
        repo_root=repo_root, host_context=host_context, mode=mode)

    return {
        "transcription":   text,
        "intent":          intent,
        "resolved_target": target,
        "goal":            resolved_goal,
        "spec":            spec,
        "mode":            mode,
        "plan":            plan,
    }


__all__ = ["voice_to_plan", "_classify_intent",
            "_resolve_target", "_build_goal_with_target"]
