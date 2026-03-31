"""
aria_os/cem/cem_advisor.py

CEM Advisor — proactively identifies what to pre-compute CEM for so the
pipeline learns faster and iteration cycles are shorter.

What it does:
  1. Surveys the generation log to find all parts that have been built
  2. Checks cem_design_history.json to see what already has CEM coverage
  3. Identifies gaps: parts with no physics baseline
  4. Runs available CEM modules for uncovered parts
  5. Uses LLM to reason about which NEW CEM domains would unlock faster
     iteration (e.g. "cooling channels need a thermal CEM module")
  6. Runs parametric sweeps on parameters that users frequently change,
     pre-populating the design history so future generations skip cold-start

Run:
  python run_aria_os.py --cem-advise
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


# ── CEM modules available in the codebase ────────────────────────────────────
_KNOWN_CEM_MODULES = {
    "aria":   ("aria", "ratchet", "spool", "housing", "brake", "rope guide",
               "cam collar", "catch pawl", "clutch", "auto belay"),
    "lre":    ("nozzle", "rocket", "lre", "turbopump", "injector",
               "combustion", "thrust chamber", "bell nozzle"),
}


def _load_json(path: Path) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _resolve_cem_module(goal_or_part: str) -> str | None:
    """Return CEM module name for a goal/part string, or None."""
    g = goal_or_part.lower()
    for module_name, keywords in _KNOWN_CEM_MODULES.items():
        if any(kw in g for kw in keywords):
            return module_name
    return None


def _run_cem_for_part(goal: str, part_id: str, params: dict,
                       repo_root: Path) -> dict | None:
    """Run CEM compute for a single part. Returns summary dict or None."""
    module_name = _resolve_cem_module(goal) or _resolve_cem_module(part_id)
    if not module_name:
        return None
    try:
        from .cem_generator import resolve_and_compute
        result = resolve_and_compute(goal, part_id, params, repo_root)
        return result
    except Exception as exc:
        print(f"  [CEM] {part_id}: failed — {exc}")
        return None


def _get_generated_parts(repo_root: Path) -> list[dict]:
    """Read all generated parts from the generation log."""
    log_path  = repo_root / "outputs" / "aria_generation_log.json"
    meta_dir  = repo_root / "outputs" / "cad" / "meta"
    parts: list[dict] = []

    log = _load_json(log_path)
    if isinstance(log, list):
        for entry in log:
            if isinstance(entry, dict):
                parts.append(entry)

    # Also scan meta/ JSON files for parts not in the log
    known_ids = {p.get("part_id", "") for p in parts}
    if meta_dir.exists():
        for f in meta_dir.glob("*.json"):
            meta = _load_json(f)
            if isinstance(meta, dict):
                pid = meta.get("part_id", f.stem)
                if pid not in known_ids:
                    parts.append({
                        "part_id": pid,
                        "goal":    meta.get("goal", pid),
                        "params":  meta.get("params", {}),
                    })
                    known_ids.add(pid)
    return parts


def _get_cem_history(repo_root: Path) -> dict[str, Any]:
    """Return the CEM design history keyed by part_id."""
    hist = _load_json(repo_root / "cem_design_history.json")
    if isinstance(hist, dict):
        return hist
    if isinstance(hist, list):
        return {e.get("part_id", str(i)): e for i, e in enumerate(hist)}
    return {}


def _llm_suggest_new_domains(uncovered_goals: list[str],
                               repo_root: Path) -> str | None:
    """Ask LLM what new CEM domains would accelerate design iteration."""
    if not uncovered_goals:
        return None
    from aria_os.llm_client import call_llm
    goal_list = "\n".join(f"  - {g}" for g in uncovered_goals[:15])
    prompt = f"""You are a mechanical/aerospace engineering advisor for ARIA,
a wall-mounted lead climbing auto-belay device.

The following part types have been generated but have NO physics/CEM model:
{goal_list}

Existing CEM domains: aria (ARIA structural parts), lre (rocket nozzles).

For each uncovered part type, suggest:
1. Whether it needs a dedicated CEM domain or can reuse an existing one
2. What physics to model (e.g., heat flux, pressure drop, fatigue cycles)
3. What input parameters the CEM should accept
4. What output geometry scalars it should produce

