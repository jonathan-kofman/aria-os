"""Clarifying-questions layer (task #79).

Before any pipeline kicks off, ensure the user's prompt is
narrowed enough to produce a usable result. Inside or outside?
What load case? Indoor/outdoor temperature range? Material
constraints? Budget / lead time?

Flow (matches the chat-loop pattern):

    1. User submits a vague prompt: "build me a drone frame"
    2. POST /api/clarify {prompt} →
         {"questions": [
            {"id": "environment", "label": "Indoor or outdoor?",
             "options": ["indoor", "outdoor", "both"], "required": true},
            {"id": "payload",     "label": "Payload weight?", ...},
            ...],
          "ready": false}
    3. User answers → POST /api/clarify {prompt, answers: {...}} →
         {"prompt_final": "<original> + indoor + 250g payload + ...",
          "questions": [],
          "ready": true}
    4. Caller passes `prompt_final` to /api/system/full-build.

Per the autonomy-first rule (memory: feedback_autonomy_first.md), the
clarifier never asks the user to re-explain — it asks ONLY the questions
needed to disambiguate this specific prompt, returns immediately when
the prompt is already specific enough (manyDimensional values + clear
constraints), and merges previously-answered questions into the assembled
final prompt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Heuristic: prompts already carrying ≥3 numeric dimensions, an explicit
# environment word, and a material almost never need clarification — the
# planner already has enough to route + size geometry. Skip the LLM call
# in that case to save round-trips.
_DIM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(mm|cm|m|in|inch|inches|ft|kg|lb|N|MPa|psi|°|deg|deg/s|rpm|V|A|W|h|s|min)\b",
    re.IGNORECASE,
)
_ENV_WORDS = {
    "indoor", "outdoor", "subsea", "underwater", "marine", "aerospace",
    "vacuum", "in-space", "in space", "orbit", "outer space",
    "cleanroom", "kitchen", "workshop", "garage", "warehouse",
    "sub-zero", "high-temp", "high temperature", "low temperature",
    "tropical", "arctic", "desert", "wet", "dry",
}
_MATERIAL_WORDS = {
    "aluminium", "aluminum", "steel", "stainless", "titanium", "carbon",
    "carbon fibre", "carbon fiber", "abs", "pla", "petg", "nylon",
    "fr4", "fr-4", "polycarbonate", "polypropylene", "ptfe", "delrin",
    "wood", "concrete", "brass", "copper", "bronze", "magnesium",
    "g10", "ultem", "hdpe", "ldpe", "kevlar", "fiberglass",
}


def _baseline_questions(prompt: str) -> list[dict[str, Any]]:
    """Deterministic fallback question set — used when no LLM is
    reachable. Always asks about environment + load case, since those
    are the two axes that most often come back wrong without an
    explicit answer."""
    pl = prompt.lower()
    qs: list[dict[str, Any]] = []

    # Environment is almost always relevant — outdoor implies UV/water/
    # corrosion/temp swings, indoor narrows tolerances. We ask unless
    # the prompt already mentions an explicit environment word.
    if not any(w in pl for w in _ENV_WORDS):
        qs.append({
            "id": "environment",
            "label": "Will this be used indoors, outdoors, or both?",
            "options": ["indoor", "outdoor", "both", "subsea / marine",
                          "in-space / vacuum"],
            "required": True,
            "hint": "Drives material choice (UV, corrosion, IP rating) "
                     "and tolerance bracket.",
        })

    # Material — only ask if not specified.
    if not any(w in pl for w in _MATERIAL_WORDS):
        qs.append({
            "id": "material",
            "label": "Preferred material (or any constraints)?",
            "options": ["aluminium 6061", "stainless steel 304",
                          "carbon fibre", "ABS / PLA (3D-printed)",
                          "titanium", "no preference"],
            "required": False,
            "hint": "Skipping defers material to the planner's "
                     "domain heuristic.",
        })

    # Load case — only ask if "load" / "force" / "weight" not present.
    if not re.search(r"\b(load|force|weight|mass|carry|payload)\b", pl):
        qs.append({
            "id": "load_case",
            "label": "What load or weight does it need to handle?",
            "options": ["nothing structural (decorative)",
                          "<1 kg / handheld",
                          "1-10 kg / desktop",
                          "10-100 kg / industrial",
                          "100+ kg / structural"],
            "required": False,
            "hint": "Used to size cross-section + run FEA target.",
        })

    # Quantity — single piece vs production matters for DFM + tolerances.
    if not re.search(r"\b(qty|quantity|production|prototype|one-off|"
                       r"single piece|batch)\b", pl):
        qs.append({
            "id": "quantity",
            "label": "Single prototype or production quantity?",
            "options": ["one-off prototype", "10-100 batch",
                          "100-1k production", "1k+ mass production"],
            "required": False,
            "hint": "Drives the CAM router (3D-print → CNC → mold).",
        })

    # Lead time — affects fab method.
    if not re.search(r"\b(deadline|by next|next week|asap|urgent|"
                       r"days|weeks|months)\b", pl):
        qs.append({
            "id": "lead_time",
            "label": "When do you need it?",
            "options": ["this week", "this month", "this quarter",
                          "no rush"],
            "required": False,
            "hint": "Constrains 'desktop 3D-printable' vs "
                     "'machined-from-billet'.",
        })

    return qs


def _is_already_specific(prompt: str) -> bool:
    """Return True if the prompt carries enough explicit constraints
    that asking would just be friction."""
    n_dims = len(_DIM_RE.findall(prompt))
    pl = prompt.lower()
    has_env = any(w in pl for w in _ENV_WORDS)
    has_mat = any(w in pl for w in _MATERIAL_WORDS)
    # Lenient: 2+ dimensions when BOTH env and material are explicit
    # → the user is clearly technical and the prompt covers the axes
    # that most often need clarification.
    if n_dims >= 2 and has_env and has_mat:
        return True
    # 3+ dimensions + at least one of (env, material) → specific enough.
    if n_dims >= 3 and (has_env or has_mat):
        return True
    # 5+ dimensions on its own → specific enough (the user is clearly
    # technical, planner can fill in defaults).
    if n_dims >= 5:
        return True
    return False


_LLM_SYSTEM = """You are an engineering project intake interviewer. The user
gives you a vague description of something they want built. Return a STRICT
JSON object asking the smallest set of questions needed to fully constrain
the design (3-6 questions max). Each question MUST be answerable in <30 sec
without research. Always include an environment/operating-conditions question
if the answer would change material, IP rating, or tolerance bracket. Never
ask the user for things you can derive from defaults (e.g. "what color?"
or "what brand?"). Always cover, when relevant: indoor/outdoor, load case,
material constraint, quantity, lead time, regulatory/safety scope.

