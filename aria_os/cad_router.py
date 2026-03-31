from __future__ import annotations

import asyncio
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .cem_checks import CEMCheckResult
from .context_loader import load_materials


class CADTool(str, Enum):
    FUSION_SCRIPT = "fusion_script"
    TEMPLATE = "template"
    MANUAL = "manual"


def _yield_for_part(part_id: str, context: dict) -> tuple[str, float]:
    pid = (part_id or "").lower()
    if any(k in pid for k in ("pawl", "lever", "trip", "blocker")):
        return "A2 tool steel", 1800.0
    if any(k in pid for k in ("ratchet", "ring", "gear", "tooth")):
        return "4140 QT", 1300.0
    if any(k in pid for k in ("housing", "shell", "enclosure")):
        return "6061-T6", 276.0
    if any(k in pid for k in ("shaft", "spool", "collar")):
        return "4140 HT", 1000.0
    mats = load_materials(context)
    if mats:
        m = mats[0]
        return m.name, float(m.yield_mpa)
    return "Unknown", 0.0


def build_part_description(part_id: str, meta: dict, cem_result: CEMCheckResult, context: dict) -> str:
    dims = (meta or {}).get("dims_mm", {}) if isinstance(meta, dict) else {}
    material_name, yield_mpa = _yield_for_part(part_id, context)
    dynamic_info = "Dynamic result: system-level in run_full_system_cem report."
    static_sf = cem_result.static_min_sf if cem_result.static_min_sf is not None else -1.0
    summary = cem_result.summary or "No summary"
    lines = [
        f"Part: {part_id}",
        "Role: catch mechanism / structural safety component in ARIA auto-belay.",
        "Key dimensions (mm):",
    ]
    for k, v in sorted(dims.items()):
        lines.append(f"  - {k}: {v} mm")
    lines.extend(
        [
            f"Material: {material_name}",
            f"Yield strength: {yield_mpa:.1f} MPa",
            f"CEM summary: {summary}",
            f"Static SF: {static_sf:.3f}",
            f"Failure mode: {cem_result.static_failure_mode or 'n/a'}",
            dynamic_info,
            "Design constraint: ANSI Z359.14 with minimum 2x SF at 16,000 N proof load.",
        ]
    )
    if cem_result.static_min_sf is not None and cem_result.static_min_sf < 2.0 and dims:
        # simple proportional estimate for thickness increase
        cur_sf = max(cem_result.static_min_sf, 1e-6)
        factor = 2.0 / cur_sf
        thickness_keys = [k for k in dims.keys() if "THICK" in k.upper() or "WIDTH" in k.upper()]
        if thickness_keys:
            k0 = thickness_keys[0]
            try:
                t = float(dims[k0])
                target = t * factor
                delta = max(target - t, 0.0)
                lines.append(f"Suggested change: increase {k0} by ~{delta:.2f} mm to target SF≈2.0.")
            except Exception:
                lines.append("Suggested change: increase section thickness and fillet stress risers.")
        else:
            lines.append("Suggested change: increase section thickness and fillet stress risers.")
    return "\n".join(lines)


