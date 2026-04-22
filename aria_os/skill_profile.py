"""
SkillProfile — adapt the whole pipeline to the operator's skill level.

The existing `TeachingEngine` (manufacturing-core) has three tiers that
only affect narration. This module widens the control surface so EVERY
stage — input handling, defaults, validation strictness, output verbosity,
error phrasing — adapts to one of four tiers:

  novice        first CAD session; explain each step; safe defaults; plain English
  intermediate  knows CAD basics; shows decisions; suggests alternatives
  advanced      fluent user; speed over narration; shows spec tables + code
  veteran       decades of experience; raw output, all knobs exposed, bypass safety nudges

Auto-detection from goal text (no user profile needed to start):
  - Count technical terms (LQFP, PCD, GD&T, Y14.5, ISO 2768, M6, σ_yield, Ra, SF …)
  - Density of numeric dims with units
  - Presence of acronym-heavy product names (STM32F405RGT6, AMS1117-3.3, …)
  - Absence of filler ("I want a", "can you make me") → higher skill

The auto-detected level is a HINT only — CLI `--skill` flag wins, then
persisted UserProfile wins, then auto-detect.

Per-stage adapters
------------------
Every agent / formatter that touches the user can import:

    from aria_os.skill_profile import SkillProfile, SkillLevel

    profile = SkillProfile.from_context(state)   # reads state.skill_level
    if profile.level is SkillLevel.NOVICE:
        ...
    output = profile.format_summary(result)
    print(profile.format_error(exc))

Defaults-per-tier (summary):
  novice        strict validation, all-param LLM autocompletion, plain-English errors,
                 progress emoji, short-summary, refuses risky ops without confirmation
  intermediate  permissive validation with warnings, LLM fills only required params,
                 balanced output, code preview toggle
  advanced      minimal hand-holding, shows spec table + CadQuery preview + validation
                 table, flags visible, no progress emoji
  veteran       raw mode: streams agent-loop internals, dumps LLM prompts on request,
                 exposes every internal metric, skips safety prompts

Persistence
-----------
SkillProfile serializes/deserializes via manufacturing_core.UserProfile when
available; falls back to a local JSON sidecar at ~/.aria_os/skill_profile.json
so this module never hard-requires manufacturing-core.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SkillLevel(str, Enum):
    """Four-tier skill classification. String values so they round-trip
    cleanly through JSON, argparse, and CLI env vars."""
    NOVICE       = "novice"
    INTERMEDIATE = "intermediate"
    ADVANCED     = "advanced"
    VETERAN      = "veteran"


# ---------------------------------------------------------------------------
# Auto-detection heuristics
# ---------------------------------------------------------------------------

# Terms that signal familiarity with the domain. Keeping this small and
# curated — false positives (matching a common word in a novice's prompt)
# would push them up a tier they don't deserve.
_TECH_TERMS = frozenset([
    # mechanical / GD&T
    "gd&t", "y14.5", "iso 2768", "iso 286", "astm", "asme", "ansi",
    "flatness", "parallelism", "perpendicularity", "runout", "cylindricity",
    "fitment", "press fit", "interference", "clearance fit",
    "tolerance zone", "feature control frame", "datum reference",
    "surface finish", "ra ", "rz ", "µm",
    "shear", "bending moment", "buckling", "yield strength", "ultimate tensile",
    "fatigue", "s-n curve", "safety factor", "sf=", "sf ",
    "pcd", "bolt circle", "concentric", "coaxial",
    "lqfp", "qfn", "bga", "tqfp", "sot-223",
    # FEA / CFD
    "fea", "cfd", "modal", "eigenfrequency", "harmonic response",
    "von mises", "principal stress", "displacement field",
    # ECAD
    "impedance", "differential pair", "controlled impedance",
    "microstrip", "stripline", "ground pour", "via stitching",
    "decoupling", "bypass cap", "bulk cap",
    # CAM
    "chipload", "surface feet per minute", "sfm", "hss", "carbide",
    "climb mill", "conventional mill", "stepover", "stepdown",
    "work offset", "g54", "g90", "g91",
    # materials
    "6061", "7075", "4140", "304", "316", "ptfe", "peek", "delrin",
    "anneal", "temper", "harden", "chromate", "anodize",
    # units abbreviations
    "mpa", "gpa", "ksi", "psi", "n·m", "n-m", "lb-ft", "lbf",
])

_ACRONYM_RE   = re.compile(r"\b[A-Z]{3,}\d?\b")
_PART_CODE_RE = re.compile(r"\b[A-Z]{2,}\d{2,}[A-Z0-9\-]*\b")
_DIM_RE       = re.compile(r"\b\d+(?:\.\d+)?\s*(mm|cm|in|inch|inches|µm|um|°|deg)\b", re.I)
_FILLER_RE    = re.compile(
    r"\b(i want|can you|please make|i need|hey|hi|build me|make me)\b", re.I)


def detect_skill_from_prompt(text: str) -> SkillLevel:
    """Heuristic skill estimate from a single prompt. Returns INTERMEDIATE
    when nothing tips the scale. Never raises.

    Signals (each shifts score by a little; score drives the final bucket):
      + tech term mention (1 each, max 4)
      + acronym density (0.5 each)
      + part-code presence (+2 for things like STM32F405RGTx)
      + numeric dims with units (0.25 each)
      - filler phrases ("i want a bracket") subtract
    """
    if not text or not text.strip():
        return SkillLevel.INTERMEDIATE
    t = text.lower()
    score = 0.0

    # tech terms — cap at 4 so a goal packed with jargon doesn't runaway
    n_tech = sum(1 for term in _TECH_TERMS if term in t)
    score += min(4, n_tech)

    # acronyms (STM, PCB, USB) but filter out common small ones from tech_terms
    acronyms = _ACRONYM_RE.findall(text)
    score += 0.5 * min(6, len(set(acronyms)))

    # part codes like STM32F405RGT6
    n_parts = len(_PART_CODE_RE.findall(text))
    score += 2.0 * min(2, n_parts)

    # numeric dims
    n_dims = len(_DIM_RE.findall(text))
    score += 0.25 * min(10, n_dims)

    # filler language docks the score
    n_filler = len(_FILLER_RE.findall(t))
    score -= 1.5 * min(2, n_filler)

    # word-count heuristic: a 3-word "a steel bracket" is probably novice
    words = len(text.split())
    if words < 5:
        score -= 1.0

    if score <= 0:
        return SkillLevel.NOVICE
    if score < 3:
        return SkillLevel.INTERMEDIATE
    if score < 6:
        return SkillLevel.ADVANCED
    return SkillLevel.VETERAN


# ---------------------------------------------------------------------------
# Profile dataclass + adapters
# ---------------------------------------------------------------------------

@dataclass
class SkillProfile:
    """Carries the active skill level + a bag of per-tier knobs the rest
    of the pipeline can read. Mutable — a `--teach` flag can upgrade
    narration without changing the level."""
    level: SkillLevel = SkillLevel.INTERMEDIATE
    source: str = "default"   # "cli" | "auto" | "persisted" | "default"
    explain_decisions: bool = False
    wait_for_confirm_on_risk: bool = True
    max_llm_autocomplete_params: int = 3
    show_code_preview: bool = False
    show_spec_table: bool = True
    show_raw_llm: bool = False
    strict_validation: bool = True
    persist_to_user_profile: bool = True

    @classmethod
    def for_level(cls, level: SkillLevel, *, source: str = "default") -> "SkillProfile":
        """Build a profile with the canonical knob set for a given level."""
        defaults = _TIER_DEFAULTS[level]
        return cls(level=level, source=source, **defaults)

    @classmethod
    def from_context(cls, goal: str = "", *,
                     cli_override: SkillLevel | None = None,
                     persisted: SkillLevel | None = None) -> "SkillProfile":
        """Resolve the active profile with priority: CLI > persisted > auto-detect.
        Use this as the single entry point so every caller picks up the same
        resolution rules."""
        if cli_override is not None:
            return cls.for_level(cli_override, source="cli")
        if persisted is not None:
            return cls.for_level(persisted, source="persisted")
        return cls.for_level(detect_skill_from_prompt(goal), source="auto")

    # ------------------------------------------------------------------
    # Adapters — formatters that every stage can call
    # ------------------------------------------------------------------

    def format_summary(self, result: dict) -> str:
        """Adapt the final pipeline summary to the operator's skill level."""
        name = result.get("part_id") or result.get("title") or "part"
        ok = result.get("passed", result.get("ok", False))
        bbox = result.get("bbox") or result.get("bbox_mm") or []
        material = result.get("material") or "—"

        if self.level is SkillLevel.NOVICE:
            mark = "[done]" if ok else "[didn't finish]"
            dims = (f" ({bbox[0]:.0f}×{bbox[1]:.0f}×{bbox[2]:.0f} mm)"
                    if bbox else "")
            return (f"{mark}: {name}{dims}\n"
                    f"material: {material}\n"
                    f"open the .step file in your CAD tool to see it.")

        if self.level is SkillLevel.INTERMEDIATE:
            mark = "PASS" if ok else "FAIL"
            line1 = f"{mark} {name}"
            if bbox:
                line1 += f"  bbox {bbox[0]:.1f}×{bbox[1]:.1f}×{bbox[2]:.1f} mm"
            line2 = f"material={material}  routing={result.get('cad_tool', 'cadquery')}"
            return f"{line1}\n{line2}"

        # advanced + veteran get a spec table
        lines: list[str] = []
        lines.append(f"[{self.level.value}] {name}  -> {'PASS' if ok else 'FAIL'}")
        if bbox:
            lines.append(f"bbox_mm = {bbox[0]:.3f} × {bbox[1]:.3f} × {bbox[2]:.3f}")
        for k in ("material", "cad_tool", "part_id", "session_id",
                  "run_id", "n_iterations", "visual_confidence",
                  "llm_calls"):
            v = result.get(k)
            if v is not None:
                lines.append(f"{k} = {v}")
        if self.level is SkillLevel.VETERAN:
            # Dump absolutely everything
            for k, v in sorted(result.items()):
                if k in ("bbox", "bbox_mm", "material", "cad_tool",
                         "part_id", "session_id", "run_id", "n_iterations",
                         "visual_confidence", "llm_calls", "passed", "ok",
                         "title"): continue
                if isinstance(v, (dict, list)):
                    v = json.dumps(v)[:140]
                lines.append(f"{k} = {v}")
        return "\n".join(lines)

    def format_error(self, exc: BaseException | str,
                     *, hint: str | None = None) -> str:
        """Translate a pipeline error to the operator's skill level."""
        msg = str(exc) if not isinstance(exc, str) else exc
        tname = type(exc).__name__ if isinstance(exc, BaseException) else "error"

        if self.level is SkillLevel.NOVICE:
            plain = _NOVICE_ERROR_DICT.get(tname)
            if plain is None:
                plain = _guess_novice_phrasing(msg)
            suggestion = hint or _NOVICE_SUGGESTIONS.get(tname,
                "try simpler dimensions or let the assistant pick defaults.")
            return f"something didn't work.\n\n{plain}\n\nwhat to try: {suggestion}"

        if self.level is SkillLevel.INTERMEDIATE:
            head = f"{tname}: {msg}"
            if hint: head += f"\n  -> {hint}"
            return head

        # advanced + veteran get stack context
        import traceback
        head = f"[{tname}] {msg}"
        if hint: head += f"\nhint: {hint}"
        if self.level is SkillLevel.VETERAN and isinstance(exc, BaseException):
            head += "\n" + "".join(traceback.format_tb(exc.__traceback__)[-4:])
        return head

    def trim_autocompletions(self, params_needed: dict,
                             current: dict) -> dict:
        """Decide how much the LLM should autocomplete the goal's spec."""
        missing = [k for k in params_needed if k not in current]
        if not missing:
            return {}
        # Novices want the system to fill everything with sensible defaults.
        # Veterans want to fill nothing automatically — they'll supply it.
        cap = self.max_llm_autocomplete_params
        return {k: None for k in missing[:cap]}

    def should_block_on_validation_failure(self, severity: str) -> bool:
        """Call from the eval agent. Returns True if the failure should
        block progress (vs warn-and-continue)."""
        if self.level is SkillLevel.VETERAN:
            return False  # always warn, never block
        if self.level is SkillLevel.ADVANCED:
            return severity == "critical"
        if self.level is SkillLevel.INTERMEDIATE:
            return severity in ("critical", "error")
        # novice — any failure blocks so we don't let them ship broken parts
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist(self, user_id: str = "default") -> None:
        """Save the level to UserProfile if manufacturing_core is available,
        else to a local JSON sidecar. Never raises."""
        if not self.persist_to_user_profile:
            return
        try:
            from manufacturing_core.profile import UserProfile
            up = UserProfile.load(user_id)
            try:
                up.skill_level = self.level.value  # type: ignore[attr-defined]
                up.save()
                return
            except Exception:
                pass  # UserProfile schema doesn't include skill_level yet
        except Exception:
            pass
        # Fallback: local sidecar
        try:
            d = Path.home() / ".aria_os"
            d.mkdir(parents=True, exist_ok=True)
            (d / "skill_profile.json").write_text(
                json.dumps({"user_id": user_id, "level": self.level.value}),
                encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def load_persisted(cls, user_id: str = "default") -> SkillLevel | None:
        """Return the last persisted level, or None. Never raises."""
        try:
            from manufacturing_core.profile import UserProfile
            up = UserProfile.load(user_id)
            lv = getattr(up, "skill_level", None)
            if lv:
                return SkillLevel(lv)
        except Exception:
            pass
        try:
            p = Path.home() / ".aria_os" / "skill_profile.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                lv = data.get("level")
                if lv:
                    return SkillLevel(lv)
        except Exception:
            pass
        return None


# Canonical knob values per tier. Tweaking these propagates to everyone
# via SkillProfile.for_level().
_TIER_DEFAULTS: dict[SkillLevel, dict[str, Any]] = {
    SkillLevel.NOVICE: dict(
        explain_decisions=True,
        wait_for_confirm_on_risk=True,
        max_llm_autocomplete_params=8,   # fill liberally
        show_code_preview=False,
        show_spec_table=False,
        show_raw_llm=False,
        strict_validation=True,
    ),
    SkillLevel.INTERMEDIATE: dict(
        explain_decisions=False,
        wait_for_confirm_on_risk=True,
        max_llm_autocomplete_params=4,
        show_code_preview=False,
        show_spec_table=True,
        show_raw_llm=False,
        strict_validation=True,
    ),
    SkillLevel.ADVANCED: dict(
        explain_decisions=False,
        wait_for_confirm_on_risk=False,
        max_llm_autocomplete_params=2,
        show_code_preview=True,
        show_spec_table=True,
        show_raw_llm=False,
        strict_validation=False,
    ),
    SkillLevel.VETERAN: dict(
        explain_decisions=False,
        wait_for_confirm_on_risk=False,
        max_llm_autocomplete_params=0,    # fill nothing — they'll supply all
        show_code_preview=True,
        show_spec_table=True,
        show_raw_llm=True,
        strict_validation=False,
    ),
}


# ---------------------------------------------------------------------------
# Novice-error phrasings — a small lookup that translates stacktrace names
# to actionable plain English. Grow over time.
# ---------------------------------------------------------------------------

_NOVICE_ERROR_DICT = {
    "UnicodeEncodeError":
        "the text contained a character the output can't print (usually a fancy dash or box character).",
    "ValueError":
        "one of the numbers in the description didn't make sense.",
    "FileNotFoundError":
        "the system couldn't find a file it needed.",
    "TimeoutError":
        "a step took longer than expected and was cut off.",
    "ConnectionError":
        "the system couldn't reach one of its helpers (usually the LLM or Ollama).",
    "KeyError":
        "one of the expected fields was missing from the spec.",
    "RuntimeError":
        "the pipeline hit an internal snag.",
}

_NOVICE_SUGGESTIONS = {
    "ValueError": "check that every dimension has units (mm or inches) and is a single number.",
    "FileNotFoundError": "check the file path exists and try again.",
    "TimeoutError": "try again — if it keeps timing out, describe a simpler shape.",
    "ConnectionError": "make sure your internet is up (for cloud models) and try again.",
    "KeyError": "add any missing dimensions — width, height, depth, thickness — to your description.",
}


def _guess_novice_phrasing(msg: str) -> str:
    m = msg.lower()
    if "cadquery" in m or "step" in m or "stl" in m:
        return "the 3D model didn't finish building — often because the numbers don't fit together."
    if "llm" in m or "api" in m or "anthropic" in m or "gemini" in m:
        return "the AI helper wasn't available just now."
    if "footprint" in m or "pcb" in m or "net" in m:
        return "the circuit board step ran into an issue."
    return "the pipeline hit a snag — see the details in the log."
