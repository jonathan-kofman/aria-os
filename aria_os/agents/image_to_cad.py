"""Image-to-CAD reverse engineer.

Single photo of a real part → goal + spec → planner.

Differs from `sketch_agent` in that the input is a PHOTOGRAPH of a
physical object, not a hand drawing. The vision LLM has to:
  1. Identify the part type (often unambiguous from photo)
  2. Estimate dimensions — preferably with a reference object in
     the photo for absolute scale (caliper, ruler, US quarter,
     M8 bolt, etc.)
  3. Extract features (holes, threads, fillets) that are visible

Calibration approaches in order of accuracy:
  - explicit `reference` arg with object_type + nominal_mm
  - LLM-detected reference in the photo (caliper readout > ruler
    > coin > "thumb width")
  - relative dims only (output spec uses ratios; absolute scale
    requested from user as follow-up)

Public API:
    from aria_os.agents.image_to_cad import image_to_plan
    out = image_to_plan(
        image_path="part.jpg",
        reference={"type": "caliper", "reading_mm": 47.3},
        repo_root=Path("."))

    out["plan"]   # validated plan ready for the host bridge
    out["spec"]   # extracted dim dict
    out["confidence"]  # 0..1 — how trustworthy the dims are
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..llm_client import (
    _try_anthropic_vision, _try_gemini_vision, _IMAGE_MIMETYPES,
)
from .sketch_agent import _parse_engineering_response, _vision_call


_IMAGE_TO_CAD_SYSTEM = """\
You are reverse-engineering a physical part from a photograph for a
CAD pipeline. Output one JSON object containing:
  - goal:        single-paragraph natural-language description
  - part_family: one of (bracket, flange, gear, impeller, shaft,
                  housing, plate, fastener, nozzle, pulley, other)
  - spec:        dim dict with mm-typed keys (od_mm, bore_mm,
                  thickness_mm, length_mm, width_mm, height_mm,
                  n_holes, hole_dia_mm, bolt_circle_r_mm,
                  material, n_blades, etc.)
  - reference_used: which scale reference you trusted (e.g.
                  "caliper readout 47.3mm", "M8 bolt visible at
                  bottom-right", "user-provided reference")
  - confidence:  0..1 — how trustworthy the absolute dims are.
                  0.9+ if a calibrated reference was clearly visible.
                  0.5-0.7 if only proportions are reliable.
                  <0.5 if dim extraction is essentially guessing.

Rules:
  - If the user provides a `reference` (caliper reading, ruler, known
    fastener), TRUST it as ground-truth scale and propagate dims
    proportionally.
  - Common reference fallbacks if no explicit reference:
      US quarter = 24.26mm dia
      US penny = 19.05mm dia
      AAA battery = 10.5mm × 44.5mm
      AA battery = 14.5mm × 50.5mm
      Standard credit card = 85.6 × 53.98mm
  - Round dims to nearest 0.5mm unless calibration is ≥3-decimal
    (digital caliper).
  - Material from finish/colour: shiny silver = AL 6061; dark grey
    matte = mild steel; white plastic = ABS or PLA.

Output ONLY the JSON object. No preamble, no markdown fences.
"""


def image_to_plan(
        image_path: str | Path,
        *,
        reference: dict | None = None,
        hint: str | None = None,
        repo_root: Path | None = None,
        prefer_llm: bool = True,
        quality: str = "balanced") -> dict:
    """Photo of a physical part → validated ARIA plan.

    Args:
        image_path: JPG/PNG of the part
        reference: optional ground-truth scale, e.g.
                   {"type": "caliper", "reading_mm": 47.3} or
                   {"type": "M8_bolt", "nominal_mm": 8.0,
                    "across_flats_mm": 13.0}
        hint: optional human-language hint (e.g. "this is the bracket
              from the rear assembly")
    """
    image_path = Path(image_path)
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    media_type = _IMAGE_MIMETYPES.get(image_path.suffix.lower(),
                                          "image/jpeg")
    image_bytes = image_path.read_bytes()

    system_prompt = _IMAGE_TO_CAD_SYSTEM
    if reference:
        system_prompt += (
            f"\n\nUSER-PROVIDED REFERENCE (trust this for scale):\n"
            f"  {json.dumps(reference, indent=2)}")
    if hint:
        system_prompt += f"\n\nUSER HINT:\n  {hint}"

    raw = _vision_call(image_bytes, media_type, system_prompt,
                         repo_root)
    if raw is None:
        raise RuntimeError(
            "Vision LLM unavailable — set GOOGLE_API_KEY / ANTHROPIC_API_KEY")

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
        "reference_used": parsed.get("reference_used"),
        "confidence": confidence,
        "plan": plan,
        "raw_response": raw,
    }


__all__ = ["image_to_plan"]
