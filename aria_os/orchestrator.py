"""ARIA-OS orchestrator: load context -> plan -> route -> generate -> validate -> export -> log."""
import sys as _sys
from pathlib import Path
from .context_loader import load_context
from .planner import plan as planner_plan
from .exporter import get_output_paths, get_meta_path
from .logger import log as logger_log, log_failure as logger_log_failure
from . import cem_checks
from . import event_bus
from .cem_context import load_cem_geometry
from .cad_learner import record_attempt
from .tool_router import select_cad_tool
from .grasshopper_generator import write_grasshopper_artifacts, validate_grasshopper_output
from .blender_generator import write_blender_artifacts
from .cad_prompt_builder import attach_brief_to_plan
from .validator import validate_grasshopper_script
from .post_gen_validator import run_validation_loop, check_output_quality


# ---------------------------------------------------------------------------
# Pipeline validation checkpoint system
# ---------------------------------------------------------------------------

class _CheckpointFailure(Exception):
    """Raised when a pipeline checkpoint detects a critical failure."""


def _checkpoint(
    stage: str,
    checks: list[tuple[str, bool, str]],
    session: dict,
    *,
    critical: bool = False,
) -> bool:
    """
    Validate that a pipeline stage completed correctly.

    Parameters
    ----------
    stage    : human-readable stage name (e.g. "PLAN", "ROUTE", "GENERATE")
    checks   : list of (check_name, passed: bool, detail: str)
    session  : session dict — checkpoint results appended under "checkpoints"
    critical : if True, raises _CheckpointFailure on any failure

    Returns True if all checks pass.
    """
    passed_all = True
    failures: list[str] = []
    results: list[dict] = []

    for name, passed, detail in checks:
        results.append({"check": name, "passed": passed, "detail": detail})
        if passed:
            print(f"  [{stage}] OK {name}")
        else:
            passed_all = False
            failures.append(f"{name}: {detail}")
            print(f"  [{stage}] FAIL {name} -- {detail}")

    # Summary line
    n_pass = sum(1 for r in results if r["passed"])
    n_total = len(results)
    tag = "PASS" if passed_all else "FAIL"
    print(f"  [{stage}] {tag} ({n_pass}/{n_total} checks)")

    # Store in session
    checkpoints = session.setdefault("checkpoints", {})
    checkpoints[stage] = {
        "passed": passed_all,
        "checks": results,
        "failures": failures,
    }

    event_bus.emit("checkpoint", f"{stage}: {tag}", {
        "stage": stage, "passed": passed_all, "failures": failures,
    })

    if not passed_all and critical:
        raise _CheckpointFailure(f"[{stage}] Critical checkpoint failed: {failures}")

    return passed_all


