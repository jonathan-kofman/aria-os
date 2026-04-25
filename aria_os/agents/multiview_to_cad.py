"""Multi-image triangulation → CAD.

Takes 2-4 photos of the same physical part from different angles
and extracts a goal + spec dict using a vision LLM that can reason
across views (it's much more accurate to extract dims when the LLM
can see top + front + side).

Triangulation strategy:
  - Each photo gets a label (top, front, right, iso, ...) the
    caller assigns.
  - The LLM is told "these are different views of the SAME part" so
    it cross-references features.
  - Optional reference object in any one of the photos calibrates
    absolute scale for all (caliper, ruler, known fastener).
  - Confidence is scored higher than single-image because each
    feature can be cross-validated.

Public API:
    from aria_os.agents.multiview_to_cad import multiview_to_plan
    out = multiview_to_plan(
        views=[
            {"path": "top.jpg",   "label": "top"},
            {"path": "front.jpg", "label": "front"},
            {"path": "right.jpg", "label": "right"},
        ],
        reference={"type": "M8_bolt", "across_flats_mm": 13.0,
                   "in_view": "front"},
        repo_root=Path("."))
"""
from __future__ import annotations

import json
from pathlib import Path

from ..llm_client import (
    get_anthropic_key, get_google_key, _IMAGE_MIMETYPES,
    _gemini_model, _record_llm_call,
)
from .sketch_agent import _parse_engineering_response


_MULTIVIEW_SYSTEM = """\
You are reverse-engineering a physical part from MULTIPLE photographs
taken from different angles. The same part is in every image.

Cross-reference features across views:
  - A hole visible in the top view and the front view IS THE SAME hole
    — don't double-count it.
  - Use the angle metadata (top/front/right/iso) to assign each
    feature to the right axis.

Output a single JSON object with:
  - goal:        single-paragraph natural-language description
  - part_family: bracket | flange | gear | impeller | shaft | housing |
                  plate | fastener | nozzle | pulley | other
  - spec:        dim dict (od_mm, bore_mm, thickness_mm, length_mm,
                  width_mm, height_mm, depth_mm, n_holes, hole_dia_mm,
                  bolt_circle_r_mm, material, etc.)
  - features_per_view: list[{view_label, observed_features}] — what
                  YOU saw in each photo (debugging aid)
  - reference_used: which scale reference you trusted
  - confidence:  0..1. With ≥2 views and a calibrated reference,
                  confidence should be ≥0.85. Without any reference,
                  cap at 0.5 (proportions only).

Rules:
  - Scale: use the user-provided reference if any. Otherwise look
    for common ones (caliper readout, US quarter 24.26mm, M8 bolt
    13mm across flats, AAA battery 10.5×44.5mm).
  - Round dims to nearest 0.5mm unless calibration is digital
    caliper (3+ decimal places).
  - Don't invent features that are only in one view if the other
    views contradict them.

Output ONLY the JSON object. No prose, no markdown fences.
"""


def _gemini_multi_image(views: list[dict], system: str,
                          repo_root: Path | None) -> str | None:
    """Multi-image Gemini call via the new google-genai SDK."""
    api_key = get_google_key(repo_root)
    if not api_key:
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        return None
    try:
        client = genai.Client(api_key=api_key)
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            max_output_tokens=2048,
        )
        contents: list = []
        for v in views:
            p = Path(v["path"])
            mt = _IMAGE_MIMETYPES.get(p.suffix.lower(), "image/jpeg")
            contents.append(types.Part.from_bytes(
                data=p.read_bytes(), mime_type=mt))
            contents.append(f"Label for image above: {v.get('label', '?')}")
        contents.append(
            "Output the JSON object now, cross-referencing features "
            "between every view above.")
        for try_model in (_gemini_model(repo_root),
                            "gemini-2.5-flash", "gemini-2.0-flash"):
            try:
                response = client.models.generate_content(
                    model=try_model, contents=contents, config=cfg)
                text = response.text or ""
                if text.strip():
                    _record_llm_call("gemini")
                    return text
            except Exception as exc:
                msg = str(exc).lower()
                if "rate" in msg or "429" in msg or "quota" in msg:
                    continue
                if "not_found" in msg or "not found" in msg:
                    continue
                print(f"[MULTIVIEW] gemini/{try_model} failed: {exc}")
                return None
    except Exception as exc:
        print(f"[MULTIVIEW] gemini setup failed: {exc}")
    return None


