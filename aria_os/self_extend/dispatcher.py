"""
Dispatcher — classify an incoming ExtensionRequest, then route it to an
existing template if one covers the prompt.

Classification is keyword-based first (deterministic, no LLM), with an
optional LLM fallback when the keywords are ambiguous.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .orchestrator import ExtensionRequest, Domain


_MCAD_KEYWORDS = {
    "bracket", "flange", "gear", "impeller", "fan", "nozzle", "housing",
    "mount", "shaft", "coupling", "plate", "enclosure", "heat sink",
    "heatsink", "spool", "spoked", "wheel", "pulley", "ratchet",
    "snap hook", "standoff", "clamp", "hinge", "gusset",
}
_ECAD_KEYWORDS = {
    "pcb", "board", "schematic", "kicad", "netlist", "gerber", "footprint",
    "esc", "flight controller", "fc", "power board", "breakout",
    "microcontroller board",
}
_LATTICE_KEYWORDS = {
    "lattice", "gyroid", "schwarz", "tpms", "octet", "bcc", "fcc",
    "kagome", "honeycomb", "iwp", "neovius", "diamond lattice",
    "stochastic beams", "stress-driven", "functionally graded", "fgm",
    "topology optim",
}


def classify_request(req: "ExtensionRequest", *, dry_run: bool = False) -> "Domain":
    from .orchestrator import Domain
    g = req.goal.lower()
    if any(kw in g for kw in _LATTICE_KEYWORDS):
        return Domain.LATTICE
    if any(kw in g for kw in _ECAD_KEYWORDS):
        return Domain.ECAD
    if any(kw in g for kw in _MCAD_KEYWORDS):
        return Domain.MCAD
    # In dry-run or missing-LLM mode, default to MCAD.
    return Domain.MCAD


def try_existing_template(req: "ExtensionRequest",
                          *, dry_run: bool = False) -> dict | None:
    """Check the existing CadQuery / SDF / ECAD template registries for
    a match. If found, return a handle (template fn name) so the
    orchestrator can short-circuit the discovery loop. Otherwise None.
    """
    from .orchestrator import Domain
    goal = req.goal
    try:
        if req.domain in (Domain.MCAD, Domain.UNKNOWN):
            from aria_os.generators.cadquery_generator import (
                _find_template_fuzzy, _CQ_TEMPLATE_MAP)  # type: ignore
            tmpl_fn, match_type = _find_template_fuzzy(
                req.spec.get("part_id", ""), goal, req.spec)
            if tmpl_fn is not None:
                name = next((k for k, v in _CQ_TEMPLATE_MAP.items()
                             if v is tmpl_fn), None)
                return {"template": name or tmpl_fn.__name__,
                        "match_type": match_type, "artifacts": {}}
    except Exception:
        pass
    try:
        if req.domain in (Domain.LATTICE, Domain.MCAD, Domain.UNKNOWN):
            from aria_os.sdf.templates import find_template  # type: ignore
            fn = find_template(goal)
            if fn is not None:
                return {"template": fn.__name__, "match_type": "sdf",
                        "artifacts": {}}
    except Exception:
        pass
    return None
