"""Coordinator Agent — decomposes high-level requests into parallel worker tasks.

Never generates geometry directly. Delegates to specialized workers,
synthesizes results, and manages the phase pipeline:

  Phase 1 (parallel): Research (materials + standards + similar parts)
  Phase 2 (serial):   Coordinator synthesizes spec from research
  Phase 3 (serial):   GeometryAgent → ValidationAgent (with refinement)
  Phase 4 (parallel):  CAMAgent + SimulationAgent (if valid)
  Phase 5 (serial):   Final assembly, MillForge bridge
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import event_bus


# ---------------------------------------------------------------------------
# Scratchpad — cross-agent data store per job
# ---------------------------------------------------------------------------

WORKSPACE = Path(__file__).resolve().parent.parent.parent / "workspace" / "scratchpad"


@dataclass
class JobContext:
    """Shared context for a single coordinator job."""
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    repo_root: Path = field(default_factory=lambda: Path("."))
    created_at: datetime = field(default_factory=datetime.now)

    # Phase 1 outputs (research)
    research_materials: dict[str, Any] = field(default_factory=dict)
    research_standards: dict[str, Any] = field(default_factory=dict)
    research_similar: dict[str, Any] = field(default_factory=dict)

    # Phase 2 output (coordinator synthesis)
    geometry_spec: dict[str, Any] = field(default_factory=dict)

    # Phase 3 outputs (geometry + validation)
    geometry_path: str = ""       # STEP/3dm path
    stl_path: str = ""
    validation_report: dict[str, Any] = field(default_factory=dict)
    validation_passed: bool = False

    # Phase 4 outputs (CAM + simulation)
    cam_result: dict[str, Any] = field(default_factory=dict)
    simulation_result: dict[str, Any] = field(default_factory=dict)

    # Phase 5 output (final)
    millforge_job: dict[str, Any] = field(default_factory=dict)

    # Tracking
    phases_completed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_time_s: float = 0.0

    @property
    def scratchpad_dir(self) -> Path:
        d = WORKSPACE / self.job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_artifact(self, name: str, data: dict | str) -> Path:
        """Save an artifact to the scratchpad."""
        path = self.scratchpad_dir / name
        if isinstance(data, dict):
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        else:
            path.write_text(str(data), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class CoordinatorAgent:
    """
    Receives high-level geometry requests and decomposes into parallel tasks.
    Never generates geometry directly — only delegates to worker agents.
    """

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent.parent

    async def run(self, goal: str) -> JobContext:
        """Execute the full 5-phase pipeline."""
        ctx = JobContext(goal=goal, repo_root=self.repo_root)
        t0 = time.time()

        _emit(ctx, "coordinator", f"Job {ctx.job_id} started", {"goal": goal})
        print(f"\n{'=' * 64}")
        print(f"  COORDINATOR — Job {ctx.job_id}")
        print(f"  Goal: {goal}")
        print(f"{'=' * 64}")

        try:
            # Phase 1: Parallel research
            await self._phase_1_research(ctx)

            # Phase 2: Synthesize geometry spec
            await self._phase_2_synthesize(ctx)

            # Phase 3: Generate + validate geometry (with refinement loop)
            await self._phase_3_geometry(ctx)

            # Phase 4: Parallel CAM + simulation (only if geometry valid)
            if ctx.validation_passed:
                await self._phase_4_manufacturing(ctx)

            # Phase 5: Final assembly + MillForge bridge
            await self._phase_5_finalize(ctx)

        except Exception as exc:
            ctx.errors.append(f"Coordinator error: {exc}")
            _emit(ctx, "error", f"Job {ctx.job_id} failed: {exc}")
            print(f"  [COORDINATOR] ERROR: {exc}")

        ctx.total_time_s = time.time() - t0
        self._print_summary(ctx)
        return ctx

    # -- Phase 1: Parallel Research ------------------------------------------

    async def _phase_1_research(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 1: Research (parallel)", {"phase": 1})
        print(f"\n  [Phase 1] Research (parallel)...")

        from .search_chain import get_search_chain
        chain = get_search_chain()

        # Run 4 research queries in parallel — targeted for CAD generation
        from .features import get_features
        _web_ok = get_features().WEB_SEARCH

        async def _research_materials():
            if not _web_ok:
                return {"status": "skipped"}
            results = await chain.search(f"{ctx.goal} material properties yield strength density")
            data = {"results": [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in results]}
            ctx.save_artifact("research_materials.json", data)
            return data

        async def _research_shape():
            """Find what this part actually looks like — geometry description."""
            if not _web_ok:
                return {"status": "skipped"}
            results = await chain.search(f"{ctx.goal} shape geometry cross section features components")
            data = {"results": [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in results]}
            ctx.save_artifact("research_shape.json", data)
            return data

        async def _research_dimensions():
            """Find real-world dimensions and measurements."""
            if not _web_ok:
                return {"status": "skipped"}
            results = await chain.search(f"{ctx.goal} exact dimensions mm measurements size chart")
            data = {"results": [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in results]}
            ctx.save_artifact("research_dimensions.json", data)
            return data

        async def _research_cad():
            """Find CAD references, 3D models, engineering drawings."""
            if not _web_ok:
                return {"status": "skipped"}
            results = await chain.search(f"{ctx.goal} 3D model CAD STEP engineering drawing")
            data = {"results": [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in results]}
            ctx.save_artifact("research_cad.json", data)
            return data

        # Execute all 4 in parallel
        mat, shape, dims, cad = await asyncio.gather(
            _research_materials(),
            _research_shape(),
            _research_dimensions(),
            _research_cad(),
            return_exceptions=True,
        )

        ctx.research_materials = mat if isinstance(mat, dict) else {"error": str(mat)}
        ctx.research_standards = shape if isinstance(shape, dict) else {"error": str(shape)}
        ctx.research_similar = dims if isinstance(dims, dict) else {"error": str(dims)}

        # Store all research for Phase 2
        ctx._research_shape = shape if isinstance(shape, dict) else {}
        ctx._research_dims = dims if isinstance(dims, dict) else {}
        ctx._research_cad = cad if isinstance(cad, dict) else {}

        n_results = sum(
            len(d.get("results", [])) for d in [
                ctx.research_materials, ctx.research_standards,
                ctx.research_similar, ctx._research_cad]
            if isinstance(d, dict)
        )
        print(f"  [Phase 1] Complete: {n_results} total research results")
        ctx.phases_completed.append("research")
        _emit(ctx, "phase_complete", f"Phase 1 done: {n_results} results", {"phase": 1})

    # -- Phase 2: Coordinator Synthesis --------------------------------------

    async def _phase_2_synthesize(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 2: Synthesis", {"phase": 2})
        print(f"\n  [Phase 2] Synthesizing geometry spec from research...")

        # Step 1: Extract structured spec from goal
        from .spec_agent import SpecAgent
        from .design_state import DesignState

        state = DesignState(goal=ctx.goal, repo_root=ctx.repo_root)

        # Compile all research into a single context
        research_text = ""
        for label, data in [
            ("Shape & Geometry", getattr(ctx, "_research_shape", {})),
            ("Dimensions", getattr(ctx, "_research_dims", {})),
            ("CAD References", getattr(ctx, "_research_cad", {})),
            ("Materials", ctx.research_materials),
        ]:
            if isinstance(data, dict) and data.get("results"):
                research_text += f"\n## {label}\n"
                for r in data["results"][:5]:
                    research_text += f"- {r.get('title', '')}: {r.get('snippet', '')}\n"

        state.plan["research_context"] = research_text

        spec_agent = SpecAgent(ctx.repo_root)
        spec_agent.extract(state)

        # Step 2: Use LLM to synthesize a BUILD RECIPE from research
        # This is the critical step — turn raw search results into a
        # step-by-step CadQuery geometry description that the 7b model can follow.
        build_recipe = await self._synthesize_build_recipe(ctx, state.spec, research_text)

        ctx.geometry_spec = {
            "spec": state.spec,
            "cem_params": state.cem_params,
            "material": state.material,
            "research_context": research_text[:2000],
            "build_recipe": build_recipe,
        }
        ctx.save_artifact("geometry_spec.json", ctx.geometry_spec)

        print(f"  [Phase 2] Spec: {len(state.spec)} params, material: {state.material or 'auto'}")
        if build_recipe:
            print(f"  [Phase 2] Build recipe: {len(build_recipe)} chars")
        ctx.phases_completed.append("synthesis")
        _emit(ctx, "phase_complete", "Phase 2 done", {"phase": 2, "spec": state.spec})

    async def _synthesize_build_recipe(
        self, ctx: JobContext, spec: dict, research: str
    ) -> str:
        """Use LLM to create a step-by-step CadQuery build recipe from research.

        The recipe tells the DesignerAgent EXACTLY what geometry operations to perform,
        in what order, with what dimensions. This compensates for the 7b model's
        inability to reason about complex 3D shapes from scratch.
        """
        from .base_agent import _call_ollama
        from .ollama_config import AGENT_MODELS

        system = """You are a CAD geometry planner. Given a part description and web research about its shape,
