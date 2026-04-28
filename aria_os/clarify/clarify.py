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
    # Match a number with a unit suffix. The leading lookbehind allows
    # box-notation like "80x60x40mm" — without it, the engine starts
    # looking from the unit and never anchors on the digit.
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s?(mm|cm|m|in|inch|inches|ft|"
    r"kg|lb|N|MPa|psi|°|deg|deg/s|rpm|V|A|W|h|s|min)\b",
    re.IGNORECASE,
)
# Box-notation: "80x60x40mm" → 3 dims. Counted separately because
# the unit only attaches to the LAST number; the simple regex above
# misses the first two of the three.
_BOX_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)"
    r"(?:\s*[xX×]\s*(\d+(?:\.\d+)?))?\s*"
    r"(mm|cm|m|in|inch|inches|ft)?",
    re.IGNORECASE,
)


def _count_dims(prompt: str) -> int:
    """Count distinct numeric dimensions in the prompt, including the
    box-notation case (`80x60x40mm` = 3 dims) the simple unit-anchored
    regex doesn't catch on its own."""
    n = len(_DIM_RE.findall(prompt))
    for m in _BOX_RE.finditer(prompt):
        # Each non-empty group adds a dim. Subtract 1 because the LAST
        # number gets double-counted by _DIM_RE if it has a unit suffix.
        groups = sum(1 for g in m.groups()[:3] if g)
        unit = m.group(4)
        if unit:
            groups -= 1   # last numeric is already in _DIM_RE
        n += max(0, groups)
    return n
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


_INTENT_RE = re.compile(
    # Phrases that signal the user actually told us what the part is FOR.
    # We're looking for purpose/use/function language — the thing that
    # determines tolerance bracket, finish, and material strategy more
    # than dimensions ever do.
    r"\b(for|to mount|to hold|to support|to attach|to carry|to connect|"
    r"to clamp|to resist|that holds|that supports|that mounts|that "
    r"carries|used (in|on|for|to)|use case|application|purpose|intended "
    r"to|will hold|will support|will carry|so that|in order to|"
    r"part of a|component of|inside an?|under load|loaded by|"
    r"sub-?assembly|fits (in|on|into)|attached to)\b",
    re.IGNORECASE,
)


_PCB_PART_RE = re.compile(
    # Detect PCB/ECAD prompts. These have their own domain signals
    # (chipset names, connector types, layer counts, voltages, currents)
    # that mean asking generic indoor/outdoor + load_case is noise.
    r"\b(esp32|esp8266|stm32|atmega|attiny|rp2040|teensy|arduino|"
    r"raspberry pi pico|tda\d+|drv\d+|rc522|nrf24|mpu\d+|lsm\d+|"
    r"buck|boost|ldo|smps|regulator|amplifier|amp|class[- ]d|"
    r"h-bridge|motor driver|breakout|usb-c|usb c|jst|microsd|"
    r"qspi|spi|i2c|uart|can bus|rs485|"
    r"\b\d+\s?layer\b|2-layer|4-layer|"
    r"pcb|board|netlist|gerber|footprint|silkscreen|copper|"
    r"\d+\s?mhz|\d+\s?ghz|\d+\s?vdc|\d+\s?vac)\b",
    re.IGNORECASE,
)


def _is_pcb_prompt(prompt: str) -> bool:
    """Heuristic: prompt looks like an ECAD/PCB request."""
    return bool(_PCB_PART_RE.search(prompt))


def _is_already_specific(prompt: str) -> bool:
    """Return True if the prompt carries enough design/manufacturing
    intent + purpose context that asking the LLM what to clarify would
    just be friction. Otherwise the LLM picks the highest-value
    questions for THIS prompt — we don't hardcode a fixed question set.
    """
    pl = prompt.lower()
    n_dims = _count_dims(prompt)
    has_intent = bool(_INTENT_RE.search(pl))
    has_mat = any(w in pl for w in _MATERIAL_WORDS)

    # PCB/ECAD prompts have their own specificity signals — chipset
    # names, layer counts, connector types — that imply intent.
    # "Material" doesn't apply to a PCB the way it does to a bracket.
    # If the prompt looks like ECAD AND has dims AND names a real
    # component or board class, treat it as specific enough.
    if _is_pcb_prompt(prompt) and n_dims >= 1:
        return True

    # Mechanical: dense numeric specification (3+ dims OR a part-class
    # word + 2 dims) is usually specific enough — a heat sink with
    # fin count + thickness + dims doesn't need anyone to ask "indoor
    # or outdoor" before we cut metal.
    if n_dims >= 3 and has_mat:
        return True

    # The original test: catches "L-bracket 80x60x40mm, 5mm wall, 4
    # M5 mounting holes" (no purpose, no material) and lets through
    # "aluminium L-bracket for mounting a camera to an outdoor wall".
    return has_intent and has_mat and n_dims >= 1


