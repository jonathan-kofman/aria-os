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
    print_success: bool = False
    cam_success: bool = False
    sim_success: bool = False        # Genesis flight dynamics
    circuit_sim_success: bool = False # PySpice analog circuit sim

    # Artifact paths (relative to repo root for transport)
    step_path: str | None = None
    stl_path: str | None = None
    render_path: str | None = None
    bom_path: str | None = None
    print_dir: str | None = None
    cam_dir: str | None = None
    drawings_dir: str | None = None
    sim_trace_path: str | None = None
    sim_summary: dict | None = None
    circuit_sim_summary: dict | None = None

    # Preview thumbnails (PNGs + SVGs) for the "what's in the box" UI tile
    preview_artifacts: list[dict] = field(default_factory=list)

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
                "print":      self.print_success,
                "cam":        self.cam_success,
                "sim":        self.sim_success,
                "circuit_sim": self.circuit_sim_success,
            },
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
            "preview_artifacts": self.preview_artifacts,
        }


def run_full_build(*, preset_id: str, params: dict | None = None) -> BuildResult:
    """Run the complete build for a preset. Returns a BuildResult with all
    artifact paths. Each stage is independent — failure in one doesn't abort
    the others (so you still get whatever did succeed)."""
    t0 = time.monotonic()

    # ── Stage 1: Mechanical assembly (+ ECAD + drawings inside the drone module) ──
    name, output_dir, mech_ok = _stage_mechanical(preset_id, params)
    result = BuildResult(preset_id=preset_id, name=name, output_dir=str(output_dir))
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
            drawings = dr.get("drawings") or {}
            result.drawings_dir = str(output_dir / "drawings") if drawings else None
            result.drawings_success = bool(drawings) and "error" not in drawings
        except Exception:
            pass

    # ── Stage 2: Print bundle (slicer-ready STLs + Elegoo config) ────────────
    try:
        from aria_os.slicer import prepare_for_print
        print_summary = prepare_for_print(output_dir)
        result.print_dir = print_summary.get("print_dir")
        result.print_success = print_summary.get("n_print_parts", 0) > 0
    except Exception as exc:
        result.print_success = False
        print(f"[build] print prep skipped: {type(exc).__name__}: {exc}")

    # ── Stage 3: CAM scripts (CNC mill toolpaths for CFRP/aluminum) ──────────
    cam_dir = output_dir / "cam"
    cam_dir.mkdir(parents=True, exist_ok=True)
    cam_count = _stage_cam(output_dir, cam_dir)
    result.cam_dir = str(cam_dir) if cam_count > 0 else None
    result.cam_success = cam_count > 0

    # ── Stage 4: Flight dynamics sim (Genesis if installed, else stub) ────────
    sim_dir = output_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    if result.stl_path and Path(result.stl_path).is_file():
        try:
            from aria_os.flight_sim import simulate_drone_hover
            sim_result = simulate_drone_hover(
                result.stl_path,
                # Heuristic: 5" race ~400g, 7" military ~700g
                mass_g=700.0 if "military" in preset_id or "7inch" in preset_id else 400.0,
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

    # ── Stage 5: Circuit / electronic sim per ECAD board ────────────────────
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

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(output_dir.parent.parent)).replace("\\", "/")
        except ValueError:
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
    return items


def _write_summary(result: BuildResult) -> None:
    """Write build_summary.json so /api/preset/run/{id} can return it."""
    out = Path(result.output_dir) / "build_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
