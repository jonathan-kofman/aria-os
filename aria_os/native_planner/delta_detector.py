"""Delta / continuation prompt detection.

Given a prompt + the current Fusion design's state, decide whether
the user wants to:

  A. Create a NEW part from scratch (normal flow)
  B. MODIFY an existing ARIA-generated part (edit User Parameters)
  C. EXTEND an existing part with new features (sketch+extrude on top)

Delta detection runs BEFORE the planner so the planner can emit the
right kind of plan:

  A → full plan starting with `beginPlan` + `addParameter`s + sketches/extrudes
  B → minimal plan of `addParameter` updates (existing params get new values)
  C → plan of new sketches/extrudes that target existing geometry (skip beginPlan)

The detector uses a mix of heuristics and LLM disambiguation. Heuristics
catch the obvious cases cheaply; LLM handles ambiguity.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..llm_client import call_llm


# Phrases that strongly imply MODIFY (change existing dims)
_MODIFY_PHRASES = [
    r"\bmake it (?:thicker|thinner|taller|shorter|bigger|smaller|wider|narrower|deeper|longer)\b",
    r"\bchange (?:the )?(?:od|bore|thickness|diameter|height|width|depth|length)\b",
    r"\b(?:set|update) (?:the )?(\w+) to \d",
    r"\bincrease (?:the )?\w+ by \d",
    r"\bdecrease (?:the )?\w+ by \d",
    r"\bmake (?:the )?\w+ \d+\s*mm\b",
    r"\b(?:bigger|smaller|thicker|thinner)$",
]

# Phrases that strongly imply EXTEND (add new features)
_EXTEND_PHRASES = [
    r"\badd (?:a |an |another |some |\d+ )?",
    r"\bput (?:a |an |another |some )?",
    r"\bdrill (?:a |an |another |some |\d+ )?",
    r"\bfillet\b",
    r"\bchamfer\b",
    r"\bmirror\b",
    r"\bpocket\b",
]

# Phrases that strongly imply NEW (override existing design)
_NEW_PHRASES = [
    r"\bnew\s+(flange|bracket|gear|impeller|plate|shaft|housing)",
    r"\b(?:design|make|create|generate) (?:a |an )",
    r"^(flange|bracket|gear|impeller|plate|shaft|housing)\s+\d",
]


def _has_aria_params(user_parameters: list[dict]) -> bool:
    """True if the active design has parameters that were declared by
    ARIA (they use a `<part>_<dim>` naming convention)."""
    prefixes = ("flange_", "sm_", "bracket_", "gear_", "impeller_",
                "plate_", "shaft_")
    return any(
        (p.get("name") or "").startswith(prefixes)
        for p in (user_parameters or []))


def _classify_heuristic(goal: str, host_context: dict | None) -> str | None:
    """Cheap regex-based classification. Returns 'new'|'modify'|'extend'
    or None if uncertain (caller falls back to LLM)."""
    g = (goal or "").lower().strip()
    if not g:
        return None
    params = (host_context or {}).get("user_parameters") or []
    has_aria = _has_aria_params(params)

    # If no ARIA design is active, treat as new
    if not has_aria:
        return "new"

    # Heuristic scan FIRST — if we can classify based on keyword
    # patterns alone, don't burn an LLM call.
    if any(re.search(p, g) for p in _NEW_PHRASES):
        return "new"
    if any(re.search(p, g) for p in _MODIFY_PHRASES):
        return "modify"
    if any(re.search(p, g) for p in _EXTEND_PHRASES):
        return "extend"

    # Has dimensions (mm/cm/inch/NxN) + no modify/extend phrases →
    # clearly a NEW part spec. Catches "gas manifold 50mm x 25mm"
    # without needing an LLM call.
    has_dim = bool(re.search(
        r"\d+\s*(?:mm|cm|inch|in|m\b|\"|'|×|x\s*\d)", g))
    if has_dim:
        return "new"

    # Fast path: prompt hits a hardcoded planner keyword AND has at
    # least one numeric dimension → clearly a NEW part spec.
    try:
        from .dispatcher import is_supported as _is_hc
        if _is_hc(goal):
            return "new"
    except Exception:
        pass
    # Short prompts (<40 chars) with an existing design usually mean delta
    if len(g) < 40:
        return "modify"
    return None


def _classify_llm(goal: str, host_context: dict | None,
                   *, quality: str = "fast",
                   repo_root: Path | None = None) -> str:
    """LLM fallback when heuristics are uncertain. Kept cheap with the
    fast tier since classification is a simple yes/no/yes task."""
    params = (host_context or {}).get("user_parameters") or []
    param_summary = ", ".join(
        f"{p['name']}={p.get('expression', '?')}"
        for p in params[:10]) or "(no parameters)"
    tree = (host_context or {}).get("feature_tree") or {}
    tree_summary = ""
    if isinstance(tree, dict):
        feats = tree.get("features") or []
        if isinstance(feats, list):
            tree_summary = ", ".join(str(f)[:40] for f in feats[:8])

    prompt = (
        f"User prompt: {goal!r}\n"
        f"Current Fusion design state:\n"
        f"  User parameters: {param_summary}\n"
        f"  Feature tree: {tree_summary or '(empty)'}\n\n"
        "Classify the prompt as exactly ONE of:\n"
        "  new    — user wants a brand new part, discard existing\n"
        "  modify — user wants to tweak dimensions of existing part\n"
        "  extend — user wants to add new features to existing part\n\n"
        "Reply with only the one word."
    )
    sys = "You classify CAD design prompts. Reply with exactly one word."
    r = (call_llm(prompt, sys, quality=quality, repo_root=repo_root) or "").strip().lower()
    if r in ("new", "modify", "extend"):
        return r
    # Fall back to "new" if LLM returned garbage
    return "new"


def classify_delta(goal: str, host_context: dict | None,
                    *, quality: str = "fast",
                    repo_root: Path | None = None) -> dict:
    """Return {kind: 'new'|'modify'|'extend', method: 'heuristic'|'llm',
    context_summary: str}. The context_summary is used by the planner to
    target the right parameters / features."""
    kind = _classify_heuristic(goal, host_context)
    method = "heuristic"
    if kind is None:
        kind = _classify_llm(goal, host_context,
                              quality=quality, repo_root=repo_root)
        method = "llm"
    params = (host_context or {}).get("user_parameters") or []
    return {
        "kind": kind,
        "method": method,
        "has_aria_params": _has_aria_params(params),
        "param_count": len(params),
    }


# --- Modify-plan emitter -------------------------------------------------

def build_modify_plan(goal: str, host_context: dict) -> list[dict]:
    """Given a modify-class prompt + current user parameters, emit a
    minimal plan that just updates the relevant params. Fusion's
    parametric tree rebuilds automatically when params change, so we
    don't need to touch sketches/extrudes."""
    params = (host_context or {}).get("user_parameters") or []
    # Extract numeric targets from the goal.  Very cheap: find
    # `<name> <value>mm` or `set <name> to <value>` patterns.
    g = goal.lower()
    updates: list[tuple[str, float]] = []

    # Parameter name hints — map colloquial words to ARIA param names.
    name_hints = {
        "od":        ["flange_od", "gear_od", "impeller_od"],
        "outer":     ["flange_od", "gear_od", "impeller_od"],
        "diameter":  ["flange_od", "gear_od", "impeller_od"],
        "bore":      ["flange_bore", "impeller_bore"],
        "id":        ["flange_bore", "impeller_bore"],
        "inner":     ["flange_bore", "impeller_bore"],
        "thickness": ["flange_thickness", "sm_thickness"],
        "thick":     ["flange_thickness", "sm_thickness"],
        "height":    ["flange_thickness", "sm_leg_h"],
        "width":     ["sm_width"],
        "depth":     ["sm_depth"],
        "bolt":      ["flange_bolt_dia"],
        "pcd":       ["flange_bolt_circle_r"],
    }
    # Scan for "<hint word> <number>mm". Case-insensitive match against
    # existing param names (planner uses CamelCase / mixed case).
    existing_lc = {p["name"].lower(): p["name"] for p in params}
    for hint, candidates in name_hints.items():
        m = re.search(rf"\b{hint}\w*\s+(?:to|=|is)?\s*(\d+(?:\.\d+)?)\s*mm\b", g)
        if m:
            val = float(m.group(1))
            for cand in candidates:
                actual = existing_lc.get(cand.lower())
                if actual:
                    # PCD is a diameter; stored param is radius
                    if cand.lower().endswith("_bolt_circle_r"):
                        val = val / 2
                    updates.append((actual, val))
                    break

    # Relative changes: "thicker", "bigger", "thinner", "smaller"
    rel_map = {
        "thicker":  ("flange_thickness", 1.5),
        "thinner":  ("flange_thickness", 0.7),
        "taller":   ("sm_leg_h", 1.5),
        "shorter":  ("sm_leg_h", 0.7),
        "bigger":   ("flange_od", 1.2),
        "smaller":  ("flange_od", 0.8),
        "wider":    ("sm_width", 1.2),
        "narrower": ("sm_width", 0.8),
    }
    existing = {p["name"].lower(): p for p in params}
    for word, (param_name, factor) in rel_map.items():
        if word in g and param_name.lower() in existing:
            p = existing[param_name.lower()]
            expr = p.get("expression", "")
            m = re.search(r"(\d+(?:\.\d+)?)", expr)
            if m:
                current = float(m.group(1))
                # Use the actual case-correct name from the design
                updates.append((p["name"], round(current * factor, 2)))

    if not updates:
        raise ValueError(
            "Modify prompt detected but no parameter target could be "
            "extracted. Consider being explicit: 'set OD to 120mm'.")

    plan: list[dict] = []
    for name, val in updates:
        plan.append({
            "kind": "addParameter",
            "params": {"name": name, "value_mm": val},
            "label": f"Update {name} → {val:g}mm",
        })
    return plan