_LLM_SYSTEM = """You are an engineering project intake interviewer. The user
gives you a description of something they want built. Pick the 2-5 questions
that will most reduce design / manufacturing ambiguity for THIS specific
prompt — don't follow a fixed checklist.

Prioritise, in this order:
  1. PURPOSE / INTENT — what is this part FOR? what does it attach to,
     hold, mount, resist, or interact with? This is usually the highest
     leverage question, because it determines tolerance bracket, finish,
     fastener style, and material strategy more than dimensions ever do.
  2. MANUFACTURING INTENT — how should this be made (3D-printed, CNC'd,
     cast, sheet-metal, lathed)? Quantity? Production timeline? These
     drive geometry trade-offs (draft, fillets, undercuts, tolerances).
  3. OPERATING CONDITIONS — only when it materially changes the design
     (outdoor / submerged / aerospace / cleanroom). Skip when irrelevant.
  4. SPECIFIC GAPS in the prompt — anything ambiguous about geometry
     ("which face is the mounting face?"), material ("does it need to
     be conductive?"), or interfaces ("M5 bolts — which side of the
     bracket carries the through-hole?").

Each question MUST be answerable in <30 sec without research. Each MUST
materially change what gets built. Don't ask for things you can derive
from defaults (color, brand, surface roughness if not load-bearing).

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
  "ready": false  // true ONLY when purpose AND manufacturing intent
                  // AND operating conditions are ALL clearly stated
                  // OR derivable from the prompt without ambiguity.
}

If the user's prompt clearly states purpose + material + manufacturing
intent (e.g. "aluminium L-bracket for mounting a security camera to an
outdoor wall, CNC-machined, single prototype, with 4 M5 holes for the
camera base"), return {"questions": [], "ready": true}. Otherwise ask
the questions that close the biggest ambiguity gap for THIS prompt."""


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

    llm_questions: list[dict] = []
    llm_source = "baseline"
    llm_marked_ready = False
    if raw:
        # Strip ```json fences if present.
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.I)
        try:
            parsed = json.loads(cleaned)
            parsed_qs = parsed.get("questions") or []
            if isinstance(parsed_qs, list):
                llm_questions = [
                    q for q in parsed_qs
                    if isinstance(q, dict)
                       and q.get("id")
                       and q["id"] not in prior_answers
                ]
                llm_source = "llm"
                llm_marked_ready = bool(parsed.get("ready"))
        except json.JSONDecodeError:
            pass

    # If the LLM returned questions, run them. Otherwise — if it marked
    # ready=true OR was unreachable — we still need to defend against
    # the original failure mode where a dimensionally-explicit-but-
    # purposeless prompt slips through (e.g. "L-bracket 80x60x40mm 5mm
    # wall, 4 M5 holes" with no purpose / material / fab intent).
    # The defence: re-test specificity using the prompt + answers; if
    # it's still not specific by the intent+material+dim test in
    # `_is_already_specific`, fall through to the deterministic
    # baseline (which the LLM was supposed to do but sometimes won't).
    if llm_questions:
        return {
            "questions": llm_questions,
            "ready":     False,
            "source":    "llm",
        }
    if llm_marked_ready and _is_already_specific(composed):
        return {"questions": [], "ready": True, "source": "llm-confirmed"}

    # LLM unreachable / overconfident → deterministic baseline.
    qs = [q for q in _baseline_questions(composed)
           if q["id"] not in prior_answers]
    return {
        "questions": qs,
        "ready":     len(qs) == 0,
        "source":    "baseline",
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
