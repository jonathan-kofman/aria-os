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

    async def run(self, goal: str, *, force_assembly: bool = False) -> JobContext:
        """Execute the full 5-phase pipeline.

        Parameters
        ----------
        goal : str
            Natural-language description of the part or assembly.
        force_assembly : bool
            If True, always treat the goal as a multi-part assembly
            (equivalent to the ``--assembly`` CLI flag).
        """
        ctx = JobContext(goal=goal, repo_root=self.repo_root)
        t0 = time.time()

        _emit(ctx, "coordinator", f"Job {ctx.job_id} started", {"goal": goal})
        print(f"\n{'=' * 64}")
        print(f"  COORDINATOR — Job {ctx.job_id}")
        print(f"  Goal: {goal}")
        print(f"{'=' * 64}")

        # ── Assembly detection ──────────────────────────────────────────
        # Check if the goal describes a multi-part assembly. If so, hand
        # off to the AssemblyAgent which decomposes, generates each part
        # via *this* coordinator, and creates the assembly config JSON.
        from .assembly_agent import is_assembly_goal
        if force_assembly or is_assembly_goal(goal):
            print(f"  [COORDINATOR] Assembly detected — delegating to AssemblyAgent")
            return await self._run_assembly(ctx)

        try:
            # Phase 1: Parallel research
            await self._phase_1_research(ctx)

            # Phase 2: Synthesize geometry spec
            await self._phase_2_synthesize(ctx)

            # Phase 3: Generate + validate geometry (with refinement loop)
            await self._phase_3_geometry(ctx)

            # Phase 4: Run manufacturing outputs if geometry EXISTS (even with warnings)
            # A part with bbox warnings is still valid geometry — don't block Onshape/DFM/Quote
            _has_geometry = ctx.geometry_path and Path(ctx.geometry_path).exists()
            if _has_geometry:
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

    # -- Assembly delegation ---------------------------------------------------

    async def _run_assembly(self, ctx: JobContext) -> JobContext:
        """Delegate to AssemblyAgent for multi-part assembly generation."""
        from .assembly_agent import AssemblyAgent

        _emit(ctx, "phase", "Assembly: decompose + generate all parts", {"phase": "assembly"})

        try:
            agent = AssemblyAgent(self.repo_root)
            result = await agent.run(ctx.goal)

            # Map assembly result back into JobContext
            ctx.phases_completed.append("assembly")

            if result.get("config_path"):
                ctx.save_artifact("assembly_config.json",
                                  json.loads(Path(result["config_path"]).read_text(encoding="utf-8")))

            if result.get("assembly_step") and Path(result["assembly_step"]).exists():
                ctx.geometry_path = result["assembly_step"]
                ctx.validation_passed = True
            else:
                # At least some parts may have generated
                generated = [p for p in result.get("parts", []) if p.get("step_path")]
                ctx.validation_passed = len(generated) > 0
                if generated:
                    ctx.geometry_path = generated[0]["step_path"]

            ctx.validation_report = {
                "assembly_name": result.get("name", ""),
                "config_path": result.get("config_path", ""),
                "parts_total": len(result.get("parts", [])),
                "parts_passed": sum(1 for p in result.get("parts", []) if p.get("passed")),
                "assembly_step": result.get("assembly_step", ""),
                "errors": result.get("errors", []),
            }
            ctx.save_artifact("assembly_result.json", ctx.validation_report)

            if result.get("errors"):
                for e in result["errors"]:
                    ctx.errors.append(e)

        except Exception as exc:
            ctx.errors.append(f"Assembly agent error: {exc}")
            _emit(ctx, "error", f"Assembly failed: {exc}")
            print(f"  [COORDINATOR] Assembly ERROR: {exc}")

        import time as _time
        ctx.total_time_s = _time.time() - ctx.created_at.timestamp()
        self._print_summary(ctx)
        return ctx

    # -- Phase 1: Parallel Research ------------------------------------------

    async def _phase_1_research(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 1: Research (parallel)", {"phase": 1})

        # Skip research when the user already provided enough dimensions.
        # If spec extraction gets >=4 params, research adds latency but not value.
        try:
            from ..spec_extractor import extract_spec
            _quick_spec = extract_spec(ctx.goal)
            _n_dims = sum(1 for k, v in _quick_spec.items()
                         if k.endswith("_mm") and v is not None)
            if _n_dims >= 4:
                print(f"\n  [Phase 1] Skipping research — {_n_dims} dimensions already specified")
                ctx.phases_completed.append("research")
                _emit(ctx, "phase_complete", "Phase 1 skipped (dims sufficient)", {"phase": 1})
                return
        except Exception:
            pass

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

        # ECAD domain: use dedicated generator instead of CadQuery pipeline
        if domain == "ecad":
            await self._phase_3_ecad(ctx)
            return

        # Civil/AutoCAD domain: use DXF generator
        if domain == "civil":
            await self._phase_3_civil(ctx)
            return

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

    # -- Phase 3 variants: ECAD and Civil domains --------------------------------

    async def _phase_3_ecad(self, ctx: JobContext) -> None:
        """Generate KiCad PCB layout for ECAD domain."""
        _emit(ctx, "phase", "Phase 3: ECAD Generation", {"phase": 3})
        print(f"\n  [Phase 3] Generating ECAD (KiCad PCB)...")

        try:
            from ..ecad.ecad_generator import generate_ecad
            loop = asyncio.get_event_loop()
            out_dir = ctx.repo_root / "outputs" / "ecad"
            raw = await loop.run_in_executor(
                None, generate_ecad, ctx.goal, str(out_dir))

            # generate_ecad returns (script_path, bom_path) tuple
            if isinstance(raw, tuple):
                result = {"script_path": str(raw[0]), "bom_path": str(raw[1]) if len(raw) > 1 else ""}
            else:
                result = raw or {}

            if result and result.get("script_path"):
                ctx.geometry_path = result["script_path"]
                ctx.validation_passed = not bool(result.get("erc_errors"))
                ctx.validation_report = {
                    "converged": ctx.validation_passed,
                    "erc_errors": result.get("erc_errors", []),
                    "drc_errors": result.get("drc_errors", []),
                    "components": result.get("n_components", 0),
                }
                ctx.save_artifact("ecad_result.json", ctx.validation_report)

                tag = "PASS" if ctx.validation_passed else "FAIL"
                n_comp = result.get("n_components", 0)
                print(f"  [Phase 3] ECAD {tag} — {n_comp} components, script: {result['script_path']}")
            else:
                ctx.validation_passed = False
                print(f"  [Phase 3] ECAD generation failed")

        except Exception as exc:
            ctx.validation_passed = False
            print(f"  [Phase 3] ECAD error: {exc}")

        ctx.phases_completed.append("geometry")
        _emit(ctx, "phase_complete", f"Phase 3: ECAD", {"phase": 3})

    async def _phase_3_civil(self, ctx: JobContext) -> None:
        """Generate civil engineering DXF for AutoCAD domain."""
        _emit(ctx, "phase", "Phase 3: Civil DXF Generation", {"phase": 3})
        print(f"\n  [Phase 3] Generating Civil DXF...")

        try:
            from ..autocad.dxf_exporter import generate_civil_dxf
            import re

            # Extract state and discipline from goal
            goal_lower = ctx.goal.lower()
            state = "TX"  # default
            for s in ["tx", "ca", "ny", "fl", "co", "nj", "oh", "pa", "il", "wa"]:
                if s in goal_lower or s.upper() in ctx.goal:
                    state = s.upper()
                    break

            discipline = "transportation"  # default
            for d, keywords in [
                ("drainage", ["drainage", "storm", "drain", "sewer", "pipe"]),
                ("grading", ["grading", "grade", "earthwork", "contour"]),
                ("utilities", ["utility", "water main", "gas"]),
                ("site", ["site", "parking", "building"]),
            ]:
                if any(kw in goal_lower for kw in keywords):
                    discipline = d
                    break

            out_dir = ctx.repo_root / "outputs" / "cad" / "dxf"
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, generate_civil_dxf, ctx.goal, state, discipline, str(out_dir))

            if result:
                ctx.geometry_path = str(result) if isinstance(result, (str, Path)) else ""
                ctx.validation_passed = True
                ctx.validation_report = {
                    "converged": True, "state": state, "discipline": discipline}
                print(f"  [Phase 3] Civil DXF: {ctx.geometry_path}")
            else:
                ctx.validation_passed = False

        except Exception as exc:
            ctx.validation_passed = False
            print(f"  [Phase 3] Civil error: {exc}")

        ctx.phases_completed.append("geometry")
        _emit(ctx, "phase_complete", "Phase 3: Civil", {"phase": 3})

    # -- Phase 4: Parallel Manufacturing Outputs --------------------------------
    # Run ALL output domains in parallel: CAM + FEA + Drawing + Fusion + DFM

    async def _phase_4_manufacturing(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 4: Manufacturing outputs (parallel)", {"phase": 4})
        print(f"\n  [Phase 4] Generating all outputs in parallel...")

        loop = asyncio.get_event_loop()
        step_exists = ctx.geometry_path and Path(ctx.geometry_path).exists()
        spec = ctx.geometry_spec.get("spec", {})
        material = ctx.geometry_spec.get("material", "aluminium_6061")
        part_id = spec.get("part_type", "agent_part")



        # ── FEA: structural analysis ─────────────────────────────────────
        async def _run_fea():
            try:
                from ..physics_analyzer import analyze
                result = await loop.run_in_executor(
                    None, analyze,
                    part_id, "auto", spec, ctx.goal, str(ctx.repo_root))
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # ── GD&T Drawing: engineering drawing SVG ────────────────────────
        async def _run_drawing():
            if not step_exists:
                return {"status": "skipped", "reason": "no geometry"}
            try:
                from ..drawing_generator import generate_gdnt_drawing
                drawing_path = await loop.run_in_executor(
                    None, generate_gdnt_drawing,
                    ctx.geometry_path, part_id, spec, ctx.repo_root)
                return {"status": "ok", "path": str(drawing_path)}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # ── DFM: manufacturability analysis ──────────────────────────────
        async def _run_dfm():
            if not step_exists:
                return {"status": "skipped", "reason": "no geometry"}
            try:
                from .dfm_agent import run_dfm_analysis
                result = await loop.run_in_executor(
                    None, run_dfm_analysis, ctx.geometry_path, ctx.goal)
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # ── Fusion 360: parametric script ────────────────────────────────
        async def _run_fusion():
            if not step_exists:
                return {"status": "skipped", "reason": "no geometry"}
            try:
                from ..generators.fusion_generator import write_fusion_artifacts
                result = await loop.run_in_executor(
                    None, write_fusion_artifacts,
                    ctx.geometry_spec.get("plan", {}), ctx.goal,
                    ctx.geometry_path, ctx.stl_path or "",
                    ctx.repo_root)
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # ── Quote: cost estimation ───────────────────────────────────────
        async def _run_quote():
            if not step_exists:
                return {"status": "skipped", "reason": "no geometry"}
            try:
                from .quote_agent import QuoteAgent
                qa = QuoteAgent()
                result = await loop.run_in_executor(
                    None, qa.quote, ctx.geometry_path, material)
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # ── Onshape: live parametric part ────────────────────────────────
        async def _run_onshape():
            try:
                from .onshape_bridge import is_onshape_available, create_onshape_part
                if not is_onshape_available():
                    return {"status": "skipped", "reason": "ONSHAPE_ACCESS_KEY not set"}
                part_name = f"ARIA-OS: {ctx.goal[:50]}"
                _step = str(ctx.geometry_path) if ctx.geometry_path else ""
                result = await loop.run_in_executor(
                    None, create_onshape_part, part_name, spec, ctx.goal, _step)
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # Execute ALL 6 outputs in parallel with 90s timeout per task
        async def _with_timeout(coro, name, secs=90):
            try:
                return await asyncio.wait_for(coro, timeout=secs)
            except asyncio.TimeoutError:
                return {"status": "error", "error": f"{name} timed out after {secs}s"}

        # ── Visual verification: Claude vision checks rendered geometry ───
        async def _run_visual_verify():
            if not step_exists:
                return {"status": "skipped"}
            try:
                from ..visual_verifier import verify_visual
                _sp = Path(ctx.geometry_path)
                stl = str(_sp.parent.parent / "stl" / _sp.name.replace(".step", ".stl"))
                result = await loop.run_in_executor(
                    None, verify_visual, str(ctx.geometry_path), stl, ctx.goal, spec)
                return result or {"status": "no_result"}
            except ImportError:
                return {"status": "skipped", "reason": "visual_verifier not installed"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        fea, drawing, dfm, fusion, quote, onshape, visual = await asyncio.gather(
            _with_timeout(_run_fea(), "FEA"),
            _with_timeout(_run_drawing(), "Drawing"),
            _with_timeout(_run_dfm(), "DFM"),
            _with_timeout(_run_fusion(), "Fusion"),
            _with_timeout(_run_quote(), "Quote"),
            _with_timeout(_run_onshape(), "Onshape"),
            _with_timeout(_run_visual_verify(), "Visual", secs=120),
            return_exceptions=True,
        )

        # Store results
        ctx.simulation_result = fea if isinstance(fea, dict) else {"error": str(fea)}

        # Print results
        results = [
            ("FEA", fea, lambda r: f"{'PASS' if r.get('passed') else 'FAIL'} SF={r.get('safety_factor', '?')}" if r.get("passed") is not None else ""),
            ("Drawing", drawing, lambda r: r.get("path", "")),
            ("DFM", dfm, lambda r: f"Score: {r.get('score', '?')}/100 — {r.get('process_recommendation', '')}" if r.get("score") else ""),
            ("Fusion", fusion, lambda r: r.get("script_path", "")),
            ("Quote", quote, lambda r: f"${r.get('unit_cost_usd', 0):.2f}" if r.get("unit_cost_usd") else ""),
            ("Onshape", onshape, lambda r: r.get("url", "")),
            ("Visual", visual, lambda r: (
                f"{'PASS' if r.get('verified') else 'ISSUES'} "
                f"({r.get('confidence', 0)*100:.0f}% confidence, "
                f"{sum(1 for c in r.get('checks', []) if c.get('found'))}"
                f"/{len(r.get('checks', []))} features)"
            ) if r.get("verified") is not None else ""),
        ]
        for name, result, fmt in results:
            if isinstance(result, dict):
                if result.get("status") == "skipped":
                    continue
                if result.get("status") == "error" or result.get("error"):
                    print(f"  [Phase 4] {name}: error — {result.get('error', '')[:80]}")
                else:
                    detail = fmt(result)
                    if detail:
                        print(f"  [Phase 4] {name}: {detail}")
            elif isinstance(result, Exception):
                print(f"  [Phase 4] {name}: exception — {result}")

        # Store in context for Phase 5
        if isinstance(dfm, dict) and dfm.get("score"):
            ctx.save_artifact("dfm_report.json", dfm)
        if isinstance(quote, dict) and quote.get("unit_cost_usd"):
            ctx.save_artifact("quote.json", quote)
        if isinstance(drawing, dict) and drawing.get("path"):
            ctx.save_artifact("drawing_path.txt", drawing["path"])
        if isinstance(fusion, dict) and fusion.get("script_path"):
            ctx.save_artifact("fusion_script_path.txt", fusion["script_path"])
        if isinstance(onshape, dict) and onshape.get("url"):
            ctx.save_artifact("onshape_url.txt", onshape["url"])
        if isinstance(visual, dict) and visual.get("verified") is not None:
            ctx.save_artifact("visual_verification.json", visual)

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
        """Print job summary with all output artifacts."""
        print(f"\n{'=' * 64}")
        print(f"  COORDINATOR SUMMARY — Job {ctx.job_id}")
        print(f"{'=' * 64}")
        print(f"  Goal:       {ctx.goal}")
        print(f"  Phases:     {' -> '.join(ctx.phases_completed)}")
        print(f"  Geometry:   {'PASS' if ctx.validation_passed else 'FAIL'}")

        # List all output artifacts
        artifacts = []
        if ctx.geometry_path and Path(ctx.geometry_path).exists():
            sz = Path(ctx.geometry_path).stat().st_size // 1024
            artifacts.append(f"  STEP:       {ctx.geometry_path} ({sz}KB)")
        if ctx.stl_path and Path(ctx.stl_path).exists():
            sz = Path(ctx.stl_path).stat().st_size // 1024
            artifacts.append(f"  STL:        {ctx.stl_path} ({sz}KB)")

        # Check scratchpad for drawing
        drawing_path = ctx.scratchpad_dir / "drawing_path.txt"
        if drawing_path.exists():
            artifacts.append(f"  Drawing:    {drawing_path.read_text().strip()}")
        # Check for fusion script
        fusion_path = ctx.scratchpad_dir / "fusion_script_path.txt"
        if fusion_path.exists():
            artifacts.append(f"  Fusion 360: {fusion_path.read_text().strip()}")
        # DFM report
        dfm_path = ctx.scratchpad_dir / "dfm_report.json"
        if dfm_path.exists():
            import json as _json
            dfm = _json.loads(dfm_path.read_text())
            artifacts.append(f"  DFM:        Score {dfm.get('score', '?')}/100 — {dfm.get('process_recommendation', '?')}")
        # Quote
        quote_path = ctx.scratchpad_dir / "quote.json"
        if quote_path.exists():
            import json as _json2
            q = _json2.loads(quote_path.read_text())
            artifacts.append(f"  Quote:      ${q.get('unit_cost_usd', 0):.2f} ({q.get('process', '?')})")
        # Onshape
        onshape_url = ctx.scratchpad_dir / "onshape_url.txt"
        if onshape_url.exists():
            artifacts.append(f"  Onshape:    {onshape_url.read_text().strip()}")
        # MillForge
        if ctx.millforge_job:
            artifacts.append(f"  MillForge:  Job {ctx.millforge_job.get('aria_job_id')}")

        for a in artifacts:
            print(a)

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