Be concise — one paragraph per part type. Focus on what would most accelerate
design iteration for ARIA (safety-critical climbing device).
"""
    return call_llm(prompt, repo_root=repo_root)


def _parametric_sweep(part_id: str, base_params: dict,
                       repo_root: Path) -> list[dict]:
    """
    Run a small parametric sweep around a part's nominal dimensions.
    Generates N=5 variants (±10%, ±20%) and computes CEM for each.
    Returns list of {params, cem_result} dicts.
    """
    # Pick the most important sweep parameter for this part type
    sweep_key = None
    for key in ("od_mm", "bore_mm", "thickness_mm", "height_mm", "length_mm"):
        if key in base_params and base_params[key]:
            sweep_key = key
            break
    if not sweep_key:
        return []

    base_val = float(base_params[sweep_key])
    variants = [-0.2, -0.1, 0.0, 0.1, 0.2]
    results  = []

    for delta in variants:
        params = dict(base_params)
        params[sweep_key] = round(base_val * (1.0 + delta), 2)
        goal   = f"{part_id} {sweep_key}={params[sweep_key]}"
        r      = _run_cem_for_part(goal, part_id, params, repo_root)
        if r:
            results.append({"params": params, "cem": r, "delta_pct": delta * 100})

    return results


def run_cem_advisor(repo_root: Path) -> None:
    """
    Main entry point. Surveys the codebase, fills CEM gaps, suggests new domains.
    Prints a structured report and updates cem_design_history.json.
    """
    print("\n" + "=" * 60)
    print("ARIA CEM Advisor")
    print("=" * 60)

    # 1. Load state
    parts       = _get_generated_parts(repo_root)
    cem_history = _get_cem_history(repo_root)
    history_path = repo_root / "cem_design_history.json"
    new_entries: dict = {}

    print(f"\nGenerated parts found   : {len(parts)}")
    print(f"CEM history entries     : {len(cem_history)}")

    # 2. Categorise
    covered:   list[dict] = []
    uncovered: list[dict] = []
    no_module: list[dict] = []

    for part in parts:
        pid  = part.get("part_id", "")
        goal = part.get("goal",    pid)
        if pid in cem_history:
            covered.append(part)
            continue
        module = _resolve_cem_module(goal) or _resolve_cem_module(pid)
        if module:
            uncovered.append(part)
        else:
            no_module.append(part)

    print(f"\nCEM coverage:")
    print(f"  Already covered : {len(covered)}")
    print(f"  Missing (can run): {len(uncovered)}")
    print(f"  No CEM module   : {len(no_module)}")

    # 3. Run CEM for uncovered parts
    if uncovered:
        print(f"\n[CEM] Running CEM for {len(uncovered)} uncovered parts...")
        for part in uncovered:
            pid    = part.get("part_id", "")
            goal   = part.get("goal",    pid)
            params = part.get("params",  {})
            print(f"  {pid} ...", end=" ", flush=True)
            result = _run_cem_for_part(goal, pid, params, repo_root)
            if result:
                entry = {
                    "part_id":     pid,
                    "goal":        goal,
                    "params":      params,
                    "cem_result":  result,
                    "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source":      "cem_advisor",
                }
                new_entries[pid] = entry
                print("OK")
            else:
                print("skipped")

    # 4. Parametric sweeps for key ARIA parts
    aria_core = [p for p in parts
                 if p.get("part_id", "").startswith("aria_")
                 and p.get("params")]
    if aria_core:
        print(f"\n[SWEEP] Running parametric sweeps for {min(3, len(aria_core))} parts...")
        for part in aria_core[:3]:
            pid    = part.get("part_id", "")
            params = part.get("params",  {})
            print(f"  Sweeping {pid}...")
            sweep_results = _parametric_sweep(pid, params, repo_root)
            if sweep_results:
                sweep_key = f"{pid}_sweep"
                new_entries[sweep_key] = {
                    "part_id":   pid,
                    "type":      "parametric_sweep",
                    "variants":  sweep_results,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source":    "cem_advisor",
                }
                best = max(sweep_results,
                           key=lambda x: x.get("cem", {}).get("safety_factor", 0),
                           default=None)
                if best:
                    print(f"    Best SF={best['cem'].get('safety_factor','?'):.2f} "
                          f"at {best['params']}")

    # 5. Persist new CEM history entries
    if new_entries:
        merged = dict(cem_history)
        merged.update(new_entries)
        _save_json(history_path, merged)
        print(f"\n[OK] Wrote {len(new_entries)} new entries to cem_design_history.json")

    # 6. LLM suggestions for new CEM domains
    if no_module:
        print(f"\n[LLM] Suggesting new CEM domains for {len(no_module)} uncovered part types...")
        uncov_goals = [p.get("goal", p.get("part_id", "")) for p in no_module]
        suggestion  = _llm_suggest_new_domains(uncov_goals, repo_root)
        if suggestion:
            print("\n" + "-" * 60)
            print("New CEM domain suggestions:")
            print("-" * 60)
            print(suggestion)
            # Save suggestions as a note
            notes_path = repo_root / "outputs" / "cem_advisor_suggestions.md"
            notes_path.write_text(
                f"# CEM Advisor Suggestions\n\n"
                f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"## Uncovered parts\n" +
                "\n".join(f"- {g}" for g in uncov_goals) +
                f"\n\n## LLM Recommendations\n\n{suggestion}\n",
                encoding="utf-8",
            )
            print(f"\n  Saved to: {notes_path}")
        else:
            print("  [LLM unavailable — add GOOGLE_API_KEY or ANTHROPIC_API_KEY]")

    # 7. Summary
    print("\n" + "=" * 60)
    print("CEM Advisor complete")
    print(f"  New CEM entries : {len(new_entries)}")
    print(f"  Parts still uncovered (no module): {len(no_module)}")
    if no_module:
        print("  Uncovered parts (add CEM module in cem_registry.py):")
        for p in no_module[:10]:
            print(f"    - {p.get('part_id', '?')}")
    print("=" * 60 + "\n")
