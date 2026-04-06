"""Assembly Agent — decomposes multi-part assembly descriptions into individual parts.

Takes a high-level assembly description, uses the LLM to decompose it into
individual part specs, generates each part via the existing coordinator pipeline,
and creates an assembly config JSON compatible with ``assemble.py``.

Usage (programmatic):
    from aria_os.agents.assembly_agent import AssemblyAgent
    agent = AssemblyAgent(repo_root)
    result = await agent.run("motor mount assembly: baseplate with M4 holes and vertical bracket")

The resulting assembly config is written to ``assembly_configs/<slug>.json`` and can
be fed directly into ``python assemble.py``.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Assembly detection
# ---------------------------------------------------------------------------

_ASSEMBLY_KEYWORDS = [
    "assembly",
    "consisting of",
    "composed of",
    "made up of",
    "with a",
    "mounted on",
    "connected to",
    "bolted to",
    "fastened to",
    "attached to",
    "welded to",
    "screwed into",
    "press-fit into",
    "mated with",
    " and a ",
    " plus a ",
    "sub-assembly",
    "subassembly",
    "multi-part",
    "multiple parts",
]


def is_assembly_goal(goal: str) -> bool:
    """Return True if the goal describes a multi-part assembly rather than a single part.

    Uses keyword matching — intentionally conservative to avoid false positives
    on single-part descriptions that happen to mention "with a bore" etc.
    Requires at least one strong assembly keyword OR two weak ones.
    """
    goal_lower = goal.lower()

    # Strong keywords — one is enough
    strong = ["assembly", "consisting of", "composed of", "multi-part",
              "multiple parts", "subassembly", "sub-assembly"]
    for kw in strong:
        if kw in goal_lower:
            return True

    # Weak keywords — need two
    weak = ["mounted on", "connected to", "bolted to", "fastened to",
            "attached to", "welded to", "screwed into", "press-fit into",
            "mated with", " and a ", " plus a "]
    hits = sum(1 for kw in weak if kw in goal_lower)
    return hits >= 2


# ---------------------------------------------------------------------------
# LLM decomposition
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = """You are a mechanical assembly decomposition agent.
Given a high-level assembly description, break it into individual parts that
can each be modeled as a single CAD solid.

Rules:
- Each part must be a single solid body (no sub-assemblies).
- Include specific dimensions in mm for every part where possible.
- Infer reasonable dimensions when not explicitly stated.
- Use standard part naming: baseplate, bracket, shaft, spacer, flange, etc.
- Give each part a unique snake_case id (e.g. "motor_baseplate", "vertical_bracket").
- Position is [0,0,0] for all parts (user adjusts later).
- Rotation is [0,0,0] for all parts.
- If one part mounts on another, note it in depends_on.

Respond with ONLY valid JSON (no markdown fences, no explanation). Format:
{
  "name": "assembly_name_snake_case",
  "parts": [
    {
      "id": "part_id",
      "goal": "full natural-language description with dimensions for the CAD generator",
      "position": [0, 0, 0],
      "rotation": [0, 0, 0],
      "depends_on": null
    }
  ]
}"""


def _decompose_with_llm(goal: str, repo_root: Path) -> dict | None:
    """Use the LLM priority chain to decompose an assembly description into parts."""
    from ..llm_client import call_llm

    prompt = f"""Decompose this assembly into individual parts:

{goal}

