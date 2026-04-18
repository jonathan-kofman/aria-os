"""
Unified build pipeline — single function that takes a drone preset and
produces the complete manufacturing bundle:

  1. Mechanical assembly (drone_quad or drone_quad_military)
  2. ECAD (KiCad PCB scripts + BOMs + populated 3D PCB STEPs)
  3. GD&T drawings (SVG per top-level part)
  4. Print bundle (oriented STLs + Elegoo Slicer config + README)
  5. CAM scripts (Fusion 360 toolpaths for non-printable parts)
  6. Preview manifest (paths to thumbnails for the UI tile)

The output directory becomes a single ZIP-able bundle that contains
everything needed to actually MAKE the drone.

Entry point:
    from aria_os.build_pipeline import run_full_build
    result = run_full_build(preset_id="military_recon")
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BuildResult:
    preset_id: str
    name: str
    output_dir: str
    success: bool = False
    elapsed_s: float = 0.0
    error: str | None = None

    # Stage outcomes
    mech_success: bool = False
    ecad_success: bool = False
    drawings_success: bool = False
    diy_fab_success: bool = False
    drc_success: bool = False      # KiCad kicad-cli pcb drc — fab-ready check
    autoroute_success: bool = False  # Freerouting Specctra autorouter
    fea_success: bool = False      # gmsh + CalculiX static-linear FEA
    print_success: bool = False
    cam_success: bool = False
    sim_success: bool = False        # Genesis flight dynamics
    circuit_sim_success: bool = False # PySpice analog circuit sim

    # Computed totals
    total_mass_g: float = 0.0        # Sum of every part's STEP-volume × density
    total_cost_usd: float = 0.0      # Print + CNC + PCB + electronics + fasteners
    cost_breakdown_path: str | None = None

    # Artifact paths (relative to repo root for transport)
    step_path: str | None = None
    stl_path: str | None = None
    render_path: str | None = None
    bom_path: str | None = None
    print_dir: str | None = None
    cam_dir: str | None = None
    drawings_dir: str | None = None
    instructions_path: str | None = None
    instructions_pdf_path: str | None = None
    fasteners_path: str | None = None
    sim_trace_path: str | None = None
    sim_summary: dict | None = None
    circuit_sim_summary: dict | None = None

    # Preview thumbnails (PNGs + SVGs) for the "what's in the box" UI tile
    preview_artifacts: list[dict] = field(default_factory=list)

    # Cross-project bridges (StructSight judgment + MillForge pre-CAM handoff)
    structsight_judgment: dict | None = None
    millforge_handoff: dict | None = None

    # Per-board ECAD artifacts (gerber paths + zip + file counts).
    # Populated from drone_quad_result.json["ecad"] so the bundle + summary
    # report which boards have fab-ready Gerbers.
    ecad: dict = field(default_factory=dict)

    # Per-board DIY fab artifacts (CNC isolation G-code + printed substrate STL
    # + copper-tape SVG + solder-paste stencil STL) for in-house PCB fab on a
    # 3D printer + CNC instead of a PCB house.
    diy_fab: dict = field(default_factory=dict)

    # Per-board DRC results from kicad-cli (pro-grade fab validation).
    drc: dict = field(default_factory=dict)
    # Per-board autoroute results from Freerouting.
    autoroute: dict = field(default_factory=dict)
    # Per-part FEA results from gmsh + CalculiX.
    fea: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "name": self.name,
            "output_dir": self.output_dir,
            "success": self.success,
            "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
            "stages": {
                "mechanical": self.mech_success,
                "ecad":       self.ecad_success,
                "drawings":   self.drawings_success,
                "diy_fab":    self.diy_fab_success,
                "drc":        self.drc_success,
                "autoroute":  self.autoroute_success,
                "fea":        self.fea_success,
                "print":      self.print_success,
                "cam":        self.cam_success,
                "sim":        self.sim_success,
                "circuit_sim": self.circuit_sim_success,
            },
            "total_mass_g": self.total_mass_g,
            "total_cost_usd": self.total_cost_usd,
            "cost_breakdown_path": self.cost_breakdown_path,
            "sim_summary": self.sim_summary,
            "sim_trace_path": self.sim_trace_path,
            "circuit_sim_summary": self.circuit_sim_summary,
            "step_path": self.step_path,
            "stl_path":  self.stl_path,
            "render_path": self.render_path,
            "bom_path":  self.bom_path,
            "print_dir": self.print_dir,
            "cam_dir":   self.cam_dir,
            "drawings_dir": self.drawings_dir,
            "instructions_path": self.instructions_path,
            "instructions_pdf_path": self.instructions_pdf_path,
            "fasteners_path": self.fasteners_path,
            "preview_artifacts": self.preview_artifacts,
            "structsight_judgment": self.structsight_judgment,
            "millforge_handoff": self.millforge_handoff,
            "ecad": self.ecad,
            "diy_fab": self.diy_fab,
            "drc": self.drc,
            "autoroute": self.autoroute,
            "fea": self.fea,
        }


def run_full_build(*, preset_id: str, params: dict | None = None,
                   on_stage: callable = None) -> BuildResult:
    """Run the complete build for a preset. Returns a BuildResult with all
    artifact paths. Each stage is independent — failure in one doesn't abort
    the others (so you still get whatever did succeed).

    on_stage(stage_name, status, elapsed_s, **extra) is called at the start
    and end of each stage so callers (e.g. the dashboard preset endpoint)
    can show live progress instead of just "building" while a 30s build runs.
    """
    t0 = time.monotonic()
    def _stage(name, status, **extra):
        if on_stage:
            try:
                on_stage(name, status, time.monotonic() - t0, **extra)
            except Exception:
                pass

    # ── Stage 0: StructSight engineering judgment (cited, typed) ─────────────
    # Runs before geometry so the judgment can inform downstream reviewers.
    # structsight is an optional sibling package — gracefully degrade if absent.
    _stage("structsight", "start")
    goal_text = _preset_goal(preset_id, params)
    structsight_judgment = _run_structsight(goal_text)
    _stage("structsight", "done",
           available=structsight_judgment.get("available", False))

    _stage("mechanical", "start")

    # ── Stage 1: Mechanical assembly (+ ECAD + drawings inside the drone module) ──
    name, output_dir, mech_ok = _stage_mechanical(preset_id, params)
    result = BuildResult(preset_id=preset_id, name=name, output_dir=str(output_dir))
    result.structsight_judgment = structsight_judgment
    _stage("mechanical", "done" if mech_ok else "fail")
    if not mech_ok:
        result.error = "mechanical assembly failed — see drone_quad_result.json"
        result.elapsed_s = time.monotonic() - t0
        _write_summary(result)
        return result
    result.mech_success = True

    # Pull paths from the drone result file
    drone_result_path = output_dir / "drone_quad_result.json"
    if drone_result_path.is_file():
        try:
            dr = json.loads(drone_result_path.read_text(encoding="utf-8"))
            result.step_path   = dr.get("step_path")
            result.stl_path    = dr.get("stl_path")
            result.render_path = dr.get("render_path")
            result.bom_path    = dr.get("bom_path")
            ecad = dr.get("ecad") or {}
            result.ecad_success = any(
                isinstance(v, dict) and not v.get("error") for v in ecad.values()
            )
            # Surface per-board gerber artifacts on BuildResult so the bundle +
            # build_summary.json report fab-ready outputs (zips + file counts).
            if isinstance(ecad, dict):
                result.ecad = {
                    board: {
                        "kicad_pcb_path": v.get("kicad_pcb_path"),
                        "bom_path":       v.get("bom_path"),
                        "gerber_dir":     v.get("gerber_dir"),
                        "n_gerber_files": v.get("n_gerber_files", 0),
                        "gerber_zip_path": v.get("gerber_zip_path"),
                        "error":          v.get("error"),
                    }
                    for board, v in ecad.items()
                    if isinstance(v, dict)
                }
            drawings = dr.get("drawings") or {}
            result.drawings_dir = str(output_dir / "drawings") if drawings else None
            result.drawings_success = bool(drawings) and "error" not in drawings
        except Exception:
            pass

    _stage("ecad",     "done" if result.ecad_success     else "skip")
    _stage("drawings", "done" if result.drawings_success else "skip")

    # ── Stage 1.4: DIY fab (home-made PCBs from .kicad_pcb) ──────────────
    # Turn each board's .kicad_pcb into:
    #   • CNC isolation G-code   (mill copper-clad FR4/FR1 on the CNC)
    #   • printed substrate STL  (print channels, inlay copper-foil tape)
    #   • copper-tape cut SVG    (vinyl-cut tape to match channels)
    #   • solder-paste stencil STL
    # so the user can fab boards in-house without a PCB house.
    # Non-blocking: skip silently if no .kicad_pcb files were produced.
    _stage("diy_fab", "start")
    diy_any_ok = False
    if isinstance(result.ecad, dict) and result.ecad:
        try:
            from aria_os.ecad.diy_fab import run_diy_fab
            for board_name, v in result.ecad.items():
                pcb_path = v.get("kicad_pcb_path") if isinstance(v, dict) else None
                if not pcb_path or not Path(pcb_path).is_file():
                    continue
                try:
                    board_out = Path(result.output_dir) / "ecad" / board_name
                    board_out.mkdir(parents=True, exist_ok=True)
                    r = run_diy_fab(pcb_path, board_out, route="both")
                    result.diy_fab[board_name] = {
                        "out_dir": r.get("out_dir"),
                        "paths": r.get("paths", {}),
                        "n_traces": r.get("n_traces", 0),
                        "board_size_mm": r.get("board_size_mm"),
                    }
                    diy_any_ok = True
                except Exception as exc:
                    result.diy_fab[board_name] = {
                        "error": f"{type(exc).__name__}: {exc}"}
        except Exception as exc:
            print(f"[build] diy_fab import failed: {type(exc).__name__}: {exc}")
    result.diy_fab_success = diy_any_ok
    _stage("diy_fab", "done" if diy_any_ok else "skip",
           n_boards=sum(1 for v in result.diy_fab.values()
                        if isinstance(v, dict) and "error" not in v))

    # ── Stage 1.41: DRC — pro-grade PCB validation via kicad-cli ─────────
    # Runs only if kicad-cli is on PATH; otherwise skips gracefully with a
    # hint. Fails the stage on real DRC errors; unconnected + parity checks
    # are reported as info but don't break the build.
    _stage("drc", "start")
    drc_any_ok = False
    drc_any_run = False
    if isinstance(result.ecad, dict) and result.ecad:
        try:
            from aria_os.ecad.drc_check import run_drc
            for board_name, v in result.ecad.items():
                pcb_path = v.get("kicad_pcb_path") if isinstance(v, dict) else None
                if not pcb_path or not Path(pcb_path).is_file():
                    continue
                out = Path(result.output_dir) / "ecad" / board_name / "drc"
                r = run_drc(pcb_path, out)
                result.drc[board_name] = r
                if r.get("available"):
                    drc_any_run = True
                    if r.get("passed"):
                        drc_any_ok = True
        except Exception as exc:
            print(f"[build] drc import failed: {type(exc).__name__}: {exc}")
    result.drc_success = drc_any_ok
    if not drc_any_run:
        _stage("drc", "skip", reason="kicad-cli not installed")
    else:
        _stage("drc", "done" if drc_any_ok else "fail",
               n_boards=sum(1 for v in result.drc.values()
                            if isinstance(v, dict) and v.get("passed")))

    # ── Stage 1.42: Autoroute — Freerouting Specctra routing ─────────────
    # Optional: replaces the naive star-routing in kicad_pcb_writer with a
    # real autorouter for boards that need proper trace routing.
    _stage("autoroute", "start")
    ar_any_ok = False
    ar_any_run = False
    if isinstance(result.ecad, dict) and result.ecad:
        try:
            from aria_os.ecad.autoroute import run_autoroute
            for board_name, v in result.ecad.items():
                pcb_path = v.get("kicad_pcb_path") if isinstance(v, dict) else None
                if not pcb_path or not Path(pcb_path).is_file():
                    continue
                out = Path(result.output_dir) / "ecad" / board_name / "autoroute"
                r = run_autoroute(pcb_path, out)
                result.autoroute[board_name] = r
                if r.get("available"):
                    ar_any_run = True
                    if r.get("routed_pcb_path"):
                        ar_any_ok = True
        except Exception as exc:
            print(f"[build] autoroute import failed: {type(exc).__name__}: {exc}")
    result.autoroute_success = ar_any_ok
    if not ar_any_run:
        _stage("autoroute", "skip", reason="freerouting.jar or java missing")
    else:
        _stage("autoroute", "done" if ar_any_ok else "fail")

    # ── Stage 1.43: FEA — static-linear stress via gmsh + CalculiX ───────
    # Per metal part, compute max von Mises under a representative load,
    # assert safety_factor >= 2 against material yield. Skips gracefully
    # if ccx (CalculiX) isn't installed.
    _stage("fea", "start")
    fea_any_ok = False
    fea_any_run = False
    parts_dir_for_fea = Path(result.output_dir) / "parts"
    if result.bom_path and Path(result.bom_path).is_file() and parts_dir_for_fea.is_dir():
        try:
            from aria_os.fea.calculix_stage import (
                run_static_fea, MATERIAL_PROPS)
            import json as _j
            bom = _j.loads(Path(result.bom_path).read_text(encoding="utf-8"))
            # Only analyse metal parts (plastic/composite FEA needs nonlinear
            # or orthotropic models we haven't wired yet).
            metal_prefixes = ("aluminum", "steel", "stainless",
                              "titanium", "brass")
            parts = bom.get("parts", []) if isinstance(bom, dict) else []
            for p in parts:
                mat = (p.get("material") or "").lower()
                if not any(mat.startswith(m) for m in metal_prefixes):
                    continue
                if mat not in MATERIAL_PROPS:
                    continue
                step_rel = p.get("step_path") or p.get("geometry_step")
                if not step_rel:
                    continue
                step_abs = (Path(result.output_dir) / step_rel
                            if not Path(step_rel).is_absolute()
                            else Path(step_rel))
                if not step_abs.is_file():
                    continue
                part_name = p.get("name") or Path(step_rel).stem
                out = Path(result.output_dir) / "fea" / part_name
                # 100N default test load — representative for bracket/mount
                r = run_static_fea(step_abs, material=mat, load_n=100.0,
                                   out_dir=out, mesh_size_mm=5.0)
                result.fea[part_name] = r
                if r.get("available"):
                    fea_any_run = True
                    if r.get("passed"):
                        fea_any_ok = True
        except Exception as exc:
            print(f"[build] fea import failed: {type(exc).__name__}: {exc}")
    result.fea_success = fea_any_ok
    if not fea_any_run:
        _stage("fea", "skip", reason="ccx (CalculiX) not installed")
    else:
        _stage("fea", "done" if fea_any_ok else "fail",
               n_parts=sum(1 for v in result.fea.values()
                           if isinstance(v, dict) and v.get("passed")))

    # ── Stage 1.5: per-part mass calculation ──────────────────────────────
    # Compute mass_g per part from STEP volume × material density and write
    # the populated values back to the BOM. Unblocks accurate flight sim
    # TWR, MillForge cost quotes, and slicer print-time estimates that all
    # used to read mass_g=0.
    _stage("mass", "start")
    parts_dir = Path(result.output_dir) / "parts"
    if result.bom_path and Path(result.bom_path).is_file() and parts_dir.is_dir():
        try:
            from aria_os.mass_calc import populate_bom_masses
            updated_bom = populate_bom_masses(result.bom_path, parts_dir)
            result.total_mass_g = float(updated_bom.get("total_mass_g", 0.0))
            _stage("mass", "done", total_mass_g=result.total_mass_g)
        except Exception as exc:
            print(f"[build] mass calc skipped: {type(exc).__name__}: {exc}")
            _stage("mass", "fail")
    else:
        _stage("mass", "skip")

    # ── Stage 1.75: Human-readable assembly instructions ─────────────────
    # Turn the populated BOM + placer-captured positions into
    # assembly_instructions.md (and PDF if a renderer is installed) so the
    # person actually building the drone has a "here's how" doc in the
    # bundle — not just CAD + drawings.
    _stage("instructions", "start")
    if result.bom_path and Path(result.bom_path).is_file():
        try:
            from aria_os.assembly_instructions import (
                generate_assembly_md, generate_assembly_pdf,
            )
            md_path = generate_assembly_md(result.bom_path, output_dir)
            result.instructions_path = str(md_path)
            pdf_path = generate_assembly_pdf(md_path)
            if pdf_path is not None:
                result.instructions_pdf_path = str(pdf_path)
            _stage("instructions", "done",
                   instructions_path=result.instructions_path,
                   pdf=bool(result.instructions_pdf_path))
        except Exception as exc:
            print(f"[build] instructions skipped: {type(exc).__name__}: {exc}")
            _stage("instructions", "fail")
    else:
        _stage("instructions", "skip")

    # ── Stage 1.9: Bill-of-Fasteners (aggregated hardware buy-list) ──────
    # Walk the BOM, roll up motor/arm/standoff/etc. fastener counts, and
    # emit fasteners.md with McMaster/BoltDepot SKUs + estimated cost. The
    # per-step assembly_instructions.md covers WHERE they go; this stage
    # covers WHAT TO BUY.
    _stage("fasteners", "start")
    if result.bom_path and Path(result.bom_path).is_file():
        try:
            from aria_os.fasteners_bom import (
                aggregate_fasteners, generate_fasteners_md,
            )
            bom = json.loads(Path(result.bom_path).read_text(encoding="utf-8"))
            rows = aggregate_fasteners(bom)
            fm_path = generate_fasteners_md(rows, output_dir)
            result.fasteners_path = str(fm_path)
            _stage("fasteners", "done",
                   fasteners_path=result.fasteners_path,
                   n_rows=len(rows),
                   total_qty=sum(r["qty"] for r in rows),
                   est_cost_usd=round(
                       sum(float(r["est_cost_usd"]) for r in rows), 2))
        except Exception as exc:
            print(f"[build] fasteners skipped: {type(exc).__name__}: {exc}")
            _stage("fasteners", "fail")
    else:
        _stage("fasteners", "skip")

    # ── Stage 1.7: Total cost estimate (sums everything) ──────────────────
    # Headline: total_usd. Sums: print material + CNC stock + machine time +
    # PCB fab + electronics catalog + fasteners. Reads bundle dirs and
    # writes cost_breakdown.json. Turns the bundle into a quotable kit.
    _stage("cost", "start")
    if result.bom_path and Path(result.bom_path).is_file():
        try:
            from aria_os.cost_estimate import estimate_cost
            cost = estimate_cost(result.bom_path, preset_id=preset_id)
            result.total_cost_usd = float(cost["totals"]["total_usd"])
            result.cost_breakdown_path = cost.get("cost_breakdown_path")
            _stage("cost", "done",
                   total_usd=result.total_cost_usd,
                   breakdown=cost["totals"])
        except Exception as exc:
            print(f"[build] cost estimate skipped: {type(exc).__name__}: {exc}")
            _stage("cost", "fail")
    else:
        _stage("cost", "skip")

    # ── Stage 2: Print bundle (slicer-ready STLs + Elegoo config) ────────────
    _stage("print", "start")
    try:
        from aria_os.slicer import prepare_for_print
        print_summary = prepare_for_print(output_dir)
        result.print_dir = print_summary.get("print_dir")
        result.print_success = print_summary.get("n_print_parts", 0) > 0
    except Exception as exc:
        result.print_success = False
        print(f"[build] print prep skipped: {type(exc).__name__}: {exc}")
    _stage("print", "done" if result.print_success else "fail")

    # ── Stage 3: CAM scripts (CNC mill toolpaths for CFRP/aluminum) ──────────
    _stage("cam", "start")
    cam_dir = output_dir / "cam"
    cam_dir.mkdir(parents=True, exist_ok=True)
    cam_count = _stage_cam(output_dir, cam_dir)
    result.cam_dir = str(cam_dir) if cam_count > 0 else None
    result.cam_success = cam_count > 0
    _stage("cam", "done" if result.cam_success else "skip", n_parts=cam_count)

    # ── Stage 4: Flight dynamics sim (Genesis if installed, else stub) ────────
    _stage("sim", "start")
    sim_dir = output_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    if result.stl_path and Path(result.stl_path).is_file():
        try:
            from aria_os.flight_sim import simulate_drone_hover
            # Use real per-part-summed mass from mass_calc when available,
            # else fall back to preset heuristic.
            mass_g = result.total_mass_g if result.total_mass_g > 10.0 else (
                700.0 if "military" in preset_id or "7inch" in preset_id else 400.0
            )
            sim_result = simulate_drone_hover(
                result.stl_path,
                mass_g=mass_g,
                motor_thrust_g=550.0 if "military" in preset_id else 450.0,
                out_dir=sim_dir,
            )
            result.sim_success = bool(sim_result.get("available"))
            result.sim_trace_path = sim_result.get("trace_path")
            result.sim_summary = {
                k: v for k, v in sim_result.items()
                if k not in ("trajectory",)
            }
        except Exception as exc:
            print(f"[build] flight sim skipped: {type(exc).__name__}: {exc}")
            result.sim_success = False
    else:
        result.sim_success = False
    _stage("sim", "done" if result.sim_success else "skip")

    # ── Stage 5: Circuit / electronic sim per ECAD board ────────────────────
    _stage("circuit_sim", "start")
    # Run PySpice (or analytical stub) on each generated PCB to estimate
    # power-rail loads + flag overloaded supplies. Lightweight — runs even
    # without ngspice installed (analytical only).
    try:
        from aria_os.circuit_sim import simulate_from_bom
        # ECAD BOM paths are nested: ecad/{label}/{slug}/*_bom.json
        ecad_boms = list(output_dir.rglob("ecad/**/*_bom.json"))
        circuit_results = []
        for bom in ecad_boms:
            cs = simulate_from_bom(bom, out_dir=bom.parent)
            circuit_results.append({
                "board": bom.parent.name,
                "engine": cs.get("engine"),
                "rails_mA": cs.get("rails_mA"),
                "warnings": cs.get("warnings"),
            })
        if circuit_results:
            result.circuit_sim_success = True
            result.circuit_sim_summary = {"boards": circuit_results}
    except Exception as exc:
        print(f"[build] circuit sim skipped: {type(exc).__name__}: {exc}")
    _stage("circuit_sim", "done" if result.circuit_sim_success else "skip")

    # ── Stage 6: Preview manifest ────────────────────────────────────────────
    result.preview_artifacts = _build_preview_manifest(output_dir, result)

    # ── Stage 7: Index this run into the Graphify knowledge graph ───────────
    # Lets the visual-verify and spec-extraction agents do cheap lookups
    # over the bundle (STEP↔BOM↔drawing relationships) via MCP.
    # No-op if graphify not installed.
    try:
        from aria_os.graphify_setup import build_outputs_graph
        build_outputs_graph(output_dir, run_id=preset_id)
    except Exception:
        pass  # Graph indexing is best-effort, don't break the build

    # ── Stage 8: MillForge pre-CAM bundle handoff ───────────────────────────
    # POSTs bundle metadata to MillForge's /api/aria/bundle so it can register
    # the part in stage 'pending_cam' and start anomaly/scheduling triage.
    _stage("millforge", "start")
    result.millforge_handoff = _post_millforge_bundle(preset_id, output_dir, result)
    _stage("millforge", "done",
           available=result.millforge_handoff.get("available", False))

    result.success = (result.mech_success and
                      (result.print_success or result.cam_success))
    result.elapsed_s = time.monotonic() - t0
    _write_summary(result)
    return result


def _stage_mechanical(preset_id: str, params: dict | None) -> tuple[str, Path, bool]:
    """Dispatch to the right drone build module per preset."""
    try:
        if preset_id == "military_recon":
            from aria_os.drone_quad_military import run_drone_quad_military
            r = run_drone_quad_military(params=params)
        elif preset_id == "5inch_fpv":
            from aria_os.drone_quad import run_drone_quad
            r = run_drone_quad(params=params)
        elif preset_id == "7inch_long_range":
            from aria_os.drone_quad import run_drone_quad
            merged = {
                "frame": {"diagonal_mm": 295.0, "plate_size_mm": 100.0,
                          "arm_length_mm": 145.0, "arm_width_mm": 22.0},
                "prop":  {"dia_mm": 178.0},
                "motor": {"stator_dia_mm": 32.0, "bell_dia_mm": 33.0},
            }
            if params:
                merged.update(params)
            r = run_drone_quad(name="preset_7inch_long_range", params=merged)
        else:
            return (preset_id, Path("outputs"), False)
        return (r.name, Path(r.output_dir), bool(r.success))
    except Exception as exc:
        traceback.print_exc()
        return (preset_id, Path("outputs"), False)


def _stage_cam(output_dir: Path, cam_dir: Path) -> int:
    """Generate Fusion 360 CAM scripts for each non-printable part (CFRP/Al).

    Skips printed parts (PETG/ABS/PC) — those go through the slicer instead.
    Returns count of CAM scripts generated.
    """
    parts_dir = output_dir / "parts"
    bom_path = output_dir / "bom.json"
    if not parts_dir.is_dir() or not bom_path.is_file():
        return 0
    try:
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    # Material → CAM material code mapping
    cnc_materials = {
        "cfrp":           "carbon_fibre",
        "carbon_fiber":   "carbon_fibre",
        "aluminum_6061":  "aluminium_6061",
        "aluminum_7075":  "aluminium_6061",  # close enough for feeds/speeds
        "aluminum":       "aluminium_6061",
        "steel":          "steel_4140",
        "stainless_steel":"steel_4140",
        "titanium":       "titanium_6al4v",
    }

    parts_meta = {p["spec"]: p for p in (bom.get("parts") or [])
                  if isinstance(p, dict) and "spec" in p}
    for p in (bom.get("parts") or []):
        if isinstance(p, dict) and "name" in p:
            parts_meta.setdefault(p["name"], p)

    try:
        from aria_os.cam.cam_generator import generate_cam_script
    except Exception as exc:
        print(f"[cam] module unavailable: {exc}")
        return 0

    n = 0
    for step_file in sorted(parts_dir.glob("*.step")):
        name = step_file.stem
        meta = parts_meta.get(name) or {}
        material = (meta.get("material") or "").lower()
        cam_mat = cnc_materials.get(material)
        if not cam_mat:
            continue   # not a CNC part (printed or purchased)
        try:
            sub_dir = cam_dir / name
            sub_dir.mkdir(parents=True, exist_ok=True)
            generate_cam_script(step_file, material=cam_mat, out_dir=sub_dir)
            n += 1
            print(f"[cam] generated {name}.py ({material})")
        except Exception as exc:
            print(f"[cam] {name} skipped: {type(exc).__name__}: {exc}")
    return n


def _build_preview_manifest(output_dir: Path, result: BuildResult) -> list[dict]:
    """Collect thumbnail-able artifacts for the UI 'What's in the box' tile.

    Returns a list of {label, type, path, rel_path} dicts. type is 'png' for
    renders or 'svg' for drawings. rel_path is suitable for /api/file?path=...
    """
    items: list[dict] = []
    # Paths must include the "outputs/" prefix: /api/file resolves REPO_ROOT / path
    # and only allows files under REPO_ROOT / outputs.
    _outputs_root = output_dir.resolve().parent.parent

    def _rel(p: Path) -> str:
        p = Path(p).resolve()
        try:
            rel = p.relative_to(_outputs_root)
            return f"outputs/{rel.as_posix()}"
        except ValueError:
            for anc in p.parents:
                if anc.name == "outputs":
                    rel = p.relative_to(anc)
                    return f"outputs/{rel.as_posix()}"
            return str(p).replace("\\", "/")

    # Main render
    if result.render_path and Path(result.render_path).is_file():
        items.append({
            "label": "Assembly render", "type": "png",
            "path": result.render_path,
            "rel_path": _rel(Path(result.render_path)),
        })
    # Drawings
    if result.drawings_dir:
        for svg in sorted(Path(result.drawings_dir).glob("*.svg")):
            items.append({
                "label": svg.stem.replace("_", " ").title(), "type": "svg",
                "path": str(svg),
                "rel_path": _rel(svg),
            })
    # Closeups (if generated separately)
    closeups = output_dir / "closeups"
    if closeups.is_dir():
        for png in sorted(closeups.glob("*.png")):
            items.append({
                "label": "Closeup: " + png.stem.replace("_closeup", "").replace("_", " "),
                "type": "png",
                "path": str(png),
                "rel_path": _rel(png),
            })
    # DIY fab artifacts — copper-tape SVG is the visually-interesting preview;
    # G-code and STLs are downloadable but not thumbnailable.
    if isinstance(result.diy_fab, dict):
        for board, info in result.diy_fab.items():
            if not isinstance(info, dict):
                continue
            svg = (info.get("paths") or {}).get("copper_tape_svg")
            if svg and Path(svg).is_file():
                items.append({
                    "label": f"{board} copper tape",
                    "type": "svg",
                    "path": svg,
                    "rel_path": _rel(Path(svg)),
                })
    return items


def _write_summary(result: BuildResult) -> None:
    """Write build_summary.json so /api/preset/run/{id} can return it."""
    out = Path(result.output_dir) / "build_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Cross-project bridges: StructSight (judgment) + MillForge (handoff)
# ---------------------------------------------------------------------------

_PRESET_GOAL_TEMPLATES = {
    "5inch_fpv": (
        "5-inch racing FPV quadcopter frame with CFRP arms and aluminum "
        "standoffs. Target AUW ~400g, 6S LiPo, 2306 motors."
    ),
    "7inch_long_range": (
        "7-inch long-range quadcopter frame, CFRP arms, aluminum standoffs. "
        "AUW ~700g, 6S LiPo, 2807 motors, 178mm prop."
    ),
    "military_recon": (
        "Military recon 7-inch quadcopter frame, hardened for ruggedized "
        "ISR payloads. CFRP primary structure, aluminum brackets, redundant power."
    ),
}


def _preset_goal(preset_id: str, params: dict | None) -> str:
    """Return a natural-language description of the preset for StructSight."""
    base = _PRESET_GOAL_TEMPLATES.get(
        preset_id,
        f"Engineering build for preset '{preset_id}'."
    )
    if params and isinstance(params, dict) and params.get("notes"):
        base = f"{base} Notes: {params['notes']}"
    return base


def _has_structsight() -> bool:
    """Detect structsight availability without importing at module load time.

    structsight is an optional sibling package (pip install -e ../structsight).
    Railway can't install from a local sibling path, so the build must stay
    functional when structsight is absent.
    """
    try:
        import structsight  # noqa: F401
        return True
    except Exception:
        return False


def _run_structsight(goal: str) -> dict:
    """Call structsight.analyze() and normalize to a JSON-safe dict.

    Returns {"available": False, "error": ...} if the package is missing or
    analyze() crashes. Never raises.
    """
    if not _has_structsight():
        return {"available": False, "error": "structsight not installed"}
    try:
        import structsight
        r = structsight.analyze(goal)
        if hasattr(r, "model_dump"):
            payload = r.model_dump()
        elif hasattr(r, "dict"):
            payload = r.dict()
        else:
            payload = {"repr": repr(r)}
        payload["available"] = True
        return payload
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


# MillForge Railway backend — /api/aria/bundle accepts ARIABundleSubmission
# (see millforge-ai/backend/routers/aria_bridge.py).
MILLFORGE_BUNDLE_URL = "https://millforge-ai-production.up.railway.app/api/aria/bundle"
_MILLFORGE_TIMEOUT_S = 10.0


def _post_millforge_bundle(
    preset_id: str,
    output_dir: Path,
    result: BuildResult,
) -> dict:
    """POST bundle metadata to MillForge. Always returns a dict — never raises.

    Success  -> {"available": True, "millforge_job_id": ..., "work_order_id": ...,
                 "quote_url": ..., "scheduled_at": ..., "status": ...}
    Failure  -> {"available": False, "error": ...}

    MillForge's /api/aria/bundle requires run_id + goal + part_name. It returns
    {aria_run_id, millforge_job_id, status, duplicate, received_at, next_step}.
    We surface those plus task-requested aliases (work_order_id, quote_url,
    scheduled_at) for a stable downstream contract.
    """
    try:
        import httpx
    except Exception as exc:
        return {"available": False, "error": f"httpx not available: {exc}"}

    # BOM-derived fields: parts_list, materials, mass_g
    parts_list: list[dict] = []
    materials: list[str] = []
    mass_g: float = 0.0
    if result.bom_path:
        try:
            bp = Path(result.bom_path)
            if bp.is_file():
                bom = json.loads(bp.read_text(encoding="utf-8"))
                raw_parts = bom.get("parts") or []
                mat_set: set[str] = set()
                for p in raw_parts:
                    if not isinstance(p, dict):
                        continue
                    parts_list.append({
                        "name": p.get("name") or p.get("spec") or "",
                        "material": p.get("material"),
                        "validation": p.get("validation"),
                    })
                    m = p.get("material")
                    if m:
                        mat_set.add(m)
                    pm = p.get("mass_g") or (p.get("measured") or {}).get("mass_g")
                    if isinstance(pm, (int, float)):
                        mass_g += float(pm)
                materials = sorted(mat_set)
        except Exception as exc:
            print(f"[millforge] bom parse skipped: {type(exc).__name__}: {exc}")

    assembly_step_path = result.step_path

    # run_id = preset + epoch seconds. MillForge is idempotent on run_id.
    run_id = f"{preset_id}-{int(time.time())}"
    goal_text = _preset_goal(preset_id, None)

    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "goal": goal_text,
        "part_name": result.name or preset_id,
        "step_path": result.step_path,
        "stl_path": result.stl_path,
        "material": (materials[0] if materials else None),
        "priority": 5,
        "notes": f"ARIA preset build: {preset_id}",
        "structsight_context": result.structsight_judgment,
        "extra": {
            "preset_id": preset_id,
            "bundle_dir": str(output_dir),
            "mass_g": round(mass_g, 2) if mass_g else None,
            "parts_list": parts_list,
            "materials": materials,
            "assembly_step_path": assembly_step_path,
            "render_path": result.render_path,
            "drawings_dir": result.drawings_dir,
            "print_dir": result.print_dir,
            "cam_dir": result.cam_dir,
        },
    }

    try:
        resp = httpx.post(
            MILLFORGE_BUNDLE_URL,
            json=payload,
            timeout=_MILLFORGE_TIMEOUT_S,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}",
                "url": MILLFORGE_BUNDLE_URL}

    if resp.status_code != 200:
        return {
            "available": False,
            "error": f"HTTP {resp.status_code}",
            "body": resp.text[:500],
            "url": MILLFORGE_BUNDLE_URL,
        }

    try:
        body = resp.json()
    except Exception as exc:
        return {"available": False, "error": f"non-JSON response: {exc}",
                "body": resp.text[:500]}

    millforge_job_id = body.get("millforge_job_id")
    received_at = body.get("received_at")
    aria_run_id = body.get("aria_run_id", run_id)
    status_base = "https://millforge-ai-production.up.railway.app/api/bridge/status"
    return {
        "available": True,
        "millforge_job_id": millforge_job_id,
        "work_order_id": millforge_job_id,           # task-requested alias
        "status": body.get("status"),
        "duplicate": body.get("duplicate"),
        "scheduled_at": received_at,                 # task-requested alias
        "received_at": received_at,
        "aria_run_id": aria_run_id,
        "quote_url": f"{status_base}/{aria_run_id}",
        "next_step": body.get("next_step"),
    }