create a step-by-step CadQuery build recipe.

Rules:
- Describe ONLY CadQuery operations (box, circle, extrude, cut, union, polyline)
- NEVER use .cylinder() — use .circle(r).extrude(h)
- NEVER use .fillet() on first attempt
- Include exact dimensions in mm for every operation
- Each step should be one CadQuery operation

Output format:
STEP 1: Create base plate — cq.Workplane("XY").box(width, depth, thickness)
STEP 2: Cut center bore — .faces(">Z").workplane().circle(r).cutThruAll()
STEP 3: Add raised feature — .workplane(offset=thickness).rect(w, d).extrude(height)
...etc

Be SPECIFIC about dimensions. Use the research to determine realistic sizes."""

        prompt = f"""Part request: {ctx.goal}

Extracted spec: {json.dumps(spec, default=str)}

Research findings:
{research[:3000]}

Create a step-by-step CadQuery build recipe for this part.
Include exact dimensions from the research or spec.
Describe the 3D shape in terms of CadQuery operations."""

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, _call_ollama, prompt, system, AGENT_MODELS.get("spec", "qwen2.5-coder:7b")
        )
        return response or ""

    # -- Phase 3: Geometry Generation + Validation ---------------------------

    async def _phase_3_geometry(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 3: Geometry + Validation", {"phase": 3})
        print(f"\n  [Phase 3] Generating geometry...")

        from .refinement_loop import run_agent_loop
        from .design_state import DesignState
        from .domains import detect_domain

        domain = detect_domain(ctx.goal)

        state = DesignState(
            goal=ctx.goal,
            repo_root=ctx.repo_root,
            domain=domain,
            spec=ctx.geometry_spec.get("spec", {}),
            cem_params=ctx.geometry_spec.get("cem_params", {}),
            material=ctx.geometry_spec.get("material", ""),
            max_iterations=10,
        )
        state.plan["research_context"] = ctx.geometry_spec.get("research_context", "")
        state.plan["build_recipe"] = ctx.geometry_spec.get("build_recipe", "")

        # Run the refinement loop (sync — runs in thread pool)
        loop = asyncio.get_event_loop()
        state = await loop.run_in_executor(None, run_agent_loop, state)

        ctx.validation_passed = state.converged
        ctx.geometry_path = state.artifacts.get("step_path", "")
        ctx.stl_path = state.artifacts.get("stl_path", "")
        ctx.validation_report = {
            "converged": state.converged,
            "iterations": state.iteration,
            "failures": list(state.failures),
            "bbox": state.bbox,
        }
        ctx.save_artifact("validation_report.json", ctx.validation_report)

        tag = "PASS" if state.converged else "FAIL"
        print(f"  [Phase 3] {tag} — {state.iteration} iterations, {len(state.failures)} failures")
        ctx.phases_completed.append("geometry")
        _emit(ctx, "phase_complete", f"Phase 3: {tag}", {"phase": 3})

    # -- Phase 4: Parallel CAM + Simulation ----------------------------------

    async def _phase_4_manufacturing(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 4: CAM + Simulation (parallel)", {"phase": 4})
        print(f"\n  [Phase 4] CAM + Simulation (parallel)...")

        async def _run_cam():
            if not ctx.geometry_path or not Path(ctx.geometry_path).exists():
                return {"status": "skipped", "reason": "no geometry"}
            try:
                from .cam_agent import run_cam_agent
                loop = asyncio.get_event_loop()
                material = ctx.geometry_spec.get("material", "aluminium_6061")
                result = await loop.run_in_executor(
                    None, run_cam_agent, ctx.geometry_path, material)
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        async def _run_simulation():
            from .features import get_features
            if not get_features().ANSYS_SIMULATION:
                return {"status": "skipped", "reason": "ANSYS_SIMULATION disabled"}
            # Run FEA via physics_analyzer (sync)
            try:
                from ..physics_analyzer import analyze
                loop = asyncio.get_event_loop()
                spec = ctx.geometry_spec.get("spec", {})
                result = await loop.run_in_executor(
                    None, analyze,
                    spec.get("part_type", ""), "auto", spec, ctx.goal, str(ctx.repo_root))
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        cam, sim = await asyncio.gather(
            _run_cam(),
            _run_simulation(),
            return_exceptions=True,
        )

        ctx.cam_result = cam if isinstance(cam, dict) else {"error": str(cam)}
        ctx.simulation_result = sim if isinstance(sim, dict) else {"error": str(sim)}

        if isinstance(cam, dict) and cam.get("script_path"):
            print(f"  [Phase 4] CAM: {cam['script_path']}")
        if isinstance(sim, dict) and sim.get("passed") is not None:
            print(f"  [Phase 4] FEA: {'PASS' if sim['passed'] else 'FAIL'} SF={sim.get('safety_factor', '?')}")

        ctx.phases_completed.append("manufacturing")
        _emit(ctx, "phase_complete", "Phase 4 done", {"phase": 4})

    # -- Phase 5: Final Assembly + MillForge Bridge --------------------------

    async def _phase_5_finalize(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 5: Finalize", {"phase": 5})
        print(f"\n  [Phase 5] Finalizing...")

        # Record to memory system
        try:
            from .memory import record_generation
            spec = ctx.geometry_spec.get("spec", {})
            record_generation(
                part_type=spec.get("part_type", "unknown"),
                material=ctx.geometry_spec.get("material", ""),
                params=spec,
                passed=ctx.validation_passed,
                failures=ctx.validation_report.get("failures", []),
                bbox=ctx.validation_report.get("bbox"),
                cam_data=ctx.cam_result if isinstance(ctx.cam_result, dict) else None,
            )
        except Exception:
            pass

        # Build MillForge bridge job (if enabled)
        from .features import get_features
        if get_features().MILLFORGE_BRIDGE and ctx.validation_passed:
            ctx.millforge_job = self._build_millforge_job(ctx)
            ctx.save_artifact("millforge_job.json", ctx.millforge_job)
            print(f"  [Phase 5] MillForge job created: {ctx.millforge_job.get('aria_job_id')}")
        elif ctx.validation_passed:
            print(f"  [Phase 5] MillForge bridge disabled — job not submitted")
        else:
            print(f"  [Phase 5] Geometry invalid — no MillForge job")

        # Check if consolidation needed
        try:
            from .memory import should_consolidate, consolidate
            if should_consolidate():
                consolidate()
        except Exception:
            pass

        ctx.phases_completed.append("finalize")
        _emit(ctx, "phase_complete", "Phase 5 done", {"phase": 5})

    def _build_millforge_job(self, ctx: JobContext) -> dict[str, Any]:
        """Build the MillForge job data from ARIA outputs."""
        spec = ctx.geometry_spec.get("spec", {})
        cam = ctx.cam_result if isinstance(ctx.cam_result, dict) else {}

        # Compute geometry hash for dedup
        geo_hash = ""
        if ctx.geometry_path and Path(ctx.geometry_path).exists():
            data = Path(ctx.geometry_path).read_bytes()
            geo_hash = hashlib.sha256(data).hexdigest()[:16]

        return {
            "part_name": spec.get("part_type", "unknown_part"),
            "geometry_file": ctx.geometry_path,
            "toolpath_file": cam.get("script_path", ""),
            "material": ctx.geometry_spec.get("material", "unknown"),
            "estimated_cycle_time_minutes": cam.get("cycle_time_min", 0),
            "required_operations": [op.get("type", "") for op in cam.get("operations", [])],
            "tolerance_class": "standard",
            "aria_job_id": ctx.job_id,
            "generated_at": ctx.created_at.isoformat(),
            "geometry_hash": geo_hash,
            "validation_passed": ctx.validation_passed,
            "simulation_results": ctx.simulation_result if isinstance(ctx.simulation_result, dict) else None,
            "priority": 5,
            "quantity": 1,
        }

    def _print_summary(self, ctx: JobContext) -> None:
        """Print job summary."""
        print(f"\n{'=' * 64}")
        print(f"  COORDINATOR SUMMARY — Job {ctx.job_id}")
        print(f"{'=' * 64}")
        print(f"  Goal:       {ctx.goal}")
        print(f"  Phases:     {' → '.join(ctx.phases_completed)}")
        print(f"  Geometry:   {'PASS' if ctx.validation_passed else 'FAIL'}")
        if ctx.geometry_path:
            print(f"  STEP:       {ctx.geometry_path}")
        if ctx.cam_result.get("script_path"):
            print(f"  CAM:        {ctx.cam_result['script_path']}")
        if ctx.millforge_job:
            print(f"  MillForge:  Job {ctx.millforge_job.get('aria_job_id')}")
        print(f"  Time:       {ctx.total_time_s:.1f}s")
        if ctx.errors:
            print(f"  Errors:")
            for e in ctx.errors:
                print(f"    - {e}")
        print(f"{'=' * 64}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(ctx: JobContext, event_type: str, message: str, data: dict | None = None) -> None:
    """Emit SSE event with job context."""
    event_bus.emit(event_type, message, {
        **(data or {}),
        "job_id": ctx.job_id,
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_coordinator(goal: str, repo_root: Path | None = None) -> JobContext:
    """Run the full coordinator pipeline. Async entry point."""
    coordinator = CoordinatorAgent(repo_root)
    return await coordinator.run(goal)


def run_coordinator_sync(goal: str, repo_root: Path | None = None) -> JobContext:
    """Synchronous wrapper for the coordinator."""
    return asyncio.run(run_coordinator(goal, repo_root))