Return a JSON object with "name" and "parts" array. Each part needs an "id" and a
detailed "goal" string with dimensions that the CadQuery generator can use to create
a single solid body."""

    response = call_llm(prompt, system=_DECOMPOSE_SYSTEM, repo_root=repo_root)
    if not response:
        return None

    # Extract JSON from response (may be wrapped in markdown fences)
    text = response.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        start = 1
        end = len(lines)
        for i, line in enumerate(lines):
            if i > 0 and line.strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[start:end])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Assembly Agent
# ---------------------------------------------------------------------------

class AssemblyAgent:
    """Decomposes an assembly goal into parts, generates each, and writes assembly config."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent.parent

    async def run(self, goal: str) -> dict[str, Any]:
        """Execute the full assembly pipeline.

        Returns a dict with:
            - name: assembly name
            - config_path: path to written assembly JSON
            - parts: list of part results (id, goal, step_path, passed)
            - assembly_step: path to combined STEP (if assemble.py succeeded)
            - errors: list of error strings
        """
        from .. import event_bus

        result: dict[str, Any] = {
            "name": "",
            "config_path": "",
            "parts": [],
            "assembly_step": "",
            "errors": [],
        }

        t0 = time.time()
        print(f"\n{'=' * 64}")
        print(f"  ASSEMBLY AGENT")
        print(f"  Goal: {goal}")
        print(f"{'=' * 64}")

        # Step 1: Decompose into parts
        print(f"\n  [Assembly] Decomposing into individual parts...")
        decomp = _decompose_with_llm(goal, self.repo_root)

        if not decomp or not decomp.get("parts"):
            msg = "LLM decomposition failed — could not parse parts from response"
            print(f"  [Assembly] ERROR: {msg}")
            result["errors"].append(msg)
            return result

        assembly_name = decomp.get("name", "unnamed_assembly")
        parts_spec = decomp["parts"]
        result["name"] = assembly_name

        print(f"  [Assembly] Decomposed into {len(parts_spec)} parts:")
        for p in parts_spec:
            dep = f" (depends_on: {p.get('depends_on')})" if p.get("depends_on") else ""
            print(f"    - {p['id']}: {p['goal'][:70]}...{dep}" if len(p.get('goal', '')) > 70
                  else f"    - {p['id']}: {p.get('goal', '?')}{dep}")

        # Step 2: Generate each part via the coordinator pipeline
        print(f"\n  [Assembly] Generating {len(parts_spec)} parts...")

        assembly_parts_cfg = []  # for the assembly JSON
        loop = asyncio.get_event_loop()

        for i, part_spec in enumerate(parts_spec):
            part_id = part_spec.get("id", f"part_{i}")
            part_goal = part_spec.get("goal", "")
            position = part_spec.get("position", [0, 0, 0])
            rotation = part_spec.get("rotation", [0, 0, 0])
            depends_on = part_spec.get("depends_on")

            print(f"\n  [Assembly] [{i+1}/{len(parts_spec)}] Generating: {part_id}")
            print(f"    Goal: {part_goal}")

            part_result = {
                "id": part_id,
                "goal": part_goal,
                "step_path": "",
                "passed": False,
            }

            try:
                # Import and run the single-part coordinator
                from .coordinator import CoordinatorAgent
                coordinator = CoordinatorAgent(self.repo_root)
                ctx = await coordinator.run(part_goal)

                part_result["passed"] = ctx.validation_passed
                if ctx.geometry_path and Path(ctx.geometry_path).exists():
                    part_result["step_path"] = ctx.geometry_path
                    tag = "PASS" if ctx.validation_passed else "WARN"
                    print(f"  [Assembly] [{i+1}/{len(parts_spec)}] {part_id}: {tag} -> {ctx.geometry_path}")
                else:
                    print(f"  [Assembly] [{i+1}/{len(parts_spec)}] {part_id}: FAIL — no geometry produced")
                    result["errors"].append(f"Part '{part_id}' failed to generate geometry")

            except Exception as exc:
                print(f"  [Assembly] [{i+1}/{len(parts_spec)}] {part_id}: ERROR — {exc}")
                result["errors"].append(f"Part '{part_id}' error: {exc}")

            result["parts"].append(part_result)

            # Add to assembly config (even if generation failed — user can fix later)
            entry: dict[str, Any] = {
                "id": part_id,
                "step": part_result["step_path"] or f"outputs/cad/step/{part_id}.step",
                "pos": position,
                "rot": rotation,
                "notes": part_goal[:100],
            }
            if depends_on:
                entry["depends_on"] = depends_on
            assembly_parts_cfg.append(entry)

        # Step 3: Write assembly config JSON
        config_dir = self.repo_root / "assembly_configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize assembly name for filename
        safe_name = re.sub(r"[^\w\-]+", "_", assembly_name).strip("_").lower()
        if not safe_name:
            safe_name = "unnamed_assembly"
        config_path = config_dir / f"{safe_name}.json"

        assembly_config = {
            "name": assembly_name,
            "parts": assembly_parts_cfg,
        }

        config_path.write_text(json.dumps(assembly_config, indent=2), encoding="utf-8")
        result["config_path"] = str(config_path)
        print(f"\n  [Assembly] Config written: {config_path}")

        # Step 4: Run assemble.py if at least one part has geometry
        parts_with_geometry = [p for p in result["parts"] if p["step_path"]]
        if parts_with_geometry:
            print(f"\n  [Assembly] Running assembly builder ({len(parts_with_geometry)}/{len(parts_spec)} parts)...")
            try:
                from assemble import build_assembly
                assembly_step = await loop.run_in_executor(
                    None,
                    build_assembly,
                    config_path,
                    None,   # output_path — auto
                    False,  # open_preview
                    True,   # run_clearance
                    0.5,    # min_clearance_mm
                )
                result["assembly_step"] = str(assembly_step)
                print(f"  [Assembly] Combined STEP: {assembly_step}")
            except Exception as exc:
                msg = f"Assembly builder failed: {exc}"
                print(f"  [Assembly] {msg}")
                result["errors"].append(msg)
        else:
            print(f"\n  [Assembly] No parts with geometry — skipping assembly builder")

        # Summary
        elapsed = time.time() - t0
        n_pass = sum(1 for p in result["parts"] if p["passed"])
        n_total = len(result["parts"])
        print(f"\n{'=' * 64}")
        print(f"  ASSEMBLY SUMMARY")
        print(f"{'=' * 64}")
        print(f"  Name:     {assembly_name}")
        print(f"  Parts:    {n_pass}/{n_total} generated successfully")
        print(f"  Config:   {result['config_path']}")
        if result["assembly_step"]:
            print(f"  Assembly: {result['assembly_step']}")
        if result["errors"]:
            print(f"  Errors:")
            for e in result["errors"]:
                print(f"    - {e}")
        print(f"  Time:     {elapsed:.1f}s")
        print(f"{'=' * 64}")

        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_assembly_agent(goal: str, repo_root: Path | None = None) -> dict[str, Any]:
    """Run the assembly agent. Async entry point."""
    agent = AssemblyAgent(repo_root)
    return await agent.run(goal)


def run_assembly_agent_sync(goal: str, repo_root: Path | None = None) -> dict[str, Any]:
    """Synchronous wrapper for the assembly agent."""
    return asyncio.run(run_assembly_agent(goal, repo_root))
