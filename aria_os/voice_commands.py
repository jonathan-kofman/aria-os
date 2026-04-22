"""
Voice command interpreter + design-intent annotation for ARIA-OS.

Two modes, both sit on top of the shared manufacturing_core.voice STT:

  1. Voice command → orchestrator action
     The operator says "regenerate the fillet with a 3mm radius" or "run
     the DFM agent on this part" or "export STEP for the bracket".
     `interpret_command(text)` returns a structured VoiceCommand dict the
     caller hands to the orchestrator. Unknown commands return
     `{"action": "unknown", "raw": "<transcript>"}` so the caller can
     decide whether to fall back to an LLM router.

  2. Design-intent annotation
     The operator speaks a note tied to a specific feature: "this face
     mates to the fixture, keep it flat". `annotate_part(part_path, text)`
     appends the note to a sidecar `<part>.intent.json` with a timestamp
     and (optional) feature_ref so downstream agents (DFM, CAM, drawing
     generator) can read design intent that otherwise gets lost between
     CAD and CAM.

Both functions return structured dicts. The CLI wiring lives in
run_aria_os.py under the `--voice-cmd` and `--voice-note` flags.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Command interpretation — keyword/phrase-first, fall through to LLM later
# ---------------------------------------------------------------------------

# Verb → canonical action. Longest phrases first so "run the DFM agent" wins
# over plain "run".
_VERB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bregenerate\b", re.I),                     "regenerate"),
    (re.compile(r"\b(re)?run\s+(?:the\s+)?dfm(?:\s+agent)?\b", re.I), "run_dfm"),
    (re.compile(r"\brun\s+(?:the\s+)?fea\b", re.I),            "run_fea"),
    (re.compile(r"\brun\s+(?:the\s+)?cfd\b", re.I),            "run_cfd"),
    (re.compile(r"\brun\s+(?:the\s+)?quote\b", re.I),          "run_quote"),
    (re.compile(r"\brun\s+(?:the\s+)?cam\b", re.I),            "run_cam"),
    (re.compile(r"\bexport\s+(?:to\s+)?step\b", re.I),         "export_step"),
    (re.compile(r"\bexport\s+(?:to\s+)?stl\b", re.I),          "export_stl"),
    (re.compile(r"\bexport\s+(?:to\s+)?dxf\b", re.I),          "export_dxf"),
    (re.compile(r"\b(generate|build|make|create)\s+(?:a\s+|the\s+)?drawing\b", re.I), "generate_drawing"),
    (re.compile(r"\b(modify|change|update)\b", re.I),          "modify"),
    (re.compile(r"\bshow\b|\bopen\b|\bview\b", re.I),          "view"),
    (re.compile(r"\bcancel\b|\bstop\b|\babort\b", re.I),       "cancel"),
]

# Parameter extractors — "3mm radius", "5 mm", "R5", "fillet", etc.
_RE_RADIUS = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:radius|fillet|R)\b", re.I)
_RE_DIM_MM = re.compile(r"\b(\d+(?:\.\d+)?)\s*mm\b", re.I)
_RE_FEATURE = re.compile(
    r"\b(fillet|chamfer|hole|bore|pocket|boss|rib|flange|slot|thread)\b", re.I)
_RE_TARGET = re.compile(
    r"\b(?:on\s+)?(?:the\s+|this\s+)?([a-z_][a-z0-9_\-]{2,})\b", re.I)


def interpret_command(text: str) -> dict[str, Any]:
    """Parse a voice transcript into a structured command dict.

    Returns something like:
      {"action": "regenerate", "feature": "fillet", "radius_mm": 3.0, "raw": text}
      {"action": "run_dfm",    "raw": text}
      {"action": "export_step","raw": text}
      {"action": "unknown",    "raw": text}
    """
    if not text or not text.strip():
        return {"action": "noop", "raw": text}

    action = "unknown"
    for pat, a in _VERB_PATTERNS:
        if pat.search(text):
            action = a
            break

    out: dict[str, Any] = {"action": action, "raw": text.strip()}

    fm = _RE_FEATURE.search(text)
    if fm:
        out["feature"] = fm.group(1).lower()

    rm = _RE_RADIUS.search(text)
    if rm:
        out["radius_mm"] = float(rm.group(1))
    else:
        dm = _RE_DIM_MM.search(text)
        if dm and action in ("modify", "regenerate"):
            out["dimension_mm"] = float(dm.group(1))

    # Let the caller decide what to do with "unknown" — most often it's an
    # LLM-routed command. Do not try to fabricate intent here.
    return out


# ---------------------------------------------------------------------------
# Design-intent annotation
# ---------------------------------------------------------------------------

def annotate_part(part_path: str | Path, text: str,
                  *, feature_ref: str | None = None,
                  user: str | None = None) -> dict[str, Any]:
    """Attach a spoken design-intent note to a part file. Writes a sidecar
    `<part>.intent.json` (created if missing) with an append-only log so
    downstream agents (DFM / CAM / drawing) can read the full intent history
    for this geometry.

    `feature_ref` is optional — it can be any string the caller wants to
    pin the note to (face tag, hole ID, etc.). Downstream agents inspect
    the field but don't require it.

    Returns the full sidecar dict after the append. Safe to call for
    nonexistent parts: the sidecar is placed next to the expected path.
    """
    part_path = Path(part_path)
    sidecar = part_path.with_suffix(part_path.suffix + ".intent.json")

    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"notes": []}
        except Exception:
            data = {"notes": []}
    else:
        data = {"part_path": str(part_path), "notes": []}

    if "notes" not in data or not isinstance(data["notes"], list):
        data["notes"] = []

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "text": text.strip(),
    }
    if feature_ref:
        entry["feature_ref"] = feature_ref
    if user:
        entry["user"] = user
    data["notes"].append(entry)

    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