class CADIterationStore:
    def __init__(self, repo_root: Optional[Path] = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.path = repo_root / "outputs" / "cad" / "iteration_history.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.write_text(json.dumps(items[-1000:], indent=2), encoding="utf-8")

    def log_iteration(self, part_id, tool, cem_result: CEMCheckResult, parameter_overrides):
        items = self._load()
        items.append(
            {
                "timestamp": datetime.now().isoformat(),
                "part_id": part_id,
                "tool": tool,
                "sf": cem_result.static_min_sf,
                "overall_passed": cem_result.overall_passed,
                "failure_mode": cem_result.static_failure_mode,
                "parameter_overrides": parameter_overrides or {},
            }
        )
        self._save(items)

    def get_history(self, part_id, n=5):
        items = [x for x in self._load() if x.get("part_id") == part_id]
        return items[-n:]

    def get_best_result(self, part_id):
        items = self.get_history(part_id, n=1000)
        if not items:
            return None
        return max(items, key=lambda x: float(x.get("sf") or -1.0))

    def summarize_trends(self, part_id):
        h = self.get_history(part_id, n=1000)
        if not h:
            return {"avg_sf_progression": [], "most_common_failure_mode": None, "changed_parameters": [], "sf_trending_up": False}
        sfs = [float(x.get("sf") or 0.0) for x in h]
        modes = [x.get("failure_mode") for x in h if x.get("failure_mode")]
        mc = max(set(modes), key=modes.count) if modes else None
        changed = set()
        for it in h:
            for k in (it.get("parameter_overrides") or {}).keys():
                changed.add(k)
        trending = len(sfs) >= 2 and sfs[-1] > sfs[0]
        return {
            "avg_sf_progression": sfs,
            "most_common_failure_mode": mc,
            "changed_parameters": sorted(changed),
            "sf_trending_up": trending,
        }


async def route_cad_request(
    part_id: str,
    meta: dict,
    cem_result: CEMCheckResult,
    context: dict,
    iteration_history: list[dict],
) -> dict:
    description = build_part_description(part_id, meta, cem_result, context)
    store = CADIterationStore()
    recent = store.get_history(part_id, n=5)

    # Escalate if 3+ consecutive iterations show no SF improvement
    if len(recent) >= 3:
        sfs = [float(x.get("sf") or 0.0) for x in recent[-3:]]
        if not (sfs[2] > sfs[1] or sfs[1] > sfs[0]):
            return {
                "cad_tool": CADTool.MANUAL.value,
                "rationale": "Escalated to manual: 3+ recent iterations show no safety-factor improvement.",
                "description": description,
                "parameter_overrides": {},
                "confidence": 0.99,
            }

    system_prompt = (
        "You are an aerospace-grade mechanical engineering assistant for ARIA, an auto-belay climbing safety device. "
        "Your role is to route CAD generation requests to the correct tool and provide precise engineering descriptions.\n\n"
        "ARIA must meet ANSI Z359.14: minimum 2× safety factor at 16,000 N proof load. "
        "Key parts: pawl (A2 tool steel, yield 1800 MPa), ratchet ring (4140 QT, yield 1300 MPa), "
        "housing (6061-T6, yield 276 MPa), main shaft (4140 HT, yield 1000 MPa).\n\n"
        "When routing:\n"
        "Use fusion_script for parametric parts where dimension overrides can fix failures\n"
        "Use template for standard geometry parts (shafts, collars, simple housings)\n"
        "Use manual when failure mode is complex (buckling, fatigue, assembly interference) or 3+ iterations failed\n\n"
        "When writing CAD descriptions, be specific: include exact dimensions, tolerances, material spec, surface finish, "
        "critical features (bore size, thread spec, fillet radii at stress concentrations)."
    )
    user_prompt = (
        description
        + "\n\nIteration history:\n"
        + json.dumps(iteration_history or [], indent=2)
        + "\n\nReturn strict JSON with keys: cad_tool, rationale, description, parameter_overrides, confidence."
    )

    try:
        import anthropic
        from .llm_generator import _get_api_key

        client = anthropic.Anthropic(api_key=_get_api_key())
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = ""
        for b in msg.content:
            if hasattr(b, "text"):
                text += b.text
        m = text[text.find("{"): text.rfind("}") + 1] if "{" in text and "}" in text else text
        out = json.loads(m)
        return {
            "cad_tool": out.get("cad_tool", CADTool.TEMPLATE.value),
            "rationale": out.get("rationale", ""),
            "description": out.get("description", description),
            "parameter_overrides": out.get("parameter_overrides", {}),
            "confidence": float(out.get("confidence", 0.5)),
        }
    except Exception:
        # deterministic local fallback
        sf = cem_result.static_min_sf if cem_result.static_min_sf is not None else 999.0
        if sf < 2.0:
            tool = CADTool.FUSION_SCRIPT.value
            rationale = "Low SF: use fusion_script for tight parametric reinforcement."
        elif sf < 3.0:
            tool = CADTool.TEMPLATE.value
            rationale = "Near-threshold SF: use template and tighten dimensions."
        else:
            tool = CADTool.TEMPLATE.value
            rationale = "SF healthy; template route sufficient."
        return {
            "cad_tool": tool,
            "rationale": rationale,
            "description": description,
            "parameter_overrides": {},
            "confidence": 0.6,
        }


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def record_cad_outcome(
    part_id: str,
    parameter_overrides_used: dict,
    new_cem_result: CEMCheckResult,
    tool: str = CADTool.TEMPLATE.value,
    repo_root: Optional[Path] = None,
) -> dict:
    store = CADIterationStore(repo_root=repo_root)
    prev_best = store.get_best_result(part_id)
    old_sf = float(prev_best.get("sf") or -1.0) if prev_best else -1.0
    new_sf = float(new_cem_result.static_min_sf or -1.0)
    store.log_iteration(part_id, tool, new_cem_result, parameter_overrides_used)
    trend = store.summarize_trends(part_id)
    if new_sf > old_sf:
        status = "improved"
    elif new_sf < old_sf:
        status = "regressed"
    else:
        status = "stalled"
    return {
        "status": status,
        "old_sf": old_sf,
        "new_sf": new_sf,
        "trend": trend,
    }