def _anthropic_multi_image(views: list[dict], system: str,
                              repo_root: Path | None) -> str | None:
    """Multi-image Anthropic call via the messages API."""
    api_key = get_anthropic_key(repo_root)
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore
        import base64
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        content = []
        for v in views:
            p = Path(v["path"])
            mt = _IMAGE_MIMETYPES.get(p.suffix.lower(), "image/jpeg")
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mt,
                    "data": base64.b64encode(p.read_bytes()).decode("ascii"),
                },
            })
            content.append({
                "type": "text",
                "text": f"Label for image above: {v.get('label', '?')}",
            })
        content.append({
            "type": "text",
            "text": "Output the JSON object now.",
        })
        for model in ("claude-sonnet-4-6",
                       "claude-3-5-sonnet-20241022"):
            try:
                response = client.messages.create(
                    model=model, max_tokens=2048,
                    system=system,
                    messages=[{"role": "user", "content": content}])
                _record_llm_call("anthropic")
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        return block.text
            except Exception as exc:
                msg = str(exc).lower()
                if "credit" in msg or "balance" in msg:
                    return None
                if "not_found" in msg or "not found" in msg:
                    continue
                print(f"[MULTIVIEW] anthropic/{model} failed: {exc}")
                return None
    except Exception as exc:
        print(f"[MULTIVIEW] anthropic setup failed: {exc}")
    return None


def multiview_to_plan(
        views: list[dict],
        *,
        reference: dict | None = None,
        hint: str | None = None,
        repo_root: Path | None = None,
        prefer_llm: bool = True,
        quality: str = "balanced") -> dict:
    """Triangulate dims across multiple views → ARIA plan.

    Args:
        views: list of {path: str, label: str}. Labels recommended:
               "top", "front", "right", "iso", "back", "bottom".
               2-4 views is the sweet spot; >4 confuses the LLM.
        reference: optional ground-truth scale (see image_to_cad.py).
        hint: optional human-language context.
    """
    if not views or len(views) < 2:
        raise ValueError("multiview_to_plan needs ≥2 views")
    if len(views) > 6:
        raise ValueError("multiview_to_plan caps at 6 views to keep "
                          "LLM context tractable")

    for v in views:
        if not Path(v.get("path", "")).is_file():
            raise FileNotFoundError(v.get("path"))

    system_prompt = _MULTIVIEW_SYSTEM
    if reference:
        system_prompt += (
            "\n\nUSER-PROVIDED REFERENCE (trust this for scale):\n"
            f"  {json.dumps(reference, indent=2)}")
    if hint:
        system_prompt += f"\n\nUSER HINT:\n  {hint}"

    raw = _gemini_multi_image(views, system_prompt, repo_root)
    if raw is None:
        raw = _anthropic_multi_image(views, system_prompt, repo_root)
    if raw is None:
        raise RuntimeError(
            "Vision LLM unavailable for multi-image triangulation")

    parsed = _parse_engineering_response(raw)
    goal = parsed.get("goal", "")
    spec = parsed.get("spec", {}) or {}
    confidence = float(parsed.get("confidence", 0.5))
    if not goal:
        raise ValueError(
            f"Vision LLM returned no usable goal (raw: {raw[:200]!r})")

    from ..native_planner.dispatcher import make_plan
    plan = make_plan(goal, spec, prefer_llm=prefer_llm,
                       quality=quality, repo_root=repo_root)

    return {
        "goal": goal,
        "spec": spec,
        "part_family": parsed.get("part_family"),
        "features_per_view": parsed.get("features_per_view", []),
        "reference_used": parsed.get("reference_used"),
        "confidence": confidence,
        "plan": plan,
        "raw_response": raw,
        "n_views": len(views),
    }


__all__ = ["multiview_to_plan"]
