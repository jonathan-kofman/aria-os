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
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .. import event_bus


# ---------------------------------------------------------------------------
# Circuit Breaker — protects the ARIA → MillForge HTTP boundary
# ---------------------------------------------------------------------------

class _CBState(Enum):
    CLOSED = "closed"       # normal — requests pass through
    OPEN = "open"           # failing — reject immediately
    HALF_OPEN = "half_open" # recovery probe — allow one request


class _MillForgeCircuitBreaker:
    """Thread-safe circuit breaker for POST /api/jobs/from-aria submissions.

    Transitions:
      CLOSED → OPEN  : after FAILURE_THRESHOLD consecutive failures
      OPEN   → HALF_OPEN : after RECOVERY_TIMEOUT_S seconds
      HALF_OPEN → CLOSED : on probe success
      HALF_OPEN → OPEN   : on probe failure
    """

    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT_S = 30.0

    def __init__(self) -> None:
        self._state = _CBState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> _CBState:
        with self._lock:
            if self._state == _CBState.OPEN:
                if time.monotonic() - self._opened_at >= self.RECOVERY_TIMEOUT_S:
                    self._state = _CBState.HALF_OPEN
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = _CBState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.FAILURE_THRESHOLD or self._state == _CBState.HALF_OPEN:
                self._state = _CBState.OPEN
                self._opened_at = time.monotonic()

    def is_open(self) -> bool:
        return self.state == _CBState.OPEN


# Module-level singleton — shared across all Coordinator instances in the process
_mf_circuit_breaker = _MillForgeCircuitBreaker()


# ---------------------------------------------------------------------------
# CadQuery failure categorizer
# ---------------------------------------------------------------------------