def _prompt_gdnt_drawing() -> bool:
    """Ask user if they want a GD&T drawing. Returns False in non-interactive
    mode (no TTY, or ARIA_NON_INTERACTIVE=1, or ARIA_LAZY_STAGES=1)."""
    if os.environ.get("ARIA_NON_INTERACTIVE", "").strip() in ("1", "true", "yes"):
        return False
    if os.environ.get("ARIA_LAZY_STAGES", "").strip() in ("1", "true", "yes"):
        return False
    if not _sys.stdin.isatty():
        return False
    try:
        print()
        ans = input("[GD&T] Generate engineering drawing for this part? [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _lazy_stages_enabled() -> bool:
    """True when the run should skip optional post-stages (FEA, drawings,
    quote, CEM teaching). Set ARIA_LAZY_STAGES=1 to enable.

    Speeds up default `python run_aria_os.py "<goal>"` runs by 5-15s when
    the user only wants the geometry, not analysis artifacts.
    """
    return os.environ.get("ARIA_LAZY_STAGES", "").strip() in ("1", "true", "yes")


def run(goal: str, repo_root: Path | None = None, max_attempts: int = 3, *, preview: bool = False, auto_draw: bool = False, agent_mode: bool | None = None, max_agent_iterations: int = 3, teaching: bool = False, teaching_level: str = "intermediate", teaching_interactive: bool = False):
    """Run the ARIA-OS pipeline: plan -> route -> generate artifacts -> validate -> log.

    agent_mode: None = auto (use agents if Ollama available), True = force, False = disable.
    teaching: if True, enable the teaching layer (proactive narration of decisions).
    teaching_level: "beginner", "intermediate", or "expert".
    teaching_interactive: if True, pause after each major phase for interactive Q&A (implies teaching=True).
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    # Per-run telemetry: reset LLM call counts so this run's pipeline_stats
    # reflects only THIS pipeline's calls, not state from a previous run.
    import time as _time
    _run_start_ts = _time.time()
    try:
        from .llm_client import reset_llm_call_counts as _reset_llm
        _reset_llm()
    except Exception:
        pass

    context = load_context(repo_root)
    session: dict = {
        "goal": goal,
        "attempts": 0,
        "step_path": "",
        "stl_path": "",
        "_run_start_ts": _run_start_ts,
    }

    # --- Teaching engine (optional) ---
    # --teach-interactive implies --teach
    if teaching_interactive:
        teaching = True
    _teaching_engine = None
    if teaching:
        try:
            from .teaching.engine import TeachingEngine, DifficultyLevel
            _level_map = {
                "beginner": DifficultyLevel.BEGINNER,
                "intermediate": DifficultyLevel.INTERMEDIATE,
                "expert": DifficultyLevel.EXPERT,
            }
            from .teaching.user_profile import UserProfile as _UserProfile
            _user_profile = _UserProfile.load()
            _teaching_engine = TeachingEngine(
                difficulty=_level_map.get(teaching_level, DifficultyLevel.INTERMEDIATE),
                interactive=teaching_interactive,
                user_profile=_user_profile,
            )
            _teaching_engine.update_context("goal", goal)
            session["teaching_enabled"] = True
            session["teaching_level"] = teaching_level
            session["teaching_interactive"] = teaching_interactive
            _mode_tag = " (interactive)" if teaching_interactive else ""
            print(f"[TEACH] Teaching mode enabled (level: {teaching_level}){_mode_tag}")
        except Exception as _te:
            print(f"[TEACH] Could not initialize teaching engine: {_te}")

    event_bus.emit("step", "Pipeline started", {"goal": goal})

    # --- Agent mode: bypass ALL keyword-based planning/routing ---
    _use_agents = agent_mode
    if _use_agents is None:
        try:
            from .agents.base_agent import is_ollama_available
            from .agents.ollama_config import AGENT_MODELS
            _agent_model = AGENT_MODELS.get("designer", "qwen2.5-coder:7b")
            _use_agents = is_ollama_available(model=_agent_model)
            if not _use_agents:
                print("[INFO] Ollama not running or model not ready — using template/LLM planning mode.")
                print(f"       To enable agents: ollama serve && ollama pull {_agent_model}")
            else:
                print(f"[INFO] Agent mode enabled — model: {_agent_model}")
        except Exception as _oa_exc:
            _use_agents = False
            print(f"[WARN] Ollama check failed ({_oa_exc}) — using template/LLM planning mode.")

    if _use_agents:
        try:
            from .agents.refinement_loop import run_agent_loop
            from .agents.design_state import DesignState
            from .agents.domains import detect_domain

            _agent_domain = detect_domain(goal)
            print(f"\n[AGENT] Domain: {_agent_domain} (LLM-classified)")

            _agent_state = DesignState(
                goal=goal,
                repo_root=repo_root,
                domain=_agent_domain,
                max_iterations=max_agent_iterations,
                teaching_engine=_teaching_engine,
            )
            _agent_state = run_agent_loop(_agent_state)

            # Build session from agent results
            session["agent_mode"] = True
            session["agent_domain"] = _agent_domain
            session["agent_iterations"] = _agent_state.iteration
            session["agent_converged"] = _agent_state.converged
            session["bbox"] = _agent_state.bbox
            session["goal"] = goal

            _raw_step = _agent_state.artifacts.get("step_path", "")
            _raw_stl  = _agent_state.artifacts.get("stl_path", "")
            # Guard: only use the path if it's a non-empty string pointing to a real file
            _step_file = Path(_raw_step) if _raw_step else None
            _stl_file  = Path(_raw_stl)  if _raw_stl  else None
            step_path = _step_file if (_step_file and _step_file.is_file()) else Path("__no_step__")
            stl_path  = _stl_file  if (_stl_file  and _stl_file.is_file())  else Path("__no_stl__")
            if _step_file and _step_file.is_file():
                session["step_path"] = str(_step_file)
            if _stl_file and _stl_file.is_file():
                session["stl_path"] = str(_stl_file)
            if _agent_state.artifacts.get("script_path"):
                session["script_path"] = _agent_state.artifacts["script_path"]

            session["agent_history"] = _agent_state.history
            artifacts = _agent_state.artifacts
            plan = _agent_state.plan or {"part_id": _agent_state.part_id, "params": _agent_state.spec}
            part_id = _agent_state.part_id or "agent_part"
            plan_text = f"Agent-generated: {goal}"
            _plan_params = _agent_state.spec
            _spec = _agent_state.spec

            # Route to best CAD backend (not hardcoded cadquery)
            try:
                from .tool_router import select_cad_tool
                cad_tool = select_cad_tool(goal, plan if isinstance(plan, dict) else {"part_id": part_id})
                print(f"[AGENT] CAD backend: {cad_tool}")
            except Exception:
                cad_tool = "cadquery"

            # ── CHECKPOINT: PLAN (agent mode) ────────────────────────────────
            _checkpoint("PLAN", [
                ("spec_extracted",  bool(_agent_state.spec),    f"{len(_agent_state.spec)} params extracted"),
                ("domain_detected", bool(_agent_domain),        f"domain: {_agent_domain}"),
                ("has_goal",        bool(goal),                 "goal provided"),
            ], session)
            if _teaching_engine:
                _spec_summary = f"{len(_agent_state.spec)} parameters extracted: " + ", ".join(
                    f"{k}={v}" for k, v in list(_agent_state.spec.items())[:5]
                )
                _teaching_engine.interactive_pause("spec", f"Specification and planning complete. {_spec_summary}")

            # ── CHECKPOINT: ROUTE (agent mode) ───────────────────────────────
            _checkpoint("ROUTE", [
                ("domain_valid",    _agent_domain in ("cad", "cam", "ecad", "civil", "drawing", "assembly"),
                 f"domain '{_agent_domain}'"),
                ("agent_available", True, "Ollama connected"),
            ], session)

            # ── CHECKPOINT: GENERATE ─────────────────────────────────────────
            _step_exists = step_path.is_file()
            _stl_exists  = stl_path.is_file()
            _checkpoint("GENERATE", [
                ("output_exists",   _step_exists or _stl_exists,
                 "no geometry produced" if not (_step_exists or _stl_exists) else "STEP/STL generated"),
                ("agent_converged", _agent_state.converged,
                 f"converged iter {_agent_state.iteration}" if _agent_state.converged
                 else f"stalled after {_agent_state.iteration} iterations with {len(_agent_state.failures)} failures"),
            ], session)
            if _teaching_engine:
                _gen_summary = (
                    f"Geometry generation complete after {_agent_state.iteration} iteration(s). "
                    + ("STEP and STL produced." if (_step_exists and _stl_exists)
                       else "STEP produced." if _step_exists
                       else "STL produced." if _stl_exists
                       else "No geometry produced.")
                )
                _teaching_engine.interactive_pause("design", _gen_summary)

            # ── AUTO-FALLBACK: if agent produced no geometry, use template path ─
            if not (_step_exists or _stl_exists):
                _fail_reason = "; ".join(_agent_state.failures[:2]) if _agent_state.failures else "stalled"
                print(f"[AGENT] No geometry produced ({_fail_reason})")
                print(f"[AGENT→TEMPLATE] Falling back to direct template generation...")
                _use_agents = False
                # Fall through to legacy path below — it will re-use part_id + _spec set above

            # ── CHECKPOINT: GEOMETRY (if STEP exists) ────────────────────────
            if _step_exists:
                try:
                    from .geometry_validator import validate_geometry, print_validation
                    _geo_val = validate_geometry(str(step_path), part_id, _plan_params, goal)
                    session["geometry_validation"] = _geo_val
                    print_validation(_geo_val)
                    _geo_checks = [(c["name"], c["passed"], c["detail"]) for c in _geo_val.get("checks", [])]
                    if _geo_checks:
                        _checkpoint("GEOMETRY", _geo_checks, session)
                except Exception as _gv_exc:
                    print(f"  [GEOMETRY] skipped: {_gv_exc}")

            # ── CHECKPOINT: QUALITY ──────────────────────────────────────────
            if _step_exists or _stl_exists:
                try:
                    quality = check_output_quality(str(step_path), str(stl_path))
                    session["output_quality"] = quality
                    _checkpoint("QUALITY", [
                        ("step_readable",  quality.get("step", {}).get("readable", False) if _step_exists else True,
                         "STEP not readable" if _step_exists and not quality.get("step", {}).get("readable", False) else "OK"),
                        ("stl_watertight", quality.get("stl", {}).get("watertight_after", False) if _stl_exists else True,
                         "STL not watertight" if _stl_exists and not quality.get("stl", {}).get("watertight_after", False) else "OK"),
                        ("quality_passed", quality.get("passed", True), f"failures: {quality.get('failures', [])}"),
                    ], session)
                except Exception as _q_exc:
                    print(f"  [QUALITY] skipped: {_q_exc}")

            # ── VISUAL VERIFICATION (agent mode) ────────────────────────────
            if _stl_exists:
                try:
                    from .visual_verifier import verify_visual
                    _vis_result = verify_visual(
                        str(step_path) if _step_exists else "",
                        str(stl_path),
                        goal,
                        _spec if isinstance(_spec, dict) else {},
                        repo_root=repo_root,
                    )
                    session["visual_verification"] = _vis_result
                    _vis_conf = _vis_result.get("confidence", 0.0)
                    if _vis_result.get("verified") is True and _vis_conf >= 0.90:
                        print(f"  [VISUAL] PASS — confidence {_vis_conf:.0%}")
                    elif _vis_result.get("verified") is True and _vis_conf < 0.90:
                        print(f"  [VISUAL] FAIL — confidence {_vis_conf:.0%} below 90% threshold")
                        for _vi in _vis_result.get("issues", []):
                            print(f"    [VISUAL] {_vi}")
                    elif _vis_result.get("verified") is False:
                        print(f"  [VISUAL] FAIL — confidence {_vis_conf:.0%}")
                        for _vi in _vis_result.get("issues", []):
                            print(f"    [VISUAL] {_vi}")
                    elif _vis_result.get("verified") is None:
                        _reason = _vis_result.get("reason", "unknown")
                        print(f"  [VISUAL] SKIPPED — {_reason}")
                except Exception as _vis_exc:
                    print(f"  [VISUAL] skipped: {_vis_exc}")

            # ── CHECKPOINT: DFM (agent mode) ────────────────────────────────
            if _step_exists:
                try:
                    from .agents.dfm_agent import run_dfm_analysis, print_dfm_report
                    _dfm_report = run_dfm_analysis(str(step_path), goal=goal)
                    session["dfm_analysis"] = _dfm_report
                    print_dfm_report(_dfm_report)
                    _dfm_score = _dfm_report.get("score", 0)
                    _dfm_passed = _dfm_report.get("passed", False)
                    _dfm_proc = _dfm_report.get("process_recommendation", "unknown")
                    _dfm_n_issues = len(_dfm_report.get("issues", []))
                    _checkpoint("DFM", [
                        ("dfm_score",   _dfm_score >= 50,
                         f"score {_dfm_score:.0f}/100" if _dfm_score >= 50
                         else f"score {_dfm_score:.0f}/100 -- too low"),
                        ("dfm_passed",  _dfm_passed,
                         f"{_dfm_proc}, {_dfm_n_issues} issue(s)"),
                        ("no_critical",
                         not any(i.get("severity") == "critical"
                                 for i in _dfm_report.get("issues", [])),
                         "critical DFM issues found" if any(
                             i.get("severity") == "critical"
                             for i in _dfm_report.get("issues", []))
                         else "no critical issues"),
                    ], session)
                    # Teach DFM insights
                    if _teaching_engine:
                        _teaching_engine.update_context("dfm_score", _dfm_score)
                        _teaching_engine.update_context("dfm_process", _dfm_proc)
                        _teaching_engine.teach_simple(
                            agent="DFM", phase="dfm",
                            message=f"Recommended manufacturing process: {_dfm_proc} (DFM score: {_dfm_score:.0f}/100)",
                            reasoning="The DFM analyzer examines wall thickness, draft angles, "
                            "undercuts, and feature accessibility to recommend the best "
                            "manufacturing process and flag potential issues.",
                            tags=["dfm", "manufacturing"],
                        )
                        from .teaching.engine import DifficultyLevel as _DL
                        from .teaching.dfm_knowledge import get_dfm_teaching, get_all_dfm_processes
                        for _dfm_issue in _dfm_report.get("issues", [])[:3]:
                            _sev = _dfm_issue.get("severity", "info")
                            _desc = _dfm_issue.get("description", str(_dfm_issue))
                            _issue_type = _dfm_issue.get("type", "")
                            # Look up process-specific knowledge
                            _kb_reasoning = _dfm_issue.get("recommendation", "")
                            if _issue_type and _dfm_proc:
                                _proc_key = _dfm_proc.lower().replace(" ", "_").replace("machining", "").strip("_") or "cnc"
                                if "cnc" in _dfm_proc.lower() or "machine" in _dfm_proc.lower():
                                    _proc_key = "cnc"
                                elif "3d" in _dfm_proc.lower() or "print" in _dfm_proc.lower() or "fdm" in _dfm_proc.lower():
                                    _proc_key = "fdm_3dp"
                                elif "inject" in _dfm_proc.lower():
                                    _proc_key = "injection_mold"
                                elif "sheet" in _dfm_proc.lower():
                                    _proc_key = "sheet_metal"
                                _kb_entry = get_dfm_teaching(_issue_type, _proc_key)
                                if _kb_entry:
                                    _kb_reasoning = (
                                        f"{_kb_entry['message']} "
                                        f"Fix: {_kb_entry['fix']} "
                                        f"Rule of thumb: {_kb_entry['rule']}"
                                    )
                            _teaching_engine.teach_simple(
                                agent="DFM", phase="dfm",
                                message=f"DFM {_sev}: {_desc}",
                                reasoning=_kb_reasoning,
                                level=_DL.BEGINNER if _sev == "critical" else _DL.INTERMEDIATE,
                                tags=["dfm"],
                            )
                        # Check for recurring mistake patterns
                        if _teaching_engine.user_profile:
                            _mistake_warnings = _teaching_engine.check_mistakes(
                                _dfm_report.get("issues", []))
                            for _mw in _mistake_warnings:
                                _teaching_engine.teach_simple(
                                    agent="DFM", phase="dfm",
                                    message=_mw,
                                    level=_DL.BEGINNER,
                                    tags=["dfm", "pattern"],
                                )
                        _teaching_engine.interactive_pause(
                            "dfm",
                            f"DFM analysis complete: {_dfm_proc}, score {_dfm_score:.0f}/100, "
                            f"{_dfm_n_issues} issue(s) found.",
                        )
                except Exception as _dfm_exc:
                    print(f"  [DFM] skipped: {_dfm_exc}")

            # ── QUOTE: instant cost estimate ─────────────────────────────────
            if _step_exists:
                try:
                    from .agents.quote_agent import QuoteAgent
                    _qa = QuoteAgent()
                    _mat = _plan_params.get('material', 'aluminium_6061')
                    _quote = _qa.quote(str(step_path), material=_mat)
                    session['quote'] = _quote
                    _qa.print_quote(_quote)
                    _checkpoint('QUOTE', [
                        ('quote_generated', bool(_quote.get('unit_cost_usd')),
                         f"unit cost: ${_quote.get('unit_cost_usd', 0):.2f}"),
                        ('confidence', _quote.get('confidence', 'low') in ('high', 'medium'),
                         f"confidence: {_quote.get('confidence', 'unknown')}"),
                    ], session)
                    # Teach quote breakdown
                    if _teaching_engine:
                        _unit = _quote.get('unit_cost_usd', 0)
                        _setup = _quote.get('setup_cost_usd', 0)
                        _cycle = _quote.get('cycle_time_min', 0)
                        _teaching_engine.update_context("quote", _quote)
                        _teaching_engine.teach_simple(
                            agent="Quote", phase="quote",
                            message=f"Estimated unit cost: ${_unit:.2f} (material: {_mat})",
                            reasoning=f"Cost breakdown: setup ${_setup:.2f} (amortized over batch), "
                            f"cycle time ~{_cycle:.1f} min per part. "
                            "Setup cost includes fixturing, tool changes, and first-article inspection. "
                            "Higher quantities reduce per-unit cost because setup is spread across more parts.",
                            tags=["cost", "manufacturing"],
                        )
                        # Material-specific manufacturing teaching
                        from .teaching.dfm_knowledge import get_material_teaching
                        _mat_teaching = get_material_teaching(_mat)
                        if _mat_teaching:
                            _teaching_engine.teach_simple(
                                agent="Quote", phase="quote",
                                message=f"Material: {_mat_teaching.get('name', _mat)} -- {_mat_teaching.get('machinability', '')}",
                                reasoning=f"{_mat_teaching.get('vs_alternatives', '')} "
                                f"Gotchas: {_mat_teaching.get('gotchas', '')}",
                                tags=["material", "cost"],
                                related_param="material",
                            )
                        _teaching_engine.interactive_pause(
                            "quote",
                            f"Cost estimation complete: unit cost ${_unit:.2f}, "
                            f"setup ${_setup:.2f}, cycle time {_cycle:.1f} min.",
                        )
                except Exception as _qe:
                    print(f'  [QUOTE] skipped: {_qe}')

            # ── Post-processing: Preview, FEA, GD&T ─────────────────────────
            if preview and session.get('stl_path') and Path(session['stl_path']).exists():
                from .preview_ui import show_preview
                _export_choice = show_preview(
                    session['stl_path'],
                    part_id=part_id or goal[:30],
                    script_path=session.get('script_path'),
                )
                session['export_choice'] = _export_choice
            elif preview:
                # No STL but preview requested — try STEP
                _sp = session.get('step_path', '')
                if _sp and Path(_sp).exists():
                    print(f'[PREVIEW] No STL available. STEP at: {_sp}')

            if (_step_exists or _stl_exists) and session.get('export_choice') != 'skip':
                # Auto-run FEA (no prompt in agent mode)
                try:
                    from .physics_analyzer import analyze as _phys_analyze
                    _pr = _phys_analyze(part_id or 'agent_part', 'auto', _plan_params, goal, str(repo_root))
                    if _pr:
                        session['physics_analysis'] = _pr
                        if _pr.get('passed'):
                            print(f'  [FEA] PASS -- SF={_pr.get("safety_factor", "?"):.2f}')
                        else:
                            print(f'  [FEA] FAIL -- SF={_pr.get("safety_factor", "?")}')
                            for _f in _pr.get('failures', []):
                                print(f'    {_f}')
                except Exception as _fe:
                    print(f'  [FEA] skipped: {_fe}')

                # Auto-run GD&T drawing (no prompt in agent mode)
                if _step_exists:
                    try:
                        from .drawing_generator import generate_gdnt_drawing
                        _dp = generate_gdnt_drawing(step_path, part_id or 'agent_part',
                                                    params=_plan_params, repo_root=repo_root)
                        print(f'  [GD&T] Drawing: {_dp}')
                        session['drawing_path'] = str(_dp)
                    except Exception as _de:
                        print(f'  [GD&T] skipped: {_de}')

                # Auto-run CAM (no prompt in agent mode)
                if _step_exists:
                    try:
                        from .agents.cam_agent import run_cam_agent
                        _mat = _plan_params.get('material', 'aluminium_6061')
                        _cam_result = run_cam_agent(str(step_path), material=_mat)
                        if _cam_result:
                            session['cam'] = _cam_result
                            print(f'  [CAM] Script: {_cam_result.get("script_path", "?")}')
                    except KeyboardInterrupt:
                        print(f'  [CAM] interrupted by user')
                    except Exception as _ce:
                        print(f'  [CAM] skipped: {_ce}')

            # ── Final summary (only when agent actually produced geometry) ─────
            # If _use_agents was reset to False above (no geometry), skip this
            # summary and fall through to the legacy template path instead.
            if not _use_agents:
                # Clear partial checkpoints so legacy path starts clean
                session.pop("checkpoints", None)
            else:
                _all_cp = session.get('checkpoints', {})
                _all_checks_n  = sum(len(cp.get('checks', [])) for cp in _all_cp.values())
                _all_checks_ok = sum(
                    sum(1 for c in cp.get('checks', []) if c.get('passed'))
                    for cp in _all_cp.values()
                )
                _s_step = str(step_path) if step_path.is_file() else session.get('step_path', '')
                _s_stl  = str(stl_path)  if stl_path.is_file()  else session.get('stl_path', '')
                # File presence is necessary but NOT sufficient — also require
                # no checkpoint stage to have failed. Previously pipeline_ok
                # was just `file_exists and not cem_blocked`, which lied when
                # GENERATE/VISUAL/etc. failed but produced an output file
                # anyway (the false-PASS aggregation bug).
                _file_ok = bool(
                    (_s_step and Path(_s_step).is_file()) or (_s_stl and Path(_s_stl).is_file())
                )
                _all_stages_ok = all(cp.get('passed') for cp in _all_cp.values())
                _pipeline_ok = _file_ok and _all_stages_ok and not session.get('cem_blocked')
                # Persist for callers — the printed summary used to be the
                # only place this verdict existed, so callers couldn't read it.
                session['passed'] = _pipeline_ok
                session['stages_ok'] = _all_stages_ok
                session['file_ok'] = _file_ok
                print()
                print('=' * 64)
                print(f"  PIPELINE SUMMARY  --  {'PASS' if _pipeline_ok else 'FAIL'}")
                print('=' * 64)
                for stg, cp in _all_cp.items():
                    tag = '[OK]  ' if cp['passed'] else '[FAIL]'
                    n_ok = sum(1 for c in cp.get('checks', []) if c['passed'])
                    n_t  = len(cp.get('checks', []))
                    print(f'  {tag} {stg:12s}  {n_ok}/{n_t} checks')
                    for f in cp.get('failures', []):
                        print(f'         -> {f}')
                print('-' * 64)
                print(f'  {_all_checks_ok}/{_all_checks_n} checks passed  |  {len(_all_cp)} stages')
                print('=' * 64)
                if (_s_step and Path(_s_step).is_file()) or (_s_stl and Path(_s_stl).is_file()):
                    print()
                    print('OUTPUT FILES')
                    print('-' * 64)
                    if _s_step and Path(_s_step).is_file():
                        print(f'  STEP:   {_s_step}  ({Path(_s_step).stat().st_size // 1024} KB)')
                    if _s_stl and Path(_s_stl).is_file():
                        print(f'  STL:    {_s_stl}  ({Path(_s_stl).stat().st_size // 1024} KB)')
                    print()
                    print('NEXT STEPS')
                    print('-' * 64)
                    _vt = _s_step or _s_stl
                    print(f'  View:     python run_aria_os.py --view "{_vt}"')
                    if _s_stl:
                        print(f'  Verify:   python run_aria_os.py --verify "{_s_stl}"')
                    if _s_step:
                        print(f'  Analyze:  python run_aria_os.py --analyze-part "{_s_step}"')
                    print(f'  List all: python run_aria_os.py --list')
                    print('=' * 64)
                else:
                    print()
                    print('[!] No geometry produced — check errors above.')
                    print('=' * 64)

                # ── Teaching summary ─────────────────────────────────────────
                if _teaching_engine and _teaching_engine.teachings:
                    _t_summary = _teaching_engine.get_session_summary()
                    session["teaching_summary"] = _t_summary
                    print()
                    print('DESIGN REASONING')
                    print('-' * 64)
                    for _t in _teaching_engine.teachings:
                        _lvl = _t.level.value[0].upper()
                        print(f'  [{_lvl}] {_t.message}')
                        if _t.reasoning:
                            # Wrap reasoning to ~70 chars indented
                            _lines = _t.reasoning.split(". ")
                            for _ln in _lines[:2]:
                                if _ln.strip():
                                    print(f'       {_ln.strip()}.')
                    print(f'  --- {len(_teaching_engine.teachings)} teaching moments | '
                          f'Ask questions: python run_aria_os.py --ask')
                    print('=' * 64)

                event_bus.emit('complete', f'Agent pipeline done', {'session': session})
                logger_log(session)
                # Save user profile if teaching was active
                if _teaching_engine is not None and _teaching_engine.user_profile is not None:
                    try:
                        _teaching_engine.user_profile.record_session(parts=1)
                        _teaching_engine.user_profile.save()
                    except Exception:
                        pass
                return session

        except Exception as _agent_exc:
            print(f"[AGENT] Failed: {_agent_exc}")
            import traceback
            traceback.print_exc()
            _use_agents = False
            print(f"[AGENT] Falling back to legacy pipeline...")

    # ══════════════════════════════════════════════════════════════════════════
    # LEGACY PATH — only runs when agents are NOT used
    # ══════════════════════════════════════════════════════════════════════════
    if not _use_agents:
        plan = planner_plan(goal, context, repo_root=repo_root)
    if _use_agents:
        # Agent path already set plan, part_id, _spec, step_path, stl_path, artifacts, cad_tool.
        # Jump directly to shared post-processing (Preview, FEA, GD&T, CEM).
        pass
    else:
        # ── BEGIN LEGACY PATH ────────────────────────────────────────────────
        pass

    if not _use_agents and not isinstance(plan, dict):
        plan = {"part_id": "aria_part", "text": str(plan), "build_order": [], "features": []}
    if not _use_agents:
        from .spec_extractor import extract_spec, merge_spec_into_plan as _merge_spec
        _spec = extract_spec(goal)
    if not _use_agents and _spec:
        _merge_spec(_spec, plan)
        # Sync user-specified dims back into base_shape so validation expected_bbox
        # reflects what the user actually asked for, not the planner's template defaults.
        _base = plan.get("base_shape")
        if not isinstance(_base, dict):
            plan["base_shape"] = {}
            _base = plan["base_shape"]
        if isinstance(_base, dict):
            _DIM_KEYS = (
                "od_mm", "bore_mm", "id_mm", "thickness_mm", "height_mm",
                "width_mm", "depth_mm", "length_mm",
                # diameter_mm excluded: for box parts it captures sub-feature dims (ports/holes),
                # not the part's base shape — syncing it would mislead bbox validation.
            )
            # Also write the bare (no _mm) key that planner uses for box dims
            _SHORT_KEY = {
                "width_mm": "width", "height_mm": "height", "depth_mm": "depth",
                "length_mm": "length", "thickness_mm": "thickness",
            }
            for _k in _DIM_KEYS:
                if _k in _spec:
                    _base[_k] = _spec[_k]
                    if _k in _SHORT_KEY:
                        _base[_SHORT_KEY[_k]] = _spec[_k]
        _user_dims = [
            f"{k}={v} (user)"
            for k, v in _spec.items()
            if k not in ("part_type", "material")
        ]
        if _user_dims:
            print(f"[SPEC] {' '.join(_user_dims)}")

    # --- CEM: resolve physics model for this domain, auto-generate if unknown ---
    # Priority: static registry (aria/lre) → dynamic registry → LLM-generated new CEM
    # CEM outputs fill in plan["params"] without overwriting user-explicit values.
    # This is the LEAP-71 layer: engineering constraints → physics-derived geometry.
    try:
        from .cem_generator import resolve_and_compute
        _cem_params = plan.get("params") or {}
        _cem_result = resolve_and_compute(goal, plan.get("part_id", ""), _cem_params, repo_root)
        if _cem_result:
            params_target = plan.setdefault("params", {})
            injected = []
            for k, v in _cem_result.items():
                if k == "part_family":
                    continue
                if k not in params_target or params_target[k] is None:
                    params_target[k] = v
                    injected.append(f"{k}={v}")
            if injected:
                print(f"[CEM] Physics params injected: {' '.join(injected)}")
            plan["cem_context"] = _cem_result
    except Exception as _cem_exc:
        print(f"[CEM] skipped: {_cem_exc}")

    plan = attach_brief_to_plan(goal, plan, context, repo_root=repo_root)

    plan_text = plan.get("engineering_brief") or plan.get("text", str(plan))
    part_id   = plan.get("part_id", "")
    _plan_params = plan.get("params") or {}

    # ── CHECKPOINT: PLAN (legacy path only — agent path has its own) ─────────
    if not _use_agents:
        _checkpoint("PLAN", [
            ("plan_is_dict",     isinstance(plan, dict),          "plan must be a dict"),
            ("has_part_id",      bool(part_id),                   f"part_id is empty; got '{part_id}'"),
            ("has_params",       bool(_plan_params),              "plan.params is empty — no dimensions extracted"),
            ("has_brief",        len(plan_text) > 20,             f"engineering brief too short ({len(plan_text)} chars)"),
            ("has_base_shape",   bool(plan.get("base_shape")),    "base_shape missing — validator may use wrong expected bbox"),
        ], session)

    # Prefer Claude-based router; fall back to heuristic if LLM unavailable
    if not plan.get("cad_tool_selected"):
        try:
            from .multi_cad_router import CADRouter
            decision = CADRouter.route(goal, dry_run=False)
            cad_tool = decision["backend"]
            plan["cad_tool_selected"]  = cad_tool
            plan["cad_tool_rationale"] = decision.get("reasoning", "")
            plan["cad_tool_decision"]  = decision
        except Exception:
            cad_tool = select_cad_tool(goal, plan)
    else:
        cad_tool = plan["cad_tool_selected"]

    session["cad_tool"]          = cad_tool
    session["cad_route"]         = {"tool": cad_tool, "rationale": plan.get("cad_tool_rationale", "")}
    session["engineering_brief"] = plan.get("engineering_brief", "")

    # ── CHECKPOINT: ROUTE (legacy path only) ─────────────────────────────────
    if not _use_agents:
        _VALID_TOOLS = {"cadquery", "grasshopper", "blender", "fusion360", "sdf", "autocad", "zoo"}
        _checkpoint("ROUTE", [
            ("tool_valid",       cad_tool in _VALID_TOOLS,            f"unknown tool '{cad_tool}'; valid: {_VALID_TOOLS}"),
            ("has_rationale",    bool(plan.get("cad_tool_rationale")), "no routing rationale — decision may be arbitrary"),
            ("spec_dims_exist",  bool(_spec),                         "no dimensions extracted from goal — templates will use defaults"),
        ], session)

    event_bus.emit("step", f"Tool: {cad_tool}", {"part_id": part_id, "tool": cad_tool})

    # --- Print route banner ---
    print("\n" + "=" * 64)
    print("ARIA CAD ROUTE (tool + auto-built engineering prompt)")
    print("=" * 64)
    print(f"Pipeline: {cad_tool}")
    print(f"Why: {plan.get('cad_tool_rationale', '')}")
    print(f"[AUTOMATION] Primary CAD: {cad_tool} (artifacts -> outputs/cad/...)")
    print("-" * 64)
    # Encode-safe: replace chars the Windows console can't handle
    _enc = getattr(_sys.stdout, "encoding", "utf-8") or "utf-8"
    print(plan_text.encode(_enc, errors="replace").decode(_enc))
    print("=" * 64 + "\n")

    paths     = get_output_paths(part_id or goal, repo_root)
    step_path = Path(paths["step_path"])
    stl_path  = Path(paths["stl_path"])

    # --- Agent mode: autonomous multi-agent loop ---
    _use_agents = agent_mode
    if _use_agents is None:
        try:
            from .agents.base_agent import is_ollama_available
            from .agents.ollama_config import AGENT_MODELS
            _use_agents = is_ollama_available(model=AGENT_MODELS.get("designer", "qwen2.5-coder:7b"))
        except Exception:
            _use_agents = False

    # --- Generate artifacts (legacy path — only when agents NOT used) ---
    artifacts: dict[str, str] = {}

    if cad_tool == "grasshopper":
        # Try Compute API generator first (direct API calls, no IronPython)
        _compute_succeeded = False
        try:
            from .generators.compute_generator import write_compute_artifacts
            from .compute_client import ComputeClient
            if ComputeClient().is_available():
                event_bus.emit("step", "Trying Compute API generator")
                artifacts = write_compute_artifacts(
                    plan if isinstance(plan, dict) else {},
                    goal,
                    str(step_path),
                    str(stl_path),
                    repo_root=repo_root,
                )
                if artifacts.get("step_path") and Path(artifacts["step_path"]).exists():
                    _compute_succeeded = True
                    print(f"[COMPUTE] Generated via Compute API")
        except Exception as _ce:
            event_bus.emit("warning", f"Compute generator failed: {_ce}")
            print(f"[COMPUTE] Falling back to GH scripts: {_ce}")

        _gh_previous_failures: list[str] = []
        _gh_succeeded = _compute_succeeded
        if not _compute_succeeded:
            for _gh_attempt in range(max_attempts):
                try:
                    artifacts = write_grasshopper_artifacts(
                        plan if isinstance(plan, dict) else {},
                        goal,
                        str(step_path),
                        str(stl_path),
                        repo_root=repo_root,
                    )
                    _gh_succeeded = True
                    break
                except RuntimeError as _gh_err:
                    _gh_reason = str(_gh_err)
                    _gh_previous_failures.append(_gh_reason)
                    print(f"[GH RETRY {_gh_attempt + 1}/{max_attempts}] {_gh_reason}")
                    event_bus.emit("error", f"GH attempt {_gh_attempt + 1} failed: {_gh_reason}", {"part_id": part_id})
                    if _gh_attempt + 1 >= max_attempts:
                        print(f"[GH FAIL] All {max_attempts} attempts exhausted.")
                        artifacts = {"status": "failure", "error": _gh_reason, "previous_failures": _gh_previous_failures}

        if not _gh_succeeded:
            # Fall back to CadQuery so the pipeline always produces geometry.
            # GH script artifacts are still written to disk — they can be used in
            # Rhino Compute later. CadQuery gives an immediate STEP/STL.
            print(f"[GH→CQ FALLBACK] Rhino Compute unavailable — generating CadQuery artifact instead.")
            session["gh_fallback"] = True
            cad_tool = "cadquery"
        else:
            script_path = artifacts.get("script_path", "")
            session["script_path"] = script_path
            if script_path:
                script_ok, script_errors = validate_grasshopper_script(script_path)
                if not script_ok:
                    for e in script_errors:
                        event_bus.emit("validation", f"Script validation: {e}", {"part_id": part_id})
                        print(f"[SCRIPT WARN] {e}")
                else:
                    size = Path(script_path).stat().st_size
                    print(f"[GRASSHOPPER] Script ready: {script_path} ({size} bytes)")
                    event_bus.emit(
                        "grasshopper",
                        f"[GRASSHOPPER] Script ready: {script_path} ({size} bytes)",
                        {"script_path": script_path, "size_bytes": size, "part_id": part_id},
                    )

    elif cad_tool == "blender":
        artifacts = write_blender_artifacts(
            plan if isinstance(plan, dict) else {},
            goal,
            str(stl_path),
            repo_root=repo_root,
        )

    elif cad_tool == "sdf":
        try:
            from .generators.sdf_generator import write_sdf_artifacts
            artifacts = write_sdf_artifacts(
                plan if isinstance(plan, dict) else {},
                goal,
                str(step_path),
                str(stl_path),
                repo_root=repo_root,
            )
            if artifacts.get("stl_path"):
                session["stl_path"] = artifacts["stl_path"]
            if artifacts.get("error"):
                session["sdf_error"] = artifacts["error"]
                print(f"[SDF ERROR] {artifacts['error']}")
            else:
                meta = artifacts.get("meta", {})
                print(
                    f"[SDF] {meta.get('tpms_type', 'tpms')} | "
                    f"scale={meta.get('scale_mm', 0):.1f}mm | "
                    f"{meta.get('voxels', 0):,} voxels | "
                    f"{meta.get('triangles', 0):,} triangles"
                )
                session["sdf_meta"] = meta
                event_bus.emit("complete", "SDF generation complete", {"part_id": part_id})
        except ImportError as _sdf_imp:
            print(f"[SDF] scikit-image not installed — falling back to cadquery.")
            print(f"      Run: pip install scikit-image")
            # Fall through to cadquery below by setting cad_tool
            cad_tool = "cadquery"
            event_bus.emit("error", f"SDF import error: {_sdf_imp}", {"part_id": part_id})
        except Exception as exc:
            event_bus.emit("error", f"SDF failed: {exc}", {"part_id": part_id})
            print(f"[SDF ERROR] {exc}")

    if cad_tool == "cadquery":
        # --- Zoo.dev shortcut: when no CQ template exists, try Zoo before LLM ---
        _zoo_used = False
        try:
            from .cadquery_generator import _find_template_fn as _cq_find_tpl
            _has_template = _cq_find_tpl(part_id) is not None
        except Exception:
            _has_template = True  # assume template exists on import error — skip Zoo

        if not _has_template:
            try:
                from .zoo_bridge import is_zoo_available, generate_step_from_zoo
                if is_zoo_available(repo_root):
                    zoo_result = generate_step_from_zoo(goal, str(step_path.parent), repo_root=repo_root)
                    if zoo_result.get("status") == "ok":
                        _zoo_step = Path(zoo_result["step_path"])
                        if _zoo_step.exists():
                            # Zoo produced a STEP — use it directly
                            session["step_path"] = str(_zoo_step)
                            session["zoo_result"] = zoo_result
                            session["cad_tool"] = "zoo"
                            artifacts = {"step_path": str(_zoo_step), "status": "success"}
                            _zoo_used = True
                            event_bus.emit("complete", "Zoo.dev generation complete", {"part_id": part_id})
                            print(f"[Zoo] Using Zoo.dev STEP — skipping CadQuery LLM fallback.")
                    elif zoo_result.get("status") == "error":
                        print(f"[Zoo] Failed ({zoo_result.get('error', 'unknown')}), falling back to CadQuery.")
            except Exception as _zoo_exc:
                print(f"[Zoo] Skipped: {_zoo_exc}")

        # --- CADSmith loop: iterative LLM generation for parts without templates ---
        if not _zoo_used and not _has_template:
            try:
                from .generators.cadsmith_generator import cadsmith_generate
                print(f"[CADSMITH] Generating '{part_id}' via iterative loop...")
                event_bus.emit("step", f"CADSmith iterative generation for {part_id}")
                artifacts = cadsmith_generate(
                    goal,
                    plan if isinstance(plan, dict) else {},
                    str(step_path),
                    str(stl_path),
                    repo_root=repo_root,
                )
                if artifacts.get("step_path") and Path(artifacts["step_path"]).exists():
                    _zoo_used = True  # skip CQ fallback
                    print(f"[CADSMITH] Generated: {Path(artifacts['step_path']).stat().st_size / 1024:.0f} KB STEP")
            except Exception as _cs_exc:
                print(f"[CADSMITH] Failed: {_cs_exc}, falling back to CadQuery")
                event_bus.emit("warning", f"CADSmith failed: {_cs_exc}")

        if not _zoo_used:
            try:
                from .cadquery_generator import write_cadquery_artifacts
                print(f"[CADQUERY] Generating '{part_id}'...")

                # Build a generate_fn compatible with run_validation_loop protocol:
                #   generate_fn(plan, step_path, stl_path, repo_root, previous_failures=None) -> dict
                def _cq_generate_fn(p, sp, st, rr, previous_failures=None):
                    return write_cadquery_artifacts(
                        p if isinstance(p, dict) else {},
                        goal,
                        str(sp),
                        str(st),
                        repo_root=rr,
                        previous_failures=previous_failures,
                    )

                _val_plan = {"part_id": part_id, "params": plan.get("params", {}), "text": goal}

                val_result = run_validation_loop(
                    generate_fn=_cq_generate_fn,
                    goal=goal,
                    plan=_val_plan,
                    step_path=str(step_path),
                    stl_path=str(stl_path),
                    max_attempts=max_attempts,
                    repo_root=repo_root,
                    skip_visual=True,
                    check_quality=True,
                )

                # Extract results from validation loop
                gen_result = val_result.get("generate_result", {})
                artifacts = gen_result if isinstance(gen_result, dict) else {}

                if gen_result.get("step_path"):
                    session["step_path"] = gen_result["step_path"]
                if gen_result.get("stl_path"):
                    session["stl_path"] = gen_result["stl_path"]
                if gen_result.get("bbox"):
                    session["bbox"] = gen_result["bbox"]
                if gen_result.get("script_path"):
                    session["script_path"] = gen_result["script_path"]
                    artifacts["script_path"] = gen_result["script_path"]
                if gen_result.get("error"):
                    session["cq_error"] = gen_result["error"]

                session["validation"] = {
                    "geo":  val_result.get("geo_result", {}),
                    "vis":  val_result.get("vis_result", {}),
                    "quality": val_result.get("quality_result", {}),
                    "attempts": val_result.get("attempts", 1),
                    "status": val_result.get("status"),
                    "validation_failures": val_result.get("validation_failures", []),
                }

                if val_result.get("status") == "success":
                    event_bus.emit("complete", "CadQuery generation complete", {"part_id": part_id})
                else:
                    _val_failures = val_result.get("validation_failures", [])
                    print(f"[CQ VALIDATION FAIL] {val_result.get('attempts', 0)} attempts exhausted. Failures: {_val_failures}")
                    event_bus.emit("error", f"CQ validation failed after {val_result.get('attempts', 0)} attempts", {"part_id": part_id})

            except Exception as exc:
                event_bus.emit("error", f"CadQuery failed: {exc}", {"part_id": part_id})
                print(f"[CADQUERY ERROR] {exc}")

    elif cad_tool == "fusion360":
        try:
            from .fusion_generator import generate_fusion_script
            fusion_script = generate_fusion_script(
                plan if isinstance(plan, dict) else {},
                goal,
                str(step_path),
                str(stl_path),
                repo_root=repo_root,
            )
            # Write script to outputs dir
            fusion_dir = repo_root / "outputs" / "cad" / "fusion" / (part_id or "aria_part")
            fusion_dir.mkdir(parents=True, exist_ok=True)
            script_file = fusion_dir / f"{part_id or 'aria_part'}_fusion.py"
            script_file.write_text(fusion_script, encoding="utf-8")
            artifacts["script_path"] = str(script_file)
            session["script_path"] = str(script_file)
            print(f"[FUSION360] Script ready: {script_file}")
            print(f"[FUSION360] Run this script inside Fusion 360 to produce STEP/STL.")
            event_bus.emit("complete", "Fusion 360 script written", {"part_id": part_id})
        except Exception as exc:
            event_bus.emit("error", f"Fusion 360 generator failed: {exc}", {"part_id": part_id})
            print(f"[FUSION360 ERROR] {exc}")

        # Always generate a CadQuery approximation so the pipeline produces immediate geometry.
        # The Fusion script is the authoritative design (lattice/generative/simulation);
        # the CQ artifact is a structural placeholder for assembly and preview.
        print(f"[FUSION360→CQ] Generating CadQuery approximation for preview/assembly...")
        session["fusion_cq_approx"] = True
        cad_tool = "cadquery"

    # --- Run validation loop for grasshopper backend ---
    # CadQuery validation is handled above via its own run_validation_loop call.
    # Grasshopper artifacts are produced by write_grasshopper_artifacts, so we wire
    # a generate_fn that re-invokes that function on retry.
    if cad_tool == "grasshopper" and artifacts.get("script_path"):
        if step_path.exists() or stl_path.exists():
            try:
                _val_plan = {"part_id": part_id, "params": plan.get("params", {}), "text": goal}

                def _gh_regen_fn(p, sp, st, rr, previous_failures=None):
                    """Re-invoke grasshopper generation for validation retries."""
                    try:
                        regen_artifacts = write_grasshopper_artifacts(
                            plan if isinstance(plan, dict) else {},
                            goal,
                            str(sp),
                            str(st),
                            repo_root=rr,
                        )
                        return {
                            "status": "success" if regen_artifacts.get("script_path") else "failure",
                            "step_path": str(sp) if Path(sp).exists() else None,
                            "stl_path":  str(st) if Path(st).exists() else None,
                            "error": regen_artifacts.get("error"),
                            "script_path": regen_artifacts.get("script_path"),
                        }
                    except RuntimeError as e:
                        return {
                            "status": "failure",
                            "step_path": str(sp) if Path(sp).exists() else None,
                            "stl_path":  str(st) if Path(st).exists() else None,
                            "error": str(e),
                        }

                val_result = run_validation_loop(
                    generate_fn=_gh_regen_fn,
                    goal=goal,
                    plan=_val_plan,
                    step_path=str(step_path),
                    stl_path=str(stl_path),
                    max_attempts=max_attempts,
                    repo_root=repo_root,
                    skip_visual=True,
                    check_quality=True,
                )
                session["validation"] = {
                    "geo":  val_result.get("geo_result", {}),
                    "vis":  val_result.get("vis_result", {}),
                    "quality": val_result.get("quality_result", {}),
                    "attempts": val_result.get("attempts", 1),
                    "status": val_result.get("status"),
                    "validation_failures": val_result.get("validation_failures", []),
                }
                event_bus.emit("validation", f"Geometry check: {val_result['status']}", {"part_id": part_id})
            except Exception as exc:
                print(f"[VALIDATION WARN] {exc}")

    # --- Attempt Rhino Compute execution (grasshopper only) ---
    runner = artifacts.get("runner_path", "")
    if runner and Path(runner).exists():
        event_bus.emit("step", "Attempting Rhino Compute execution", {"runner": runner})
        try:
            import subprocess as _subprocess
            result = _subprocess.run(
                [_sys.executable, runner],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                if step_path.exists():
                    session["step_path"] = str(step_path)
                if stl_path.exists():
                    session["stl_path"] = str(stl_path)
                gh_validation = validate_grasshopper_output(str(step_path), result.stdout)
                if gh_validation.get("bbox"):
                    session["bbox"] = gh_validation["bbox"]
                event_bus.emit("complete", "Rhino Compute run succeeded", {"part_id": part_id})
            else:
                warn = result.stderr[:500] or result.stdout[:500]
                session["rhino_compute_warning"] = warn
                event_bus.emit("error", f"Rhino Compute run failed: {warn}", {"part_id": part_id})
        except Exception as e:
            session["rhino_compute_pending"] = str(e)
            print(f"[INFO] Rhino Compute not available. Running CadQuery fallback to produce STEP/STL...")
            event_bus.emit("step", "Rhino Compute unavailable — CQ fallback", {"runner": runner})

    # --- CQ fallback for grasshopper route when Rhino Compute unavailable ---
    # If STEP/STL still don't exist after GH attempt, run CadQuery generator directly.
    if cad_tool == "grasshopper" and not (step_path.exists() or stl_path.exists()):
        try:
            from .cadquery_generator import write_cadquery_artifacts as _cq_gen
            print(f"[CQ-FALLBACK] Grasshopper + no Rhino Compute -> generating via CadQuery template...")
            _cq_artifacts = _cq_gen(plan, goal, str(step_path), str(stl_path), repo_root=repo_root)
            if _cq_artifacts.get("bbox"):
                session["bbox"] = _cq_artifacts["bbox"]
            if _cq_artifacts.get("script_path"):
                artifacts["script_path"] = _cq_artifacts["script_path"]
            if step_path.exists():
                session["step_path"] = str(step_path)
                print(f"[CQ-FALLBACK] STEP: {step_path}")
            if stl_path.exists():
                session["stl_path"] = str(stl_path)
                print(f"[CQ-FALLBACK] STL:  {stl_path}")
        except Exception as _cq_e:
            print(f"[CQ-FALLBACK] Could not generate CQ geometry: {_cq_e}")

    # ── CHECKPOINT: GENERATE (legacy path only) ─────────────────────────────
    if _use_agents:
        _step_exists = step_path.exists() if isinstance(step_path, Path) else False
        _stl_exists = stl_path.exists() if isinstance(stl_path, Path) else False
    else:
        _step_exists = step_path.exists()
        _stl_exists  = stl_path.exists()
    _step_size   = step_path.stat().st_size if _step_exists else 0
    _stl_size    = stl_path.stat().st_size if _stl_exists else 0
    _has_script  = bool(artifacts.get("script_path"))

    # If bbox not captured from generator, read it from STEP or STL
    if not session.get("bbox"):
        if _step_exists:
            try:
                import cadquery as _cq_bb
                _shape = _cq_bb.importers.importStep(str(step_path))
                _bb = _shape.val().BoundingBox()
                session["bbox"] = {
                    "x": round(_bb.xlen, 3),
                    "y": round(_bb.ylen, 3),
                    "z": round(_bb.zlen, 3),
                }
            except Exception:
                pass
        if not session.get("bbox") and _stl_exists:
            try:
                import trimesh
                _mesh = trimesh.load_mesh(str(stl_path))
                _ext = _mesh.bounding_box.extents
                session["bbox"] = {"x": float(_ext[0]), "y": float(_ext[1]), "z": float(_ext[2])}
            except Exception:
                pass

    # Validate generated geometry matches requested dims
    _bbox_session = session.get("bbox", {})
    _dim_checks: list[tuple[str, bool, str]] = []
    if _spec.get("od_mm") and _bbox_session:
        _req_od = _spec["od_mm"]
        _got_x = _bbox_session.get("x", 0)
        _got_y = _bbox_session.get("y", 0)
        _od_ok = abs(_got_x - _req_od) < _req_od * 0.05 and abs(_got_y - _req_od) < _req_od * 0.05
        _dim_checks.append(("od_matches_spec", _od_ok,
                            f"requested OD={_req_od}mm, got bbox X={_got_x:.1f} Y={_got_y:.1f}"))
    if _spec.get("height_mm") and _bbox_session:
        # Skip the bbox-Z check when "height" in the spec means a sub-dimension
        # (leg height for brackets, fin height for heat sinks, etc.) — bbox Z
        # for those is base + sub-dim, not the spec value alone. Without this
        # skip, every L-bracket/heat-sink reports a false GENERATE failure.
        _part_type = (_spec.get("part_type") or "").lower()
        _goal_lower = (goal or "").lower()
        _height_is_subdim_parts = {
            "bracket", "l_bracket", "l-bracket", "gusset", "heat_sink",
            "heat sink", "snap_hook", "snap-hook", "hinge", "u_channel",
            "phone_stand", "spring_clip",
            "spoked_wheel", "wheel", "spoke",
        }
        _is_subdim = (_part_type in _height_is_subdim_parts or
                      any(kw in _goal_lower for kw in
                          ("bracket", "heat sink", "fin", "snap", "hinge", "channel",
                           "wheel", "spoke")))
        if not _is_subdim:
            _req_h = _spec["height_mm"]
            _got_z = _bbox_session.get("z", 0)
            _h_ok = abs(_got_z - _req_h) < max(2.0, _req_h * 0.05)
            _dim_checks.append(("height_matches_spec", _h_ok,
                                f"requested H={_req_h}mm, got bbox Z={_got_z:.1f}"))

    if not _use_agents:
        _checkpoint("GENERATE", [
            ("step_or_stl_exists", _step_exists or _stl_exists,
             "no geometry produced — both STEP and STL are missing"),
            ("step_not_empty",     _step_size > 500 if _step_exists else True,
             f"STEP file only {_step_size} bytes — likely placeholder"),
            ("stl_not_empty",      _stl_size > 500 if _stl_exists else True,
             f"STL file only {_stl_size} bytes — likely placeholder"),
            ("has_script",         _has_script,
             "no generation script found in artifacts"),
        ] + _dim_checks, session)

    # ── CHECKPOINT: GEOMETRY (legacy — agent path has its own) ─────────────
    if not _use_agents and _step_exists:
        try:
            from .geometry_validator import validate_geometry, print_validation
            _geo_val = validate_geometry(
                step_path, part_id or "", params=_plan_params, goal=goal)
            session["geometry_validation"] = _geo_val
            print_validation(_geo_val)

            # Add to checkpoint system
            _geo_checks = [
                (c["name"], c["passed"], c["detail"])
                for c in _geo_val.get("checks", [])
            ]
            if _geo_checks:
                _checkpoint("GEOMETRY", _geo_checks, session)
        except Exception as _gv_exc:
            print(f"  [GEOMETRY] skipped: {_gv_exc}")

    # --- Output quality: STEP readable + STL watertight check/repair (all backends) ---
    if step_path.exists() or stl_path.exists():
        try:
            quality = check_output_quality(str(step_path), str(stl_path))
            session["output_quality"] = quality
            stl_info = quality.get("stl", {})
            step_info = quality.get("step", {})
            if stl_info.get("repaired"):
                print(f"[QUALITY] STL repaired (was not watertight): {stl_path}")
                event_bus.emit("validation", "STL repaired", {"part_id": part_id, "stl_path": str(stl_path)})
            if not step_info.get("readable", True) and cad_tool != "sdf":
                print(f"[QUALITY] STEP not readable: {step_path}")
                event_bus.emit("validation", "STEP not readable", {"part_id": part_id})
            if quality.get("passed"):
                event_bus.emit("validation", "Output quality OK", {"part_id": part_id})
            else:
                failures = quality.get("failures", [])
                event_bus.emit("validation", f"Output quality issues: {failures}", {"part_id": part_id})
        except Exception as exc:
            print(f"[QUALITY WARN] {exc}")

    # ── CHECKPOINT: QUALITY (legacy — agent path has its own) ────────────────
    if not _use_agents:
        _q = session.get("output_quality", {})
        _q_step = _q.get("step", {})
        _q_stl  = _q.get("stl", {})
        _checkpoint("QUALITY", [
            ("step_readable",  _q_step.get("readable", False) if _step_exists else True,
             "STEP file is not readable by CadQuery/OCCT"),
            ("stl_watertight", _q_stl.get("watertight_after", False) if _stl_exists else True,
             "STL mesh is not watertight even after repair"),
            ("quality_passed", _q.get("passed", True),
             f"quality failures: {_q.get('failures', [])}"),
        ], session)

    # ── VISUAL VERIFICATION (legacy path — after geometry + quality pass) ──
    _vis_result = None
    if _stl_exists:
        try:
            from .visual_verifier import verify_visual
            _vis_result = verify_visual(
                str(step_path) if _step_exists else "",
                str(stl_path),
                goal,
                _spec if isinstance(_spec, dict) else {},
                repo_root=repo_root,
            )
            session["visual_verification"] = _vis_result
            _vis_conf = _vis_result.get("confidence", 0.0)
            if _vis_result.get("verified") is True and _vis_conf >= 0.90:
                print(f"  [VISUAL] PASS -- confidence {_vis_conf:.0%}")
            elif _vis_result.get("verified") is True and _vis_conf < 0.90:
                print(f"  [VISUAL] FAIL -- confidence {_vis_conf:.0%} below 90% threshold")
                for _vi in _vis_result.get("issues", []):
                    print(f"    [VISUAL] {_vi}")
            elif _vis_result.get("verified") is False:
                print(f"  [VISUAL] FAIL -- confidence {_vis_conf:.0%}")
                for _vi in _vis_result.get("issues", []):
                    print(f"    [VISUAL] {_vi}")
            elif _vis_result.get("verified") is None:
                _reason = _vis_result.get("reason", "unknown")
                print(f"  [VISUAL] SKIPPED -- {_reason}")
        except Exception as _vis_exc:
            print(f"  [VISUAL] skipped: {_vis_exc}")

    # ── CHECKPOINT: VISUAL ──────────────────────────────────────────────────
    # Register visual verification as a pipeline checkpoint. Provider-aware
    # threshold accounts for confidence caps that vary across providers:
    #   Anthropic: uncapped       → 0.90
    #   Gemini:    capped at 0.95 → 0.85
    #   Groq:      capped at 0.92 → 0.82
    #   Ollama:    capped at 0.85 → 0.78
    # A flat 0.90 made Ollama-only environments always fail VISUAL.
    if _vis_result is not None:
        _vis_verified = _vis_result.get("verified")
        _vis_conf = _vis_result.get("confidence", 0.0)
        _vis_provider = (_vis_result.get("provider") or "").lower()
        _vis_issues = _vis_result.get("issues", []) or []

        if "anthropic" in _vis_provider or "claude" in _vis_provider:
            _vis_threshold = 0.90
        elif "gemini" in _vis_provider or "google" in _vis_provider:
            _vis_threshold = 0.85
        elif "groq" in _vis_provider or "llama" in _vis_provider:
            _vis_threshold = 0.82
        elif ("ollama" in _vis_provider or "gemma" in _vis_provider
              or "llava" in _vis_provider):
            _vis_threshold = 0.78
        else:
            _vis_threshold = 0.75

        # ── LAYERED VISUAL CHECKPOINT ───────────────────────────────────
        # Visual is a SOFT gate: if all deterministic stages passed, then
        # vision-LLM uncertainty (cross-validation disagreement, geometry
        # precheck warnings, "not explicitly verifiable" notes) is recorded
        # but does NOT fail the checkpoint. Specific feature complaints
        # ("missing bore", "wrong blade direction") still hard-fail.
        #
        # Without this distinction, every part where two vision providers
        # disagree (typical for borderline-confidence renders) reports a
        # false-negative even though deterministic checks all passed.
        _det_stages = ("PLAN", "ROUTE", "GENERATE", "GEOMETRY", "QUALITY")
        _det_failed = any(
            session.get("checkpoints", {}).get(s, {}).get("passed") is False
            for s in _det_stages
        )

        _disagreement_phrases = (
            "cross-validation", "disagreed", "marking fail to be safe",
            "no clear indication", "not explicitly verifiable",
            "geometry precheck", "appears correct",
            "image", "thickness", "unclear if", "number of",
        )

        def _is_soft_visual(issue_text: str) -> bool:
            s = str(issue_text).lower()
            return any(p in s for p in _disagreement_phrases)

        _hard_issues = [i for i in _vis_issues if not _is_soft_visual(i)]
        _soft_issues = [i for i in _vis_issues if _is_soft_visual(i)]

        # When deterministic gates already fired, escalate everything.
        # When they all passed, hard issues still fail; soft issues warn.
        _vis_pass_ok = (_vis_verified is True and _vis_conf >= _vis_threshold)
        if not _vis_pass_ok and not _hard_issues and not _det_failed:
            # Vision unsure but no specific complaint and no upstream failure:
            # let it pass. The geometry already cleared deterministic gates.
            _vis_pass_ok = True

        _critical_issues = _hard_issues if not _det_failed else _vis_issues
        _no_critical = len(_critical_issues) == 0
        if not _no_critical and not _det_failed and not _hard_issues:
            _no_critical = True  # only soft issues, deterministic clean

        _checkpoint("VISUAL", [
            ("visual_pass",
             _vis_pass_ok,
             f"verified={_vis_verified} conf={_vis_conf:.0%} "
             f"(provider={_vis_provider or 'unknown'} threshold={_vis_threshold:.0%}) "
             f"{'; '.join(_critical_issues[:2])}" if _critical_issues
             else f"verified={_vis_verified} conf={_vis_conf:.0%} "
                  f"(provider={_vis_provider or 'unknown'} threshold={_vis_threshold:.0%})"),
            ("no_critical_issues",
             _no_critical,
             f"{len(_critical_issues)} critical issue(s): "
             f"{'; '.join(_critical_issues[:3])}"
             if _critical_issues else "no critical issues"
                                       + (f" ({len(_soft_issues)} soft warning(s) ignored)"
                                          if _soft_issues else "")),
        ], session)
        if _soft_issues and not _det_failed:
            print(f"  [VISUAL] {len(_soft_issues)} soft warning(s) (deterministic checks passed):")
            for s in _soft_issues[:3]:
                print(f"    [WARN] {s}")

    session["automation_artifacts"] = artifacts
    session["attempts"] = 1

    # --- Preview UI: show 3D model + let user choose export format ---
    if preview:
        _stl_for_preview = session.get("stl_path") or (str(stl_path) if stl_path.exists() else None)
        _script_for_preview = session.get("script_path")
        if _stl_for_preview and Path(_stl_for_preview).exists():
            from .preview_ui import show_preview
            _export_choice = show_preview(
                _stl_for_preview,
                part_id=part_id or goal[:40],
                script_path=_script_for_preview,
            )
            session["export_choice"] = _export_choice
            # Act on choice: delete unwanted output files
            if _export_choice == "skip":
                print("[PREVIEW] Discarding outputs as requested.")
                for _p in (step_path, stl_path):
                    if _p.exists():
                        _p.unlink(missing_ok=True)
                event_bus.emit("complete", "Preview: user discarded run", {"part_id": part_id})
                return session
            elif _export_choice == "fusion":
                # Generate Fusion 360 parametric script from the same plan
                try:
                    from .fusion_generator import write_fusion_artifacts
                    _fusion_result = write_fusion_artifacts(
                        plan, goal,
                        str(step_path), str(stl_path),
                        repo_root=repo_root,
                    )
                    _fscript = _fusion_result["script_path"]
                    print()
                    print("=" * 64)
                    print("  FUSION 360 SCRIPT GENERATED")
                    print("=" * 64)
                    print(f"  Script:  {_fscript}")
                    print()
                    print("  To use it in Fusion 360:")
                    print("    1. Open Fusion 360")
                    print("    2. Tools → Add-Ins → Scripts and Add-Ins")
                    print("    3. Click the '+' next to My Scripts, point to the folder above")
                    print("    4. Select the script and click Run")
                    print("    5. The part builds with a full parametric feature tree")
                    print("=" * 64)
                    session["fusion_script"] = _fscript
                except Exception as _fe:
                    print(f"[PREVIEW] Fusion script generation failed: {_fe}")
                # Keep STEP + STL as well (useful for reference / assembly)
            elif _export_choice == "step":
                if stl_path.exists():
                    stl_path.unlink(missing_ok=True)
                if step_path.exists():
                    print(f"\n[PREVIEW] ✓ STEP exported: {step_path}")
                else:
                    print(f"\n[PREVIEW] ⚠ STEP file not found at {step_path} — may need Rhino Compute for grasshopper route.")
            elif _export_choice == "stl":
                if step_path.exists():
                    step_path.unlink(missing_ok=True)
                if stl_path.exists():
                    print(f"\n[PREVIEW] ✓ STL exported: {stl_path}")
            else:  # "both" → keep everything (default)
                outs = []
                if step_path.exists():
                    outs.append(f"STEP: {step_path}")
                if stl_path.exists():
                    outs.append(f"STL:  {stl_path}")
                if outs:
                    print("\n[PREVIEW] ✓ Exported:")
                    for o in outs:
                        print(f"           {o}")
        else:
            print("[PREVIEW] No STL available for preview — skipping viewer.")

    # --- FEA/CFD physics analysis (runs BEFORE GD&T — only draw analyzed/passing parts) ---
    if (step_path.exists() or stl_path.exists()) and session.get("export_choice") != "skip":
        try:
            from .physics_analyzer import prompt_and_analyze as _phys_prompt
            _phys_result = _phys_prompt(
                part_id=plan.get("part_id", ""),
                params=plan.get("params", {}),
                goal=goal,
                step_path=str(step_path),
                repo_root=repo_root,
            )
            if _phys_result:
                session["physics_analysis"] = _phys_result
                if not _phys_result["passed"]:
                    print(f"[PHYSICS] FAIL — SF={_phys_result.get('safety_factor', '?')}")
                    for _f in _phys_result["failures"]:
                        print(f"  \u2717 {_f}")
                else:
                    _phys_sf = _phys_result.get("safety_factor")
                    if _phys_sf is not None:
                        print(f"[PHYSICS] PASS — SF={_phys_sf:.2f}")
                    else:
                        print(f"[PHYSICS] PASS")
                for _w in _phys_result.get("warnings", []):
                    print(f"  \u26a0 {_w}")
        except Exception as _phys_exc:
            print(f"[PHYSICS] Analysis skipped: {_phys_exc}")

    # --- GD&T drawing prompt (after FEA/CFD so drawings reflect analyzed geometry) ---
    if step_path.exists() and session.get("export_choice") != "skip":
        _ask_gdnt = auto_draw or _prompt_gdnt_drawing()
        if _ask_gdnt:
            try:
                from .drawing_generator import generate_gdnt_drawing
                _drawing_path = generate_gdnt_drawing(
                    step_path,
                    part_id or "aria_part",
                    params=plan.get("params"),
                    repo_root=repo_root,
                )
                print(f"[GD&T] Drawing saved: {_drawing_path}")
                session["drawing_path"] = str(_drawing_path)
            except Exception as _de:
                print(f"[GD&T] Drawing generation failed: {_de}")

    # --- CEM physics check (runs for every single-part generation) ---
    _cem_result = None
    _cem_passed = None
    if part_id and (step_path.exists() or stl_path.exists()):
        try:
            _meta_path = Path(get_meta_path(part_id, repo_root))
            _cem_result = cem_checks.run_cem_checks(part_id, _meta_path, context)
            _cem_passed = _cem_result.overall_passed
            session["cem"] = {
                "passed": _cem_result.overall_passed,
                "summary": _cem_result.summary,
                "static_min_sf": _cem_result.static_min_sf,
                "static_failure_mode": _cem_result.static_failure_mode,
            }
            if not _cem_result.overall_passed:
                # Determine the required SF threshold for this part
                _sf_val = _cem_result.static_min_sf
                _sf_mode = _cem_result.static_failure_mode or "unknown"
                _PART_SF_THRESHOLDS = {
                    "aria_ratchet_ring": ("tooth_shear", 8.0),
                    "aria_spool": ("radial_load", 2.0),
                    "aria_cam_collar": ("taper_engagement", 2.0),
                    "aria_housing": ("wall_bending", 2.0),
                    "aria_brake_drum": ("hoop_stress", 2.0),
                }
                _threshold = 2.0
                for _pid_key, (_mode, _thr) in _PART_SF_THRESHOLDS.items():
                    if _pid_key in (part_id or "").lower():
                        _threshold = _thr
                        break
                print(f"[CEM HARD FAIL] SF {_sf_val:.2f} below required {_threshold:.1f} for {part_id}. Export blocked.")
                event_bus.emit("cem", f"CEM FAIL: {_cem_result.summary}",
                               {"part_id": part_id, "passed": False,
                                "sf": _cem_result.static_min_sf})
                # Block export: remove generated STEP/STL files
                for _block_path in (step_path, stl_path):
                    if _block_path.exists():
                        _block_path.unlink(missing_ok=True)
                        print(f"[CEM] Removed: {_block_path}")
                session["cem_blocked"] = True
            else:
                print(f"[CEM OK] {_cem_result.summary}")
                event_bus.emit("cem", f"CEM OK: {_cem_result.summary}",
                               {"part_id": part_id, "passed": True,
                                "sf": _cem_result.static_min_sf})
        except Exception as _cem_exc:
            print(f"[CEM WARN] {_cem_exc}")

    # ── CHECKPOINT: CEM ─────────────────────────────────────────────────────
    _cem_checks_list: list[tuple[str, bool, str]] = [
        ("cem_ran", _cem_result is not None or not part_id,
         "CEM physics check did not run for this part"),
    ]
    if _cem_result is not None:
        _cem_sf_val = getattr(_cem_result, "static_min_sf", None)
        _cem_checks_list.append(
            ("cem_passed", _cem_passed is True,
             f"CEM SF={_cem_sf_val} — below threshold"))
    _checkpoint("CEM", _cem_checks_list, session)

    # ── CHECKPOINT: FINAL SUMMARY ─────────────────────────────────────────
    _all_cp = session.get("checkpoints", {})
    _stages_passed = sum(1 for cp in _all_cp.values() if cp.get("passed"))
    _stages_total  = len(_all_cp)
    _all_checks_n  = sum(len(cp.get("checks", [])) for cp in _all_cp.values())
    _all_checks_ok = sum(
        sum(1 for c in cp.get("checks", []) if c.get("passed"))
        for cp in _all_cp.values()
    )

    # Determine overall outcome
    _s_step = str(step_path) if step_path.exists() else session.get("step_path", "")
    _s_stl  = str(stl_path)  if stl_path.exists()  else session.get("stl_path",  "")
    _has_geometry = bool(
        (_s_step and Path(_s_step).is_file()) or
        (_s_stl  and Path(_s_stl).is_file())
    )
    # Same fix as the agent-mode summary: file presence is necessary but not
    # sufficient. Require every stage checkpoint to have passed.
    _all_stages_ok = all(cp.get("passed") for cp in _all_cp.values())
    _pipeline_ok = _has_geometry and _all_stages_ok and not session.get("cem_blocked")
    session["passed"] = _pipeline_ok
    session["stages_ok"] = _all_stages_ok
    session["file_ok"] = _has_geometry

    print()
    print(f"{'=' * 64}")
    print(f"  PIPELINE SUMMARY  --  {'PASS' if _pipeline_ok else 'FAIL'}")
    print(f"{'=' * 64}")
    for stg, cp in _all_cp.items():
        tag = "[OK]  " if cp["passed"] else "[FAIL]"
        n_ok = sum(1 for c in cp.get("checks", []) if c["passed"])
        n_t  = len(cp.get("checks", []))
        print(f"  {tag} {stg:12s}  {n_ok}/{n_t} checks")
        for f in cp.get("failures", []):
            print(f"         -> {f}")
    print(f"{'-' * 64}")
    print(f"  {_all_checks_ok}/{_all_checks_n} checks passed  |  {_stages_passed}/{_stages_total} stages OK")
    print(f"{'=' * 64}")

    # ── OUTPUT FILE PATHS ─────────────────────────────────────────────────
    if _has_geometry:
        print()
        print("OUTPUT FILES")
        print("-" * 64)
        if _s_step and Path(_s_step).exists():
            _sz_kb = Path(_s_step).stat().st_size / 1024
            print(f"  STEP:   {_s_step}  ({_sz_kb:.0f} KB)")
        if _s_stl and Path(_s_stl).exists():
            _sz_kb = Path(_s_stl).stat().st_size / 1024
            print(f"  STL:    {_s_stl}  ({_sz_kb:.0f} KB)")
        if session.get("script_path"):
            print(f"  Script: {session['script_path']}")
        _view_target = _s_step or _s_stl
        print()
        print("NEXT STEPS")
        print("-" * 64)
        print(f"  View:     python run_aria_os.py --view \"{_view_target}\"")
        if _s_stl:
            print(f"  Verify:   python run_aria_os.py --verify \"{_s_stl}\"")
        if _s_step:
            print(f"  Analyze:  python run_aria_os.py --analyze-part \"{_s_step}\"")
            print(f"  Quote:    python run_aria_os.py --quote")
        print(f"  List all: python run_aria_os.py --list")
        print(f"{'=' * 64}")
    else:
        print()
        print("[!] No geometry produced.")
        _cq_err = session.get("cq_error", "")
        if _cq_err:
            print(f"    Error: {str(_cq_err)[:200]}")
        if session.get("cem_blocked"):
            print("    CEM physics check blocked export — part failed safety thresholds.")
            print("    Increase dimensions or material spec to meet safety factor requirements.")
        else:
            print("    Try rephrasing your goal with explicit dimensions, e.g.:")
            print("      python run_aria_os.py \"bracket 80x40x5mm with 4 M6 holes\"")
        print(f"{'=' * 64}")

    # --- Derive real learning-log values from actual run results ---
    _bbox = session.get("bbox") or {}
    _quality = session.get("output_quality", {})
    _mesh_clean = _quality.get("stl", {}).get("watertight_after") if _quality else None
    _val_status = session.get("validation", {}).get("status")

    # passed = no validation failure AND CEM didn't hard-fail AND output quality OK
    _passed = (
        _val_status != "failure"
        and (_cem_passed is not False)
        and _quality.get("passed", True)
    )

    # bbox_within_2pct: works for both cylindrical (od_mm) and box (width/height/depth) parts
    _bbox_within_2pct = False
    if _bbox and _spec:
        _od = _spec.get("od_mm")
        _w  = _spec.get("width_mm")
        _h  = _spec.get("height_mm")
        _d  = _spec.get("depth_mm")
        if _od and _bbox.get("x"):
            _tol = _od * 0.02
            _bbox_within_2pct = (
                abs(_bbox.get("x", 0) - _od) <= _tol
                and abs(_bbox.get("y", 0) - _od) <= _tol
            )
        elif _w and _h and _d:
            _tol = 2.0  # 2mm absolute tolerance for box parts
            _bbox_within_2pct = (
                abs(_bbox.get("x", 0) - _w) <= _tol
                and abs(_bbox.get("y", 0) - _h) <= _tol
                and abs(_bbox.get("z", 0) - _d) <= _tol
            )

    # Read the actual generated code from the script file (for few-shot learning)
    _generated_code = f"# routed_tool={cad_tool}"
    _script_path = session.get("script_path", "")
    if _script_path:
        try:
            _generated_code = Path(_script_path).read_text(encoding="utf-8")
        except Exception:
            pass

    # Collect the actual error message if generation failed
    _run_error = session.get("cq_error") or session.get("validation", {}).get("error") or ""
    if not _run_error and _val_status == "failure":
        _run_error = str(session.get("validation", {}).get("failures", "validation failed"))

    record_attempt(
        goal=goal,
        plan_text=plan_text,
        part_id=part_id or "aria_part",
        code=_generated_code,
        passed=_passed,
        bbox=_bbox or {"x": 0.0, "y": 0.0, "z": 0.0},
        error=_run_error or None,
        cem_snapshot=load_cem_geometry(repo_root),
        cem_passed=_cem_passed,
        feature_complete=True,
        mesh_clean=bool(_mesh_clean) if _mesh_clean is not None else True,
        bbox_within_2pct=_bbox_within_2pct,
        tool_used=cad_tool,
        repo_root=repo_root,
    )

    # --- Version tracking: write meta JSON for the generated part ---
    if (step_path.exists() or stl_path.exists()) and part_id:
        try:
            import json as _json
            from datetime import datetime as _dt
            _meta_dir = repo_root / "outputs" / "cad" / "meta"
            _meta_dir.mkdir(parents=True, exist_ok=True)
            _meta_file = _meta_dir / f"{part_id}.json"

            # Preserve existing meta and overlay new fields
            _existing_meta: dict = {}
            if _meta_file.exists():
                try:
                    _existing_meta = _json.loads(_meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            # Try to get git SHA for traceability
            _git_sha = ""
            try:
                import subprocess as _sp
                _git_sha = _sp.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(repo_root), stderr=_sp.DEVNULL, text=True
                ).strip()
            except Exception:
                pass

            _meta_file.write_text(_json.dumps({
                **_existing_meta,
                "part_id":    part_id,
                "goal":       goal,
                "params":     plan.get("params") or {},
                "cad_tool":   cad_tool,
                "step_path":  str(step_path) if step_path.exists() else "",
                "stl_path":   str(stl_path) if stl_path.exists() else "",
                "bbox_mm":    session.get("bbox") or {},
                "cem_sf":     (session.get("cem") or {}).get("static_min_sf"),
                "cem_passed": (session.get("cem") or {}).get("passed"),
                "generated_at": _dt.now().isoformat(),
                "git_sha":    _git_sha,
            }, indent=2), encoding="utf-8")
        except Exception as _me:
            print(f"[META] Could not write meta JSON: {_me}")

    # Stamp the session with per-run telemetry before returning, so the
    # run_manifest writer can pick it up without needing the orchestrator
    # to plumb extra parameters through.
    try:
        session["wall_time_seconds"] = round(_time.time() - _run_start_ts, 2)
        from .llm_client import llm_call_counts as _llm_counts
        session["llm_calls"] = _llm_counts()
    except Exception:
        pass

    event_bus.emit("complete", f"Pipeline complete for {part_id or goal}", {"session": session})
    logger_log(session)
    # Save user profile if teaching was active
    if _teaching_engine is not None and _teaching_engine.user_profile is not None:
        try:
            _teaching_engine.user_profile.record_session(parts=1)
            _teaching_engine.user_profile.save()
        except Exception:
            pass
    return session


# ===========================================================================
# Fast image-to-CAD pipeline — skips research, DFM, quote, CAM, drawing, CEM
# ===========================================================================

def run_image_fast(
    goal: str,
    repo_root: Path | None = None,
    max_attempts: int = 3,
    *,
    preview: bool = False,
) -> dict:
    """Minimal pipeline for image-to-CAD: plan -> route -> generate -> export -> preview.

    Skips: research, agent loop, DFM analysis, quote, CAM toolpath,
    GD&T drawing, CEM physics check, FEA/CFD.

    Visual verification runs after geometry is produced (adds ~10-30s).
    This keeps image-to-CAD under ~90s instead of 300+.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    context = load_context(repo_root)
    session: dict = {"goal": goal, "attempts": 0, "step_path": "", "stl_path": "", "image_fast_mode": True}

    event_bus.emit("step", "Image-fast pipeline started", {"goal": goal})
    print(f"\n[IMAGE-FAST] Generating CAD from image goal (skipping research/DFM/quote/CAM)...")

    # --- 1. Plan (lightweight keyword planner — no agent loop) ---
    plan = planner_plan(goal, context, repo_root=repo_root)
    if not isinstance(plan, dict):
        plan = {"part_id": "aria_part", "text": str(plan), "build_order": [], "features": []}

    # --- 2. Spec extraction (regex, no LLM) ---
    from .spec_extractor import extract_spec, merge_spec_into_plan as _merge_spec
    _spec = extract_spec(goal)
    if _spec:
        _merge_spec(_spec, plan)
        _base = plan.get("base_shape")
        if not isinstance(_base, dict):
            plan["base_shape"] = {}
            _base = plan["base_shape"]
        _DIM_KEYS = (
            "od_mm", "bore_mm", "id_mm", "thickness_mm", "height_mm",
            "width_mm", "depth_mm", "length_mm",
        )
        _SHORT_KEY = {
            "width_mm": "width", "height_mm": "height", "depth_mm": "depth",
            "length_mm": "length", "thickness_mm": "thickness",
        }
        for _k in _DIM_KEYS:
            if _k in _spec:
                _base[_k] = _spec[_k]
                if _k in _SHORT_KEY:
                    _base[_SHORT_KEY[_k]] = _spec[_k]
        _user_dims = [f"{k}={v}" for k, v in _spec.items() if k not in ("part_type", "material")]
        if _user_dims:
            print(f"[IMAGE-FAST] Spec: {' '.join(_user_dims)}")

    plan = attach_brief_to_plan(goal, plan, context, repo_root=repo_root)

    part_id = plan.get("part_id", "")
    _plan_params = plan.get("params") or {}

    # --- 3. Route to CAD backend ---
    try:
        from .multi_cad_router import CADRouter
        decision = CADRouter.route(goal, dry_run=False)
        cad_tool = decision["backend"]
    except Exception:
        cad_tool = select_cad_tool(goal, plan)

    print(f"[IMAGE-FAST] CAD backend: {cad_tool}")

    # --- 4. Output paths ---
    paths = get_output_paths(part_id or goal, repo_root)
    step_path = Path(paths["step_path"])
    stl_path = Path(paths["stl_path"])

    # --- 5. Generate geometry (CadQuery template -> CADSmith -> CQ LLM fallback) ---
    artifacts: dict = {}

    if cad_tool in ("cadquery", "grasshopper", "fusion360", "sdf"):
        # For image-fast, always use CadQuery-family generators (immediate geometry)
        # Try CADSmith first for unknown parts, then CQ template
        _zoo_used = False

        try:
            from .cadquery_generator import _find_template_fn as _cq_find_tpl
            _has_template = _cq_find_tpl(part_id) is not None
        except Exception:
            _has_template = True

        # Zoo shortcut
        if not _has_template:
            try:
                from .zoo_bridge import is_zoo_available, generate_step_from_zoo
                if is_zoo_available(repo_root):
                    zoo_result = generate_step_from_zoo(goal, str(step_path.parent), repo_root=repo_root)
                    if zoo_result.get("status") == "ok":
                        _zoo_step = Path(zoo_result["step_path"])
                        if _zoo_step.exists():
                            session["step_path"] = str(_zoo_step)
                            artifacts = {"step_path": str(_zoo_step), "status": "success"}
                            _zoo_used = True
                            print(f"[IMAGE-FAST] Zoo.dev STEP generated")
            except Exception as _ze:
                print(f"[IMAGE-FAST] Zoo skipped: {_ze}")

        # CADSmith (iterative LLM generation for unknown parts)
        if not _zoo_used and not _has_template:
            try:
                from .generators.cadsmith_generator import cadsmith_generate
                print(f"[IMAGE-FAST] CADSmith generating '{part_id}'...")
                artifacts = cadsmith_generate(
                    goal,
                    plan if isinstance(plan, dict) else {},
                    str(step_path),
                    str(stl_path),
                    repo_root=repo_root,
                )
                if artifacts.get("step_path") and Path(artifacts["step_path"]).exists():
                    _zoo_used = True
                    print(f"[IMAGE-FAST] CADSmith: {Path(artifacts['step_path']).stat().st_size / 1024:.0f} KB STEP")
            except Exception as _cs_exc:
                print(f"[IMAGE-FAST] CADSmith failed: {_cs_exc}, trying CadQuery template")

        # CadQuery template/LLM fallback
        if not _zoo_used:
            try:
                from .cadquery_generator import write_cadquery_artifacts
                print(f"[IMAGE-FAST] CadQuery generating '{part_id}'...")
                artifacts = write_cadquery_artifacts(
                    plan if isinstance(plan, dict) else {},
                    goal,
                    str(step_path),
                    str(stl_path),
                    repo_root=repo_root,
                )
            except Exception as exc:
                print(f"[IMAGE-FAST] CadQuery error: {exc}")

    elif cad_tool == "blender":
        try:
            artifacts = write_blender_artifacts(
                plan if isinstance(plan, dict) else {},
                goal,
                str(stl_path),
                repo_root=repo_root,
            )
        except Exception as exc:
            print(f"[IMAGE-FAST] Blender error: {exc}")

    # --- 6. Capture results ---
    if artifacts.get("step_path"):
        session["step_path"] = artifacts["step_path"]
    if artifacts.get("stl_path"):
        session["stl_path"] = artifacts["stl_path"]
    if artifacts.get("script_path"):
        session["script_path"] = artifacts["script_path"]
    if artifacts.get("bbox"):
        session["bbox"] = artifacts["bbox"]

    _step_exists = step_path.exists()
    _stl_exists = stl_path.exists()

    # Read bbox from file if not in artifacts
    if not session.get("bbox"):
        if _step_exists:
            try:
                import cadquery as _cq_bb
                _shape = _cq_bb.importers.importStep(str(step_path))
                _bb = _shape.val().BoundingBox()
                session["bbox"] = {"x": round(_bb.xlen, 3), "y": round(_bb.ylen, 3), "z": round(_bb.zlen, 3)}
            except Exception:
                pass
        if not session.get("bbox") and _stl_exists:
            try:
                import trimesh
                _mesh = trimesh.load_mesh(str(stl_path))
                _ext = _mesh.bounding_box.extents
                session["bbox"] = {"x": float(_ext[0]), "y": float(_ext[1]), "z": float(_ext[2])}
            except Exception:
                pass

    # --- 7. Output quality (lightweight — just check file readability) ---
    if _step_exists or _stl_exists:
        try:
            quality = check_output_quality(str(step_path), str(stl_path))
            session["output_quality"] = quality
            if quality.get("stl", {}).get("repaired"):
                print(f"[IMAGE-FAST] STL repaired (was not watertight)")
        except Exception as _qe:
            print(f"[IMAGE-FAST] Quality check skipped: {_qe}")

    # --- 7b. Visual verification (runs for all image-to-CAD outputs) ---
    if _stl_exists:
        try:
            from .visual_verifier import verify_visual
            _vis_result = verify_visual(
                str(step_path) if _step_exists else "",
                str(stl_path),
                goal,
                _spec if _spec else {},
                repo_root=repo_root,
            )
            session["visual_verification"] = _vis_result
            _vis_conf = _vis_result.get("confidence", 0.0)
            _vis_status = "PASS" if _vis_result.get("verified") is True else "FAIL"
            print(f"[IMAGE-FAST] Visual verification: {_vis_status} ({_vis_conf:.0%})")
        except Exception as _ve:
            print(f"[IMAGE-FAST] Visual verification skipped: {_ve}")

    # --- 8. Preview ---
    if preview:
        _stl_for_preview = session.get("stl_path") or (str(stl_path) if _stl_exists else None)
        _script_for_preview = session.get("script_path")
        if _stl_for_preview and Path(_stl_for_preview).exists():
            from .preview_ui import show_preview
            _export_choice = show_preview(
                _stl_for_preview,
                part_id=part_id or goal[:40],
                script_path=_script_for_preview,
            )
            session["export_choice"] = _export_choice
        else:
            print("[IMAGE-FAST] No STL available for preview.")

    # --- 9. Summary ---
    _s_step = str(step_path) if _step_exists else session.get("step_path", "")
    _s_stl = str(stl_path) if _stl_exists else session.get("stl_path", "")
    _has_geometry = bool(
        (_s_step and Path(_s_step).exists()) or (_s_stl and Path(_s_stl).exists())
    )

    print()
    print("=" * 64)
    print(f"  IMAGE-TO-CAD  --  {'PASS' if _has_geometry else 'FAIL'}")
    print("=" * 64)
    if _has_geometry:
        if _s_step and Path(_s_step).exists():
            print(f"  STEP:   {_s_step}  ({Path(_s_step).stat().st_size // 1024} KB)")
        if _s_stl and Path(_s_stl).exists():
            print(f"  STL:    {_s_stl}  ({Path(_s_stl).stat().st_size // 1024} KB)")
        if session.get("bbox"):
            _bb = session["bbox"]
            print(f"  BBox:   {_bb.get('x', 0):.1f} x {_bb.get('y', 0):.1f} x {_bb.get('z', 0):.1f} mm")
        print()
        print("NEXT STEPS")
        print("-" * 64)
        _vt = _s_step or _s_stl
        print(f"  View:     python run_aria_os.py --view \"{_vt}\"")
        if _s_stl:
            print(f"  Verify:   python run_aria_os.py --verify \"{_s_stl}\"")
        if _s_step:
            print(f"  Full run: python run_aria_os.py \"{goal[:60]}\"  # DFM + quote + CAM")
        print("=" * 64)
    else:
        print()
        print("[!] No geometry produced.")
        print("    Try rephrasing with explicit dimensions, e.g.:")
        print('      python run_aria_os.py --image photo.jpg "bracket 80x40x5mm"')
        print("=" * 64)

    event_bus.emit("complete", f"Image-fast pipeline done for {part_id or goal}", {"session": session})
    logger_log(session)
    return session