Respond ONLY in this JSON format (no markdown, no preamble):

{
  "questions": [
    {
      "id": "<short snake_case id>",
      "label": "<question text>",
      "options": ["<choice 1>", "<choice 2>", ...],   // optional
      "required": true|false,
      "hint": "<why this matters in <=80 chars>"
    },
    ...
  ],
  "ready": false  // true ONLY when the prompt carries no real ambiguity
}

If the user's prompt already specifies enough (3+ numeric dims + env +
material, or otherwise unambiguous), return {"questions": [], "ready": true}."""


def get_clarifying_questions(prompt: str,
                              *, repo_root: Path | None = None,
                              prior_answers: dict | None = None
                              ) -> dict:
    """Return {"questions": [...], "ready": bool, "source": str}.

    `prior_answers` lets the caller include answered questions so the
    LLM doesn't ask them again — the assembled final prompt
    (`assemble_final_prompt`) is what gets sent into the build pipeline.
    """
    prior_answers = prior_answers or {}

    # Fast-path: the prompt + answers together cover the bases →
    # nothing more to ask.
    composed = prompt
    if prior_answers:
        composed = f"{prompt}\n\nClarifications: " \
            + ", ".join(f"{k}={v}" for k, v in prior_answers.items())
    if _is_already_specific(composed):
        return {"questions": [], "ready": True, "source": "fast-path"}

    # LLM path. Use 'fast' tier — clarification is short, cheap.
    user_msg = f"User prompt: {prompt!r}"
    if prior_answers:
        user_msg += "\n\nAlready answered:\n" + "\n".join(
            f"  {k}: {v}" for k, v in prior_answers.items())
    user_msg += ("\n\nReturn only the JSON object described in your "
                  "system prompt. No preamble.")

    try:
        from aria_os.llm_client import call_llm
    except Exception:
        call_llm = None  # type: ignore

    raw = None
    if call_llm is not None:
        try:
            raw = call_llm(user_msg, _LLM_SYSTEM,
                            repo_root=repo_root, quality="fast")
        except Exception:
            raw = None

    if raw:
        # Strip ```json fences if present.
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.I)
        try:
            parsed = json.loads(cleaned)
            qs = parsed.get("questions") or []
            if isinstance(qs, list):
                # Filter out any question whose id was already answered
                # — defends against the LLM asking the same question
                # that's in `prior_answers`.
                qs = [q for q in qs
                       if isinstance(q, dict)
                          and q.get("id")
                          and q["id"] not in prior_answers]
                ready = bool(parsed.get("ready")) or len(qs) == 0
                return {"questions": qs, "ready": ready, "source": "llm"}
        except json.JSONDecodeError:
            pass

    # LLM unavailable / unparseable → deterministic baseline.
    qs = [q for q in _baseline_questions(prompt)
           if q["id"] not in prior_answers]
    return {
        "questions": qs,
        "ready": len(qs) == 0,
        "source": "baseline",
    }


def assemble_final_prompt(prompt: str, answers: dict | None) -> str:
    """Merge the user's answers into a single prompt string the planner
    can ingest directly. Uses inline phrasing instead of a separate
    'context' block so existing planner regexes still trigger."""
    if not answers:
        return prompt
    parts = [prompt.strip().rstrip(".")]
    # Translate well-known IDs into engineering-flavoured prose so the
    # spec_extractor can pick them up via existing keyword scanners.
    if (env := answers.get("environment")):
        parts.append(f"for {env} use")
    if (mat := answers.get("material")) and mat != "no preference":
        parts.append(f"in {mat}")
    if (load := answers.get("load_case")) and load.startswith("nothing") is False:
        parts.append(f"loaded {load}")
    if (qty := answers.get("quantity")):
        parts.append(f"({qty})")
    if (lt := answers.get("lead_time")):
        parts.append(f"needed {lt}")
    # Any other answers — passed through as "key: value, ..." so the
    # LLM-driven branches still see them.
    seen = {"environment", "material", "load_case", "quantity",
             "lead_time"}
    extras = {k: v for k, v in answers.items() if k not in seen and v}
    if extras:
        parts.append("("
                      + ", ".join(f"{k}: {v}" for k, v in extras.items())
                      + ")")
    return ". ".join(p.strip() for p in parts if p.strip())


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "build me a drone frame"
    r = get_clarifying_questions(p)
    print(json.dumps(r, indent=2))
    if r["questions"]:
        print(f"\n{len(r['questions'])} clarifying question(s) for: {p!r}")