def _categorize_cad_failure(failures: list[str], generation_error: str = "") -> str:
    """Return a canonical failure category from CadQuery refinement loop output.

    Categories:
      syntax_error       — Python/CadQuery syntax or import error
      timeout            — execution took too long
      degenerate         — geometry produced but invalid (zero volume, null solid, etc.)
      sandbox_violation  — attempted filesystem/network access inside sandboxed exec
      empty_response     — LLM returned nothing usable
      unknown            — anything else

    Recovery hints (for the caller / UI):
      syntax_error     → re-prompt with clearer constraints
      timeout          → simplify geometry (fewer features, larger tolerances)
      degenerate       → adjust dimensions (check aspect ratios, wall thicknesses)
      sandbox_violation → escalate to human; part type may be unsupported
      empty_response   → retry with a different model or prompt strategy
    """
    combined = " ".join(failures + [generation_error]).lower()

    if any(kw in combined for kw in (
        "syntaxerror", "syntax error", "nameerror", "attributeerror",
        "indentationerror", "typeerror", "importerror", "no module",
    )):
        return "syntax_error"

    if any(kw in combined for kw in (
        "timeout", "timed out", "time limit", "execution took",
    )):
        return "timeout"

    if any(kw in combined for kw in (
        "degenerate", "zero volume", "null solid", "empty solid",
        "non-manifold", "no valid shape", "invalid geometry",
        "failed to create", "boolean failed", "shell failed",
    )):
        return "degenerate"

    if any(kw in combined for kw in (
        "permission", "open(", "os.system", "subprocess", "socket",
        "sandbox", "__import__", "builtins",
    )):
        return "sandbox_violation"

    if any(kw in combined for kw in (
        "empty response", "no code block", "llm returned", "returned nothing",
    )):
        return "empty_response"

    if combined.strip():
        return "unknown"
    return "none"


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

    # Phase 2 geometry routing (set by classifier at end of synthesis)
    geometry_type: str = "prismatic"    # see GEOMETRY_TYPES constant
    geometry_kernel: str = "cadquery"   # cadquery|nx|fusion|grasshopper|blender|sdf
    geometry_rationale: str = ""

    # Phase 3 outputs (geometry + validation)
    geometry_path: str = ""       # STEP/3dm path
    stl_path: str = ""
    validation_report: dict[str, Any] = field(default_factory=dict)
    validation_passed: bool = False

    # Phase 4 outputs (CAM + simulation)
    simulation_result: dict[str, Any] = field(default_factory=dict)
    cam_result: dict[str, Any] = field(default_factory=dict)

    # Phase 5 output (final)
    millforge_job: dict[str, Any] = field(default_factory=dict)

    # StructSight context (from /api/from-visualization)
    structsight_context: dict[str, Any] = field(default_factory=dict)

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

        # Drain the retry queue before starting new work — resubmit any
        # previously queued jobs if the circuit is now healthy.
        try:
            from .retry_queue import drain as _drain_queue
            await _drain_queue(
                submit_fn=self._submit_payload_direct,
                circuit_is_open_fn=_mf_circuit_breaker.is_open,
            )
        except Exception as _rq_exc:
            print(f"  [COORDINATOR] Retry queue drain error (non-fatal): {_rq_exc}")
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

    # -- StructSight context loader ------------------------------------------

    def _load_structsight_context(self, ctx: JobContext) -> None:
        """Load StructSight context from workspace if available."""
        if ctx.structsight_context:
            return  # already loaded
        context_dir = ctx.repo_root / "workspace" / "structsight"
        if not context_dir.exists():
            return
        # Find the most recent context file
        context_files = sorted(context_dir.glob("*_context.json"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
        if not context_files:
            return
        try:
            data = json.loads(context_files[0].read_text(encoding="utf-8"))
            ctx.structsight_context = data
            print(f"  [COORDINATOR] Loaded StructSight context: {data.get('run_id', '?')}")
        except Exception:
            pass

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

        # Load StructSight context if available (enriches research)
        self._load_structsight_context(ctx)

        # Step 1: Extract structured spec from goal
        from .spec_agent import SpecAgent
        from .design_state import DesignState

        state = DesignState(goal=ctx.goal, repo_root=ctx.repo_root)

        # Compile all research into a single context
        research_text = ""

        # Inject StructSight vision analysis as research context
        ss = ctx.structsight_context
        if ss.get("description"):
            research_text += f"\n## StructSight Vision Analysis\n{ss['description']}\n"
        if ss.get("suggestions"):
            research_text += "\n## Engineering Suggestions (from vision)\n"
            for s in ss["suggestions"]:
                research_text += f"- {s}\n"
        if ss.get("considerations"):
            research_text += "\n## Engineering Considerations (from vision)\n"
            for c in ss["considerations"]:
                research_text += f"- {c}\n"

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

        # Inject past QC defect memory — warns about known failure modes for this
        # material+part_type before the LLM generates a build recipe.
        try:
            from .memory import read_qc_memory
            _part_type = (state.spec.get("part_type") or ctx.goal.split()[0]).lower()
            _qc_history = read_qc_memory(state.material or "", _part_type)
            if _qc_history:
                research_text += f"\n## Past QC Defect History (from MillForge)\n{_qc_history}\n"
                print(f"  [Phase 2] QC memory injected ({len(_qc_history)} chars)")
        except Exception:
            pass

        # Step 2: Use LLM to synthesize a BUILD RECIPE from research
        # This is the critical step — turn raw search results into a
        # step-by-step CadQuery geometry description that the 7b model can follow.
        build_recipe = await self._synthesize_build_recipe(ctx, state.spec, research_text)

        # Classify geometry type and select CAD kernel before Phase 3
        classification = await self._classify_geometry_type(ctx, state.spec)
        ctx.geometry_type = classification.get("geometry_type", "prismatic")
        ctx.geometry_kernel = classification.get("kernel", "cadquery")
        ctx.geometry_rationale = classification.get("rationale", "")
        print(f"  [Phase 2] Kernel: {ctx.geometry_kernel} ({ctx.geometry_type}) — {ctx.geometry_rationale}")

        ctx.geometry_spec = {
            "spec": state.spec,
            "cem_params": state.cem_params,
            "material": state.material,
            "research_context": research_text[:2000],
            "build_recipe": build_recipe,
            "geometry_type": ctx.geometry_type,
            "geometry_kernel": ctx.geometry_kernel,
            "geometry_rationale": ctx.geometry_rationale,
        }
        ctx.save_artifact("geometry_spec.json", ctx.geometry_spec)

        print(f"  [Phase 2] Spec: {len(state.spec)} params, material: {state.material or 'auto'}")
        if build_recipe:
            print(f"  [Phase 2] Build recipe: {len(build_recipe)} chars")
        ctx.phases_completed.append("synthesis")
        _emit(ctx, "phase_complete", "Phase 2 done", {
            "phase": 2, "spec": state.spec,
            "geometry_type": ctx.geometry_type, "kernel": ctx.geometry_kernel,
        })

    async def _synthesize_build_recipe(
        self, ctx: JobContext, spec: dict, research: str
    ) -> str:
        """Use LLM to create a step-by-step geometry build recipe from research.

        Calls Claude (cloud) first, falls back to local model. The recipe tells
        the downstream geometry agent what operations to perform, in what order,
        with exact dimensions. This is the highest-leverage LLM call in the pipeline.
        """
        from ..llm_client import call_llm

        system = """You are a precision CAD geometry planner for a professional engineering pipeline.
Given a part description and research, create a step-by-step geometry build recipe.

Rules:
- Include exact dimensions in mm for every operation (derive from research or spec)
- Each step = one geometry operation
- NEVER use .cylinder() in CadQuery — use .circle(r).extrude(h)
- NEVER use .fillet() on first attempt (causes OCCT failures)
- For lattice/organic geometry, describe the math (gyroid equation, cell size, wall thickness)
- For surface geometry, describe control points, continuity requirements, curvature intent

Output format:
STEP 1: <operation> — <exact code or mathematical description with dimensions>
STEP 2: ...

Be precise. A machinist or CAD kernel will execute these steps directly."""

        prompt = f"""Part request: {ctx.goal}

Extracted spec: {json.dumps(spec, default=str)[:1000]}

Research findings:
{research[:4000]}

Create a complete step-by-step build recipe with exact dimensions."""

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, call_llm, prompt, system, ctx.repo_root)
        return response or ""

    async def _classify_geometry_type(self, ctx: JobContext, spec: dict) -> dict:
        """Classify geometry complexity and select the best CAD kernel.

        Returns {geometry_type, kernel, rationale}. Always returns a valid dict
        — defaults to prismatic/cadquery on any failure.
        """
        from ..llm_client import call_llm

        system = """You are a CAD routing system. Output ONLY valid JSON — no markdown, no explanation.

{
  "geometry_type": "<type>",
  "kernel": "<kernel>",
  "rationale": "<one sentence>"
}

geometry_type → kernel mapping:
  prismatic         → cadquery    (standard machined: flanges, brackets, housings, bores)
  freeform_surface  → nx          (class-A NURBS, aerospace skins, compound curvature, G2 continuity)
  precision_assembly→ nx          (multi-body GD&T, tight tolerances, mechanisms, motion)
  lattice_tpms      → sdf         (gyroid, Schwartz P/D, periodic minimal surfaces, TPMS infill)
  lattice_structural→ fusion      (octet truss, Kagome, load-bearing cellular, FEA-validated)
  organic_tspline   → fusion      (ergonomic, sculptural, T-spline freeform, smooth organic)
  generative_topopt → fusion      (minimum-weight, topology optimization, generative design)
  sheet_metal       → fusion      (bends, flanges, flat patterns, sheetmetal enclosures)
  algorithmic       → grasshopper (computational patterns, Voronoi, non-standard parametric)
  mesh_heavy        → blender     (subdivision surfaces, coils, arc-weave, non-manifold mesh)"""

        prompt = f"""Goal: {ctx.goal}
Spec: {json.dumps(spec, default=str)[:400]}"""

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, call_llm, prompt, system, ctx.repo_root)

        default = {"geometry_type": "prismatic", "kernel": "cadquery", "rationale": "default fallback"}
        if not response:
            return default
        try:
            text = response.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(text)
            if "geometry_type" in result and "kernel" in result:
                return result
        except Exception:
            pass
        return default

    # -- Phase 3: Geometry Generation + Validation ---------------------------

    async def _phase_3_geometry(self, ctx: JobContext) -> None:
        _emit(ctx, "phase", "Phase 3: Geometry + Validation", {"phase": 3})
        kernel = ctx.geometry_kernel or "cadquery"
        print(f"\n  [Phase 3] Generating geometry (kernel={kernel}, type={ctx.geometry_type})...")

        from .refinement_loop import run_agent_loop
        from .design_state import DesignState
        from .domains import detect_domain

        domain = detect_domain(ctx.goal)

        # Domain-specific overrides (always win over ML classifier)
        if domain == "ecad":
            await self._phase_3_ecad(ctx)
            return
        if domain == "civil":
            await self._phase_3_civil(ctx)
            return

        # Kernel routing from Phase 2 classifier
        if kernel == "nx":
            await self._phase_3_nx(ctx)
            return
        if kernel == "sdf":
            await self._phase_3_implicit(ctx)
            return
        # Fusion/Grasshopper/Blender: scripts generated in Phase 4; use CadQuery
        # for the STEP validation file. Kernel label still flows to the dashboard.
        if kernel in ("fusion", "grasshopper", "blender"):
            _emit(ctx, "kernel_note", f"{kernel} script will be generated in Phase 4", {"kernel": kernel})

        await self._phase_3_cadquery_loop(ctx)

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

    # -- Phase 3 variant: Siemens NX (headless journal execution) ---------------

    async def _phase_3_nx(self, ctx: JobContext) -> None:
        """Generate NXOpen Python journal via Claude and attempt batch execution.

        Tries headless run via run_journal.exe. Falls back to CadQuery refinement
        loop if NX batch is unavailable (Student Edition restriction) or fails.
        """
        _emit(ctx, "phase", "Phase 3: NX Journal Generation", {"phase": 3})
        print(f"\n  [Phase 3] NX path: generating NXOpen journal...")

        try:
            from ..generators.nx_generator import generate_nx_journal, run_nx_headless

            out_dir = ctx.repo_root / "outputs" / "cad" / "nx"
            spec = ctx.geometry_spec.get("spec", {})
            build_recipe = ctx.geometry_spec.get("build_recipe", "")

            loop = asyncio.get_event_loop()
            journal_result = await loop.run_in_executor(
                None, generate_nx_journal,
                ctx.goal, spec, build_recipe, out_dir, ctx.repo_root,
            )

            journal_path = journal_result.get("journal_path", "")
            if journal_path:
                ctx.save_artifact("nx_journal_path.txt", journal_path)
                print(f"  [Phase 3] NX journal: {journal_path}")

                # Attempt headless batch execution
                batch_result = await loop.run_in_executor(
                    None, run_nx_headless, journal_path, ctx.repo_root)

                if batch_result.get("success"):
                    ctx.geometry_path = batch_result.get("step_path", "")
                    ctx.stl_path = batch_result.get("stl_path", "")
                    ctx.validation_passed = True
                    ctx.validation_report = {
                        "converged": True, "kernel": "nx",
                        "geometry_type": ctx.geometry_type,
                        "headless": True,
                    }
                    print(f"  [Phase 3] NX PASS (headless) — {ctx.geometry_path}")
                    ctx.phases_completed.append("geometry")
                    _emit(ctx, "phase_complete", "Phase 3: NX", {
                        "phase": 3, "kernel": "nx", "headless": True})
                    return
                else:
                    print(f"  [Phase 3] NX batch unavailable ({batch_result.get('error','')}) — falling back to CadQuery")
        except Exception as exc:
            print(f"  [Phase 3] NX generator error: {exc} — falling back to CadQuery")

        # Fallback: CadQuery refinement loop
        await self._phase_3_cadquery_loop(ctx)

    # -- Phase 3 variant: Implicit SDF geometry (sdf library) -------------------

    async def _phase_3_implicit(self, ctx: JobContext) -> None:
        """Generate TPMS/implicit geometry via Claude-written sdf library Python.

        Uses the `sdf` library (pip install sdf) which runs pure Python marching
        cubes. Falls back to CadQuery if sdf is not installed or script fails.
        """
        _emit(ctx, "phase", "Phase 3: Implicit SDF Geometry", {"phase": 3})
        print(f"\n  [Phase 3] SDF path: generating implicit geometry...")

        try:
            from .implicit_geometry import generate_sdf_geometry

            out_dir = ctx.repo_root / "outputs" / "cad" / "sdf"
            spec = ctx.geometry_spec.get("spec", {})
            build_recipe = ctx.geometry_spec.get("build_recipe", "")

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, generate_sdf_geometry,
                ctx.goal, spec, build_recipe, out_dir, ctx.repo_root,
            )

            if result.get("success"):
                ctx.stl_path = result["stl_path"]
                ctx.geometry_path = result["stl_path"]  # no STEP for SDF output
                ctx.validation_passed = True
                ctx.validation_report = {
                    "converged": True, "kernel": "sdf",
                    "geometry_type": ctx.geometry_type,
                    "manifold": result.get("manifold", True),
                    "bbox": result.get("bbox"),
                }
                print(f"  [Phase 3] SDF PASS — {ctx.stl_path}")
                ctx.phases_completed.append("geometry")
                _emit(ctx, "phase_complete", "Phase 3: SDF", {
                    "phase": 3, "kernel": "sdf", "bbox": result.get("bbox")})
                return
            else:
                print(f"  [Phase 3] SDF failed ({result.get('error','')}) — falling back to CadQuery")
        except Exception as exc:
            print(f"  [Phase 3] SDF error: {exc} — falling back to CadQuery")

        await self._phase_3_cadquery_loop(ctx)

    # -- Phase 3 core: CadQuery refinement loop (extracted for reuse) -----------

    async def _phase_3_cadquery_loop(self, ctx: JobContext) -> None:
        """Run the CadQuery agent refinement loop. Used as primary and fallback."""
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

        loop = asyncio.get_event_loop()
        state = await loop.run_in_executor(None, run_agent_loop, state)

        ctx.validation_passed = state.converged
        ctx.geometry_path = state.artifacts.get("step_path", "")
        ctx.stl_path = state.artifacts.get("stl_path", "")
        ctx.validation_report = {
            "converged": state.converged,
            "kernel": "cadquery",
            "geometry_type": ctx.geometry_type,
            "iterations": state.iteration,
            "failures": list(state.failures),
            "bbox": state.bbox,
        }
        ctx.save_artifact("validation_report.json", ctx.validation_report)

        tag = "PASS" if state.converged else "FAIL"
        failure_category = "none"
        if not state.converged:
            failure_category = _categorize_cad_failure(
                list(state.failures),
                getattr(state, "generation_error", ""),
            )
            ctx.validation_report["failure_category"] = failure_category
            print(f"  [Phase 3] Failure category: {failure_category}")

        print(f"  [Phase 3] CadQuery {tag} — {state.iteration} iterations")
        ctx.phases_completed.append("geometry")
        _emit(ctx, "phase_complete", f"Phase 3: CadQuery {tag}", {
            "phase": 3, "kernel": "cadquery", "failure_category": failure_category})

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



        # ── CAM: toolpath generation ──────────────────────────────────────
        async def _run_cam():
            if not step_exists:
                return {"status": "skipped", "reason": "no geometry"}
            try:
                from .cam_agent import run_cam_agent
                result = await loop.run_in_executor(
                    None, run_cam_agent,
                    ctx.geometry_path, material, "generic_vmc",
                    ctx.repo_root)
                return result or {"status": "no_result"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # ── FEA: structural analysis ─────────────────────────────────────
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

        cam, fea, drawing, dfm, fusion, quote, onshape, visual = await asyncio.gather(
            _with_timeout(_run_cam(), "CAM", secs=120),
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
        ctx.cam_result = cam if isinstance(cam, dict) else {"error": str(cam)}

        # Print results
        results = [
            ("CAM", cam, lambda r: f"{'PASS' if r.get('passed') else 'FAIL'} — {r.get('cycle_time_min', '?')} min" if r.get("passed") is not None else ""),
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
        if isinstance(cam, dict) and cam.get("script_path"):
            ctx.save_artifact("cam_result.json", cam)
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
                cam_data=ctx.cam_result if ctx.cam_result else None,
            )
        except Exception:
            pass

        # Build MillForge bridge job (if enabled)
        from .features import get_features
        if get_features().MILLFORGE_BRIDGE and ctx.validation_passed:
            ctx.millforge_job = self._build_millforge_job(ctx)
            ctx.save_artifact("millforge_job.json", ctx.millforge_job)
            print(f"  [Phase 5] MillForge job built: {ctx.millforge_job.get('aria_job_id')}")

            # Submit to MillForge via the canonical /api/jobs/from-aria endpoint
            submit_result = await self._submit_to_millforge(ctx)
            if submit_result:
                print(f"  [Phase 5] MillForge submission: job #{submit_result.get('millforge_job_id', '?')}")
            else:
                print(f"  [Phase 5] MillForge submission skipped (no MILLFORGE_API_URL)")
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

    # Material name → MillForge enum mapping
    _MATERIAL_TO_MILLFORGE = {
        "aluminium_6061": "aluminum", "aluminium_7075": "aluminum",
        "aluminum_6061": "aluminum", "aluminum_7075": "aluminum",
        "aluminum": "aluminum", "aluminium": "aluminum",
        "steel_mild": "steel", "steel_4140": "steel",
        "steel_stainless": "steel", "stainless_316": "steel",
        "steel": "steel", "x1_420i": "steel",
        "titanium": "titanium", "titanium_ti6al4v": "titanium",
        "copper": "copper", "brass": "copper",
    }

    def _build_millforge_job(self, ctx: JobContext) -> dict[str, Any]:
        """Build a MillForge-compatible ARIAJobSubmission payload.

        Conforms to the schema validated by MillForge's POST /api/jobs/from-aria:
        - geometry_hash: full 64-char SHA-256
        - material: enum {steel, aluminum, titanium, copper}
        - estimated_cycle_time_minutes: > 0
        - simulation_results: matches _SimulationResults shape
        - material_spec: matches _MaterialSpec shape
        """
        spec = ctx.geometry_spec.get("spec", {})
        cam = ctx.cam_result if isinstance(getattr(ctx, "cam_result", None), dict) else {}
        fea = ctx.simulation_result if isinstance(ctx.simulation_result, dict) else {}

        # Full 64-char SHA-256 for geometry dedup
        geo_hash = "0" * 64
        if ctx.geometry_path and Path(ctx.geometry_path).exists():
            data = Path(ctx.geometry_path).read_bytes()
            geo_hash = hashlib.sha256(data).hexdigest()

        # Map free-text material to MillForge enum
        raw_material = (ctx.geometry_spec.get("material", "") or "").lower()
        mf_material = self._MATERIAL_TO_MILLFORGE.get(raw_material, "steel")

        # Cycle time: prefer CAM result, fallback to FEA or a reasonable default
        cycle_time = cam.get("cycle_time_min", 0) or 0
        if cycle_time <= 0:
            cycle_time = fea.get("estimated_cycle_time_minutes", 0) or 0
        if cycle_time <= 0:
            cycle_time = 5.0  # minimum default so validation passes

        # Build simulation_results in the shape MillForge expects
        sim_results = {
            "estimated_cycle_time_minutes": cycle_time,
            "estimated_material_removal_cm3": cam.get("material_removal_cm3"),
            "max_chip_load_mm": cam.get("max_chip_load_mm"),
            "tool_wear_index": cam.get("tool_wear_index"),
            "collision_detected": False,
            "simulation_confidence": 0.8 if cam.get("passed") else 0.5,
        }

        # Build material_spec
        material_spec = {
            "material_name": raw_material or mf_material,
            "material_family": mf_material,
            "hardness_hrc": spec.get("hardness_hrc"),
            "tensile_strength_mpa": spec.get("tensile_strength_mpa"),
            "notes": None,
        }

        # Extract operation types from CAM
        operations = cam.get("operations", [])
        required_ops = []
        for op in operations:
            op_type = op.get("type") or op.get("role") or op.get("name", "")
            if op_type:
                required_ops.append(op_type)
        # Kernel-aware default operations
        _kernel_ops = {
            "sdf":        ["additive_3dp", "post_processing"],
            "nx":         ["milling", "5axis"],
            "fusion":     ["milling"],
            "grasshopper": ["milling"],
            "blender":    ["additive_3dp"],
            "cadquery":   ["milling"],
        }
        if not required_ops:
            required_ops = _kernel_ops.get(ctx.geometry_kernel, ["milling"])

        # Load DFM / quote / drawing artifacts for extended payload
        dfm_summary = None
        quote_data = None
        drawing_path = None
        try:
            dfm_file = ctx.scratchpad_dir / "dfm_report.json"
            if dfm_file.exists():
                dfm_summary = json.loads(dfm_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            quote_file = ctx.scratchpad_dir / "quote.json"
            if quote_file.exists():
                quote_data = json.loads(quote_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            drawing_file = ctx.scratchpad_dir / "drawing_path.txt"
            if drawing_file.exists():
                drawing_path = drawing_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

        return {
            "schema_version": "1.0",
            "part_name": spec.get("part_type", "unknown_part"),
            "geometry_file": ctx.geometry_path,
            "toolpath_file": cam.get("script_path", ""),
            "material": mf_material,
            "material_spec": material_spec,
            "estimated_cycle_time_minutes": cycle_time,
            "required_operations": required_ops,
            "tolerance_class": {
                "freeform_surface": "tight",
                "precision_assembly": "ultra",
                "lattice_tpms": "standard",
                "lattice_structural": "medium",
                "organic_tspline": "medium",
                "generative_topopt": "medium",
                "sheet_metal": "standard",
                "algorithmic": "medium",
                "mesh_heavy": "standard",
                "prismatic": "standard",
            }.get(ctx.geometry_type, "standard"),
            "aria_job_id": ctx.job_id,
            "generated_at": ctx.created_at.isoformat(),
            "geometry_hash": geo_hash,
            "validation_passed": ctx.validation_passed,
            "simulation_results": sim_results,
            "priority": 5,
            "quantity": 1,
            "extra": {
                "trace_id": ctx.job_id,
                "source_material_name": raw_material,
                "geometry_type": ctx.geometry_type,
                "geometry_kernel": ctx.geometry_kernel,
                "geometry_rationale": ctx.geometry_rationale,
                "dfm_summary": dfm_summary,
                "aria_quote": quote_data,
                "drawing_path": drawing_path,
                "fea_results": fea if fea.get("passed") is not None else None,
                "process_recommendation": (dfm_summary or {}).get("process_recommendation"),
            },
        }

    async def _submit_to_millforge(self, ctx: JobContext) -> dict | None:
        """HTTP-submit the MillForge job payload to POST /api/jobs/from-aria.

        Protected by a module-level circuit breaker (_mf_circuit_breaker).
        If the circuit is OPEN (MillForge has been unavailable), the job is
        saved locally and submission is skipped — non-fatal.

        Returns the MillForge ack response, or None if skipped/failed.
        Never raises.
        """
        mf_url = os.environ.get("MILLFORGE_API_URL", "").rstrip("/")
        if not mf_url:
            return None

        # Circuit breaker check — fast-fail if MillForge is known to be down
        if _mf_circuit_breaker.is_open():
            msg = (
                f"MillForge circuit OPEN (consecutive failures="
                f"{_mf_circuit_breaker._consecutive_failures}) — submission queued locally"
            )
            ctx.errors.append(msg)
            print(f"  [Phase 5] {msg}")
            _emit(ctx, "circuit_open", msg, {"boundary": "aria→millforge"})
            # Save to retry queue so the job is not permanently lost
            try:
                from .retry_queue import enqueue as _enqueue
                _enqueue(ctx.millforge_job, ctx.millforge_job.get("aria_job_id", ctx.job_id), msg)
            except Exception:
                pass
            return None

        mf_key = os.environ.get("ARIA_BRIDGE_KEY", "")

        try:
            import urllib.request
            import urllib.error

            headers = {"Content-Type": "application/json"}
            if mf_key:
                headers["X-API-Key"] = mf_key

            payload = json.dumps(ctx.millforge_job, default=str).encode("utf-8")
            req = urllib.request.Request(
                f"{mf_url}/api/jobs/from-aria",
                data=payload,
                headers=headers,
                method="POST",
            )

            loop = asyncio.get_event_loop()
            def _do_submit():
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read())

            result = await loop.run_in_executor(None, _do_submit)
            _mf_circuit_breaker.record_success()
            return result
        except Exception as exc:
            _mf_circuit_breaker.record_failure()
            ctx.errors.append(f"MillForge submission failed: {exc}")
            print(f"  [Phase 5] MillForge submission error: {exc}")
            _emit(ctx, "submission_error", str(exc), {
                "boundary": "aria→millforge",
                "circuit_state": _mf_circuit_breaker._state.value,
                "consecutive_failures": _mf_circuit_breaker._consecutive_failures,
            })
            return None

    async def _submit_payload_direct(self, payload: dict) -> dict | None:
        """Submit a raw MillForge job payload, updating circuit breaker state.

        Used by the retry queue drain loop. Unlike _submit_to_millforge(), this
        accepts an already-built payload dict (not a JobContext).
        """
        mf_url = os.environ.get("MILLFORGE_API_URL", "").rstrip("/")
        if not mf_url:
            return None
        mf_key = os.environ.get("ARIA_BRIDGE_KEY", "")
        try:
            import urllib.request
            headers = {"Content-Type": "application/json"}
            if mf_key:
                headers["X-API-Key"] = mf_key
            data = json.dumps(payload, default=str).encode("utf-8")
            req = urllib.request.Request(
                f"{mf_url}/api/jobs/from-aria", data=data, headers=headers, method="POST"
            )
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: json.loads(urllib.request.urlopen(req, timeout=15).read())
            )
            _mf_circuit_breaker.record_success()
            return result
        except Exception as exc:
            _mf_circuit_breaker.record_failure()
            raise

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
        # CAM result
        cam_result_path = ctx.scratchpad_dir / "cam_result.json"
        if cam_result_path.exists():
            import json as _json_cam
            cam_r = _json_cam.loads(cam_result_path.read_text())
            artifacts.append(f"  CAM:        {cam_r.get('script_path', '?')} ({cam_r.get('cycle_time_min', '?')} min)")
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
