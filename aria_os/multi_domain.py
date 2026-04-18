"""
Multi-domain design workflow — outer MCAD + ECAD + inner enclosure + drawings + BOMs.

Composes existing ariaOS modules into a single workflow that, given a design
spec, produces:

  1. Outer mechanical CAD (housing / airframe / enclosure shell)
  2. PCB / ECAD output (KiCad project + pcbnew script + BOM)
  3. Inner mechanical enclosure sized to the PCB, positioned inside outer shape
  4. GD&T drawings for the two MCAD pieces
  5. Rendered PNG previews of every artifact
  6. HTML index page that shows everything together
  7. Two BOMs — mechanical (assembly_bom) + electronic (ECAD bom JSON)

Partial-success-allowed: if any single stage fails, others continue and the
final report records what's missing.

Entry point:

    from aria_os.multi_domain import run_multi_domain
    result = run_multi_domain(
        outer_spec="quadcopter airframe 50x50x20mm carbon plate",
        ecad_spec="STM32F405 flight controller with MPU6000 IMU and barometer, 36x36mm",
        enclosure_position="centered",
    )
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StageResult:
    """One workflow stage outcome."""
    name: str
    success: bool
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    elapsed_s: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiDomainResult:
    """Full workflow outcome."""
    name: str
    output_dir: str
    stages: list[StageResult] = field(default_factory=list)
    bom_mcad: dict[str, Any] | None = None
    bom_ecad: dict[str, Any] | None = None
    html_index: str | None = None
    elapsed_s: float = 0.0

    @property
    def success(self) -> bool:
        return all(s.success for s in self.stages)

    @property
    def partial_success(self) -> bool:
        return any(s.success for s in self.stages) and not self.success

    def stage(self, name: str) -> StageResult | None:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "output_dir": self.output_dir,
            "success": self.success,
            "partial_success": self.partial_success,
            "elapsed_s": round(self.elapsed_s, 2),
            "stages": [
                {"name": s.name, "success": s.success, "error": s.error,
                 "elapsed_s": round(s.elapsed_s, 2), "artifacts": s.artifacts}
                for s in self.stages
            ],
            "bom_mcad_summary": (self.bom_mcad or {}).get("summary"),
            "bom_ecad_total_components": (self.bom_ecad or {}).get("total_components"),
            "html_index": self.html_index,
        }


# ---------------------------------------------------------------------------
# Default demo prompt — drone flight controller stack
# ---------------------------------------------------------------------------

DEFAULT_DEMO_SPEC = {
    "name": "drone_fc_stack",
    "outer_spec": (
        "quadcopter flight controller airframe plate 50x50mm with 5mm thickness, "
        "carbon-fiber composite, with 4 M3 bolt holes at 30mm pitch corners "
        "for stack mounting"
    ),
    "ecad_spec": (
        "STM32F405 flight controller PCB 36x36mm with MPU6000 IMU, "
        "BMP280 barometer, JST-XH battery connector, USB-C, 4x ESC pads, "
        "4x M3 mounting holes at 30.5mm pitch"
    ),
    "enclosure_offset_mm": (0.0, 0.0, 8.0),  # PCB sits 8mm above outer plate
}


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------

def run_multi_domain(
    outer_spec: str | None = None,
    ecad_spec: str | None = None,
    *,
    name: str | None = None,
    enclosure_offset_mm: tuple[float, float, float] | None = None,
    output_dir: str | Path | None = None,
    repo_root: Path | None = None,
) -> MultiDomainResult:
    """Run the full MCAD + ECAD + enclosure + drawings + BOMs workflow.

    Falls back to the drone flight-controller demo spec when args omitted.
    Returns a MultiDomainResult — partial success is allowed (each stage is
    independent and reports its own outcome).
    """
    if not outer_spec or not ecad_spec:
        outer_spec = outer_spec or DEFAULT_DEMO_SPEC["outer_spec"]
        ecad_spec = ecad_spec or DEFAULT_DEMO_SPEC["ecad_spec"]
        name = name or DEFAULT_DEMO_SPEC["name"]
        if enclosure_offset_mm is None:
            enclosure_offset_mm = DEFAULT_DEMO_SPEC["enclosure_offset_mm"]

    name = name or _slug(outer_spec)
    enclosure_offset_mm = enclosure_offset_mm or (0.0, 0.0, 8.0)

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    if output_dir is None:
        output_dir = repo_root / "outputs" / "multi_domain" / name
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    result = MultiDomainResult(name=name, output_dir=str(output_dir))

    # Stage 1 — Outer MCAD
    result.stages.append(_stage_outer_mcad(outer_spec, output_dir, repo_root))

    # Stage 2 — ECAD (PCB)
    result.stages.append(_stage_ecad(ecad_spec, output_dir))

    # Stage 3 — Inner enclosure (depends on ECAD output OR spec fallback)
    ecad_stage = result.stage("ecad")
    pcb_path = (ecad_stage.artifacts.get("kicad_pcb")
                if ecad_stage and ecad_stage.success else None)
    result.stages.append(_stage_inner_enclosure(
        pcb_path=pcb_path, output_dir=output_dir,
        offset_mm=enclosure_offset_mm,
        ecad_spec=ecad_spec,
        ecad_artifacts=ecad_stage.artifacts if ecad_stage else None,
    ))

    # Stage 4 — Drawings (depends on outer + enclosure STEP files)
    outer_step = (result.stage("outer_mcad").artifacts.get("step_path")
                  if result.stage("outer_mcad") else None)
    enc_step = (result.stage("inner_enclosure").artifacts.get("step_path")
                if result.stage("inner_enclosure") else None)
    result.stages.append(_stage_drawings(
        outer_step, enc_step, output_dir, repo_root,
        outer_spec=outer_spec, ecad_spec=ecad_spec,
        ecad_artifacts=ecad_stage.artifacts if ecad_stage else None,
    ))

    # Stage 5 — Combined assembly (depends on outer + enclosure STEP files)
    result.stages.append(_stage_combined_assembly(
        outer_step=outer_step, enc_step=enc_step,
        offset_mm=enclosure_offset_mm, output_dir=output_dir, name=name,
    ))

    # Stage 6 — Renders (depends on STEP files — includes all enclosure halves)
    assembly_step = (result.stage("combined_assembly").artifacts.get("step_path")
                     if result.stage("combined_assembly") else None)
    enc_stage = result.stage("inner_enclosure")
    result.stages.append(_stage_renders(
        outer_step,
        enc_stage.artifacts if enc_stage else None,
        output_dir,
        assembly_step=assembly_step,
    ))

    # Stage 6 — BOM mechanical
    result.bom_mcad = _build_mcad_bom(name, outer_step, enc_step, ecad_spec)

    # Stage 7 — BOM electronic
    result.bom_ecad = _build_ecad_bom(ecad_stage.artifacts if ecad_stage else None)

    # Stage 8 — HTML index
    result.html_index = _build_html_index(result, output_dir)

    result.elapsed_s = time.monotonic() - t0
    # Save the full result JSON
    (output_dir / "multi_domain_result.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8",
    )
    return result


# ---------------------------------------------------------------------------
# Stage implementations — each catches its own exceptions
# ---------------------------------------------------------------------------

def _stage_outer_mcad(spec: str, out_dir: Path, repo_root: Path) -> StageResult:
    """Generate the outer MCAD.

    Two paths:
      1. **Template fast-path** — if the spec matches a known plate-with-bolt-holes
         pattern, call `_cq_flat_plate` directly with explicit params. This
         deterministically produces the requested holes (the LLM path was
         dropping them silently).
      2. **Pipeline fallback** — for shapes outside the template fast-path,
         call the full `aria_os.orchestrator.run()`. Bubbles up the inner
         VISUAL checkpoint result so feature-level failures (e.g. missing
         holes detected by the vision verifier) fail this stage.
    """
    t0 = time.monotonic()
    mcad_out = out_dir / "outer_mcad"
    mcad_out.mkdir(parents=True, exist_ok=True)

    # 1. Try the template fast-path
    template_result = _try_plate_template(spec, mcad_out)
    if template_result is not None:
        template_result.elapsed_s = time.monotonic() - t0
        return template_result

    # 2. Fall back to the full pipeline
    try:
        from aria_os.orchestrator import run as _run_pipeline
        session = _run_pipeline(spec, repo_root=repo_root, agent_mode=False)
        step_path = session.get("step_path") or ""
        stl_path = session.get("stl_path") or ""
        if not step_path or not Path(step_path).is_file():
            return StageResult(
                name="outer_mcad", success=False,
                error="No STEP file produced by outer MCAD pipeline",
                elapsed_s=time.monotonic() - t0,
                details={"session_keys": list(session.keys())[:20]},
            )

        # Bubble up inner VISUAL checkpoint — fail this stage if vision verifier failed
        cps = session.get("checkpoints", {}) or {}
        vis = cps.get("VISUAL", {}) or {}
        vis_passed = vis.get("passed", True)
        vis_failures = vis.get("failures", []) or []

        return StageResult(
            name="outer_mcad",
            success=vis_passed,
            artifacts={"step_path": step_path, "stl_path": stl_path},
            error=("VISUAL: " + "; ".join(vis_failures[:2])) if not vis_passed else None,
            elapsed_s=time.monotonic() - t0,
            details={
                "bbox": session.get("bbox", {}),
                "visual_checkpoint": vis,
                "source": "pipeline",
            },
        )
    except Exception as exc:
        return StageResult(
            name="outer_mcad", success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


def _try_plate_template(spec: str, out_dir: Path) -> StageResult | None:
    """If the spec describes a plate with bolt holes, generate it directly via
    the `_cq_flat_plate` template. Returns None if the spec doesn't match.

    Pattern matched: "WxHmm with N M<dia> bolt holes [at <pitch>mm pitch corners]"
    plus a thickness somewhere. Generates a deterministic STEP+STL with the
    holes in place — no LLM in the loop.
    """
    import re
    from aria_os.spec_extractor import extract_spec
    from aria_os.generators.cadquery_generator import _cq_flat_plate

    spec_lower = spec.lower()
    # Heuristic gate: must mention "plate" or "panel" or "airframe" + bolt holes
    if not any(w in spec_lower for w in ("plate", "panel", "airframe", "baseplate")):
        return None
    if "bolt hole" not in spec_lower and " holes" not in spec_lower:
        return None

    parsed = extract_spec(spec)
    width = parsed.get("width_mm") or parsed.get("depth_mm")
    if not width:
        return None
    depth = parsed.get("depth_mm") or width
    thickness = (parsed.get("thickness_mm")
                 or parsed.get("height_mm")
                 or 5.0)
    n_bolts = parsed.get("n_bolts", 4)
    bolt_dia = parsed.get("bolt_dia_mm", 3.0)

    # Parse "at <pitch>mm pitch" for square corner pattern
    bolt_square = parsed.get("bolt_square_mm")
    if bolt_square is None:
        m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*pitch", spec, re.I)
        if m and n_bolts == 4:
            bolt_square = float(m.group(1))

    params = {
        "width_mm": width,
        "depth_mm": depth,
        "thickness_mm": thickness,
        "n_bolts": n_bolts,
        "bolt_dia_mm": bolt_dia,
    }
    if bolt_square:
        params["bolt_square_mm"] = bolt_square

    code = _cq_flat_plate(params)
    step_path = out_dir / "outer_plate.step"
    stl_path = out_dir / "outer_plate.stl"

    # Append export to the generated code
    code += f"""
import cadquery as _cq_export
_cq_export.exporters.export(result, r"{step_path}", "STEP")
_cq_export.exporters.export(result, r"{stl_path}", "STL")
"""
    try:
        ns: dict[str, Any] = {}
        exec(code, ns)
    except Exception as exc:
        return StageResult(
            name="outer_mcad", success=False,
            error=f"plate template execution failed: {type(exc).__name__}: {exc}",
            details={"params": params, "source": "template"},
        )

    if not step_path.is_file():
        return StageResult(
            name="outer_mcad", success=False,
            error="plate template ran but no STEP exported",
            details={"params": params, "source": "template"},
        )

    # Verify with Euler characteristic — N through-holes => Euler = 2 - 2N
    import trimesh
    n_holes_actual = -1
    try:
        mesh = trimesh.load(str(stl_path))
        # Euler char = 2*(1 - genus); genus = number of through-holes
        euler = mesh.euler_number
        n_holes_actual = (2 - euler) // 2
    except Exception:
        pass

    success = (n_holes_actual == n_bolts)
    err = None if success else (
        f"plate has {n_holes_actual} through-holes, expected {n_bolts} "
        "(template generated geometry but hole count is wrong)"
    )
    return StageResult(
        name="outer_mcad",
        success=success,
        artifacts={"step_path": str(step_path), "stl_path": str(stl_path)},
        error=err,
        details={
            "source": "template",
            "params": params,
            "n_holes_expected": n_bolts,
            "n_holes_actual": n_holes_actual,
        },
    )


def _stage_ecad(spec: str, out_dir: Path) -> StageResult:
    """Generate the PCB + spec-adherence + ERC/DRC validation surfacing.

    Stage fails (success=False) when:
      - ECAD generation throws
      - Validation reports ANY ERC errors (critical — missing power, no decoupling)
      - DRC reports >=2 component overlaps (genuine layout problems, not a single
        edge clearance miss)
      - Spec-adherence shows zero matched components

    Spec drift alone is a warning — board may still be useful.
    """
    t0 = time.monotonic()
    try:
        from aria_os.ecad.ecad_generator import generate_ecad
        ecad_out = out_dir / "ecad"
        ecad_out.mkdir(parents=True, exist_ok=True)
        script_path, bom_path = generate_ecad(spec, out_dir=ecad_out)

        kicad_pcb = next(iter(ecad_out.rglob("*.kicad_pcb")), None)

        artifacts = {
            "pcbnew_script": str(script_path),
            "bom_json": str(bom_path),
        }
        if kicad_pcb:
            artifacts["kicad_pcb"] = str(kicad_pcb)

        # Read validation.json that the ECAD generator writes
        validation_path = next(iter(Path(script_path).parent.rglob("validation.json")), None)
        validation: dict[str, Any] = {}
        if validation_path and validation_path.is_file():
            try:
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
                artifacts["validation_json"] = str(validation_path)
            except Exception:
                pass

        erc_errors = (validation.get("erc", {}) or {}).get("errors", []) or []
        drc_violations = (validation.get("drc", {}) or {}).get("violations", []) or []
        n_overlaps = sum(1 for v in drc_violations if "overlap" in str(v).lower())
        warnings_list = (validation.get("warnings") or []) + (
            (validation.get("erc", {}) or {}).get("warnings", []) or []
        )

        # Spec-adherence
        adherence = _check_ecad_spec_adherence(spec, bom_path)

        # Decide success
        problems: list[str] = []
        if erc_errors:
            problems.append(f"ERC: {len(erc_errors)} error(s)")
        if n_overlaps >= 2:
            problems.append(f"DRC: {n_overlaps} component overlap(s)")
        if adherence["missing"]:
            n_miss = len(adherence["missing"])
            if adherence["matched"] == 0 and len(adherence["requested"]) > 0:
                problems.append(
                    f"Spec drift: 0/{len(adherence['requested'])} requested components in BOM "
                    f"(missing: {', '.join(adherence['missing'][:3])}"
                    f"{'...' if n_miss > 3 else ''})"
                )

        success = len(problems) == 0

        # Build error/warning text
        msg_parts: list[str] = []
        if problems:
            msg_parts.extend(problems)
        else:
            # Even on success, surface drift + warnings so user sees them
            if adherence["missing"]:
                msg_parts.append(
                    f"Spec drift (warning): {len(adherence['missing'])} not in BOM: "
                    f"{', '.join(adherence['missing'][:3])}"
                )
        # First few specific errors for the human
        if erc_errors:
            msg_parts.append(f"ERC: {erc_errors[0][:120]}")
        if n_overlaps:
            sample = next((v for v in drc_violations if "overlap" in str(v).lower()), "")
            msg_parts.append(f"DRC: {sample[:120]}")
        msg = " | ".join(msg_parts) if msg_parts else None

        return StageResult(
            name="ecad",
            success=success,
            artifacts=artifacts,
            error=msg,
            elapsed_s=time.monotonic() - t0,
            details={
                "spec_adherence": adherence,
                "erc_errors_count": len(erc_errors),
                "drc_violations_count": len(drc_violations),
                "drc_overlaps_count": n_overlaps,
                "warnings_count": len(warnings_list),
                "erc_errors": erc_errors[:5],
                "drc_violations": drc_violations[:5],
                "warnings": warnings_list[:5],
            },
        )
    except Exception as exc:
        return StageResult(
            name="ecad", success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


def _check_ecad_spec_adherence(
    spec: str, bom_path: str | Path,
) -> dict[str, Any]:
    """Compare requested components in the spec against the actual BOM.

    Returns {requested, matched, missing, found} — missing components are the
    ones the user mentioned that aren't anywhere in the BOM (under any of:
    component value, ref, or description).
    """
    import re
    result: dict[str, Any] = {
        "requested": [], "matched": 0, "missing": [], "found": [],
    }

    # Extract requested component IDs from the spec text.
    # Patterns: STM32F405, MPU6000, BMP280, TP4056, USB-C, JST-XH, ESP32, etc.
    # Also catch "MAX31855", "ATmega328P", "RP2040", "BME280" etc.
    patterns = [
        r"\b(STM32[A-Z0-9]+)\b",          # STM32F405, STM32F411
        r"\b(ESP32(?:-[A-Z0-9]+)?)\b",   # ESP32, ESP32-S3
        r"\b(ATmega\d+[A-Z]*)\b",        # ATmega328P
        r"\b(MPU\d{4})\b",                # MPU6000, MPU9250
        r"\b(BMP\d{3})\b",                # BMP280
        r"\b(BME\d{3})\b",                # BME280
        r"\b(MAX\d{4,5}[A-Z]*)\b",        # MAX31855
        r"\b(RP\d{4})\b",                  # RP2040
        r"\b(TP\d{4})\b",                  # TP4056
        r"\b(JST-?[A-Z]{2,3})\b",         # JST-XH, JST-PH
        r"\b(USB-?C)\b",                   # USB-C
        r"\b(USB-?A)\b",                   # USB-A
        r"\b(SD-?card|microSD)\b",        # SD card
        r"\b(barometer)\b",                # generic ask
        r"\b(IMU)\b",                      # generic ask
        r"\b(OLED|LCD|TFT)\b",            # displays
    ]
    requested: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, spec, re.I):
            tok = m.group(1).upper().replace("-", "")
            if tok not in seen:
                seen.add(tok)
                requested.append(m.group(1))
    result["requested"] = requested
    if not requested:
        return result

    # Load the BOM
    try:
        data = json.loads(Path(bom_path).read_text(encoding="utf-8"))
    except Exception:
        result["missing"] = list(requested)
        return result

    components = data.get("components", []) if isinstance(data, dict) else (
        data if isinstance(data, list) else []
    )
    haystack = " ".join(
        (str(c.get("value", "")) + " " + str(c.get("ref", ""))
         + " " + str(c.get("description", "")))
        for c in components
    ).upper().replace("-", "")

    found: list[str] = []
    missing: list[str] = []
    for r in requested:
        norm = r.upper().replace("-", "")
        # Generic asks count as matched if the BOM has at least one component
        # with a related description keyword
        if norm in ("BAROMETER", "IMU", "OLED", "LCD", "TFT"):
            generic_keywords = {
                "BAROMETER": ("BAROMETER", "BMP", "BME"),
                "IMU": ("IMU", "MPU", "ICM", "BMI", "ACCEL", "GYRO"),
                "OLED": ("OLED",), "LCD": ("LCD",), "TFT": ("TFT",),
            }[norm]
            if any(kw in haystack for kw in generic_keywords):
                found.append(r)
                continue
            missing.append(r)
            continue
        if norm in haystack:
            found.append(r)
        else:
            missing.append(r)

    result["matched"] = len(found)
    result["found"] = found
    result["missing"] = missing
    return result


def _stage_inner_enclosure(
    pcb_path: str | None, output_dir: Path,
    offset_mm: tuple[float, float, float],
    ecad_spec: str = "",
    ecad_artifacts: dict[str, str] | None = None,
) -> StageResult:
    """Generate the PCB enclosure.

    Two paths:
      1. If a real .kicad_pcb file exists, use generate_enclosure_from_pcb()
      2. Otherwise (the common case — pcbnew not installed), build PCBGeometry
         directly from the ECAD spec text + bom JSON, then run the script
    """
    t0 = time.monotonic()
    enc_out = output_dir / "enclosure"
    enc_out.mkdir(parents=True, exist_ok=True)

    try:
        from aria_os.ecad.ecad_to_enclosure import (
            generate_enclosure_from_pcb,
            generate_enclosure_script,
            PCBGeometry,
            EnclosureOptions,
        )
    except ImportError as exc:
        return StageResult(
            name="inner_enclosure", success=False,
            error=f"ecad_to_enclosure import failed: {exc}",
            elapsed_s=time.monotonic() - t0,
        )

    # Try the real-PCB path first
    result = None
    if pcb_path and Path(pcb_path).is_file():
        try:
            result = generate_enclosure_from_pcb(pcb_path, str(enc_out))
            if result and getattr(result, "error", None):
                result = None  # fall back to spec-driven path
        except Exception:
            result = None

    # Fallback: build directly from spec
    if result is None:
        try:
            board_w, board_h = _extract_board_dims_from_spec(
                ecad_spec, ecad_artifacts or {},
            )
            pcb_geom = PCBGeometry(
                board_width_mm=board_w,
                board_height_mm=board_h,
                board_thickness_mm=1.6,
                mounting_holes=[],
                connectors=[],
            )
            options = EnclosureOptions()
            script = generate_enclosure_script(pcb_geom, options)
            (enc_out / "enclosure_cq.py").write_text(script, encoding="utf-8")

            step_paths: dict[str, str] = {}
            stl_paths: dict[str, str] = {}
            try:
                import cadquery as cq
                ns: dict[str, Any] = {"cq": cq}
                exec(script, ns)
                for label in ("bottom", "top"):
                    obj = ns.get(label) or (ns.get("result") if label == "bottom" else None)
                    if obj is None:
                        continue
                    step_p = enc_out / f"enclosure_{label}.step"
                    stl_p = enc_out / f"enclosure_{label}.stl"
                    cq.exporters.export(obj, str(step_p))
                    cq.exporters.export(obj, str(stl_p))
                    step_paths[label] = str(step_p)
                    stl_paths[label] = str(stl_p)
            except Exception as exc:
                return StageResult(
                    name="inner_enclosure", success=False,
                    error=f"enclosure script execution failed: "
                          f"{type(exc).__name__}: {exc}",
                    elapsed_s=time.monotonic() - t0,
                )

            class _SpecDerivedResult:
                pass
            result = _SpecDerivedResult()
            result.step_paths = step_paths
            result.stl_paths = stl_paths
        except Exception as exc:
            return StageResult(
                name="inner_enclosure", success=False,
                error=f"spec-derived enclosure failed: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - t0,
            )

    # Walk result for STEP/STL paths — generate_enclosure_from_pcb's
    # return shape varies; be tolerant
    try:
        artifacts: dict[str, str] = {}
        step_attr = getattr(result, "step_paths", None) or {}
        if isinstance(step_attr, dict):
            for label, p in step_attr.items():
                if Path(p).is_file():
                    artifacts[f"step_{label}"] = str(p)
                    if "step_path" not in artifacts:
                        artifacts["step_path"] = str(p)
        stl_attr = getattr(result, "stl_paths", None) or {}
        if isinstance(stl_attr, dict):
            for label, p in stl_attr.items():
                if Path(p).is_file():
                    artifacts[f"stl_{label}"] = str(p)
                    if "stl_path" not in artifacts:
                        artifacts["stl_path"] = str(p)

        # Fallback: scan the output dir for any STEP/STL
        if "step_path" not in artifacts:
            for p in enc_out.rglob("*.step"):
                artifacts["step_path"] = str(p)
                break
        if "stl_path" not in artifacts:
            for p in enc_out.rglob("*.stl"):
                artifacts["stl_path"] = str(p)
                break

        if not artifacts.get("step_path"):
            return StageResult(
                name="inner_enclosure", success=False,
                error="Enclosure generated but no STEP file found in output",
                elapsed_s=time.monotonic() - t0,
                details={"result_attrs": dir(result)[:30] if result else []},
            )
        return StageResult(
            name="inner_enclosure", success=True, artifacts=artifacts,
            elapsed_s=time.monotonic() - t0,
            details={"offset_mm": list(offset_mm)},
        )
    except Exception as exc:
        return StageResult(
            name="inner_enclosure", success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


def _stage_drawings(
    outer_step: str | None, enc_step: str | None,
    output_dir: Path, repo_root: Path,
    *,
    outer_spec: str = "",
    ecad_spec: str = "",
    ecad_artifacts: dict[str, str] | None = None,
) -> StageResult:
    """Generate GD&T drawings (SVG) for both mechanical pieces.

    Passes spec-derived params (n_bolts, bolt_dia, dimensions) into
    drawing_generator so the GD&T callouts dimension actual features
    instead of just emitting generic flatness/perpendicularity.
    """
    t0 = time.monotonic()
    artifacts: dict[str, str] = {}
    errors: list[str] = []
    drawings_out = output_dir / "drawings"
    drawings_out.mkdir(parents=True, exist_ok=True)

    # Extract feature params from the outer spec — n_bolts, bolt_dia, dims
    try:
        from aria_os.spec_extractor import extract_spec
        outer_params = extract_spec(outer_spec) if outer_spec else {}
    except Exception:
        outer_params = {}

    # Build enclosure params from PCB dims + standard mounting pattern
    enc_params: dict[str, Any] = {}
    if ecad_spec:
        try:
            board_w, board_h = _extract_board_dims_from_spec(
                ecad_spec, ecad_artifacts or {},
            )
            enc_params = {
                "width_mm": board_w + 6.0,   # +clearance + walls
                "depth_mm": board_h + 6.0,
                "n_bolts": 4,                # 4 corner standoffs
                "bolt_dia_mm": 3.0,
                "bolt_circle_r_mm": ((board_w / 2) ** 2 + (board_h / 2) ** 2) ** 0.5,
            }
        except Exception:
            pass

    try:
        from aria_os.drawing_generator import generate_gdnt_drawing
        if outer_step and Path(outer_step).is_file():
            try:
                p = generate_gdnt_drawing(
                    Path(outer_step), part_id="outer_mcad",
                    params=outer_params, repo_root=repo_root,
                )
                target = drawings_out / "outer_mcad.svg"
                if Path(p).is_file():
                    target.write_bytes(Path(p).read_bytes())
                    artifacts["outer_drawing"] = str(target)
            except Exception as exc:
                errors.append(f"outer drawing: {exc}")

        if enc_step and Path(enc_step).is_file():
            try:
                p = generate_gdnt_drawing(
                    Path(enc_step), part_id="inner_enclosure",
                    params=enc_params, repo_root=repo_root,
                )
                target = drawings_out / "inner_enclosure.svg"
                if Path(p).is_file():
                    target.write_bytes(Path(p).read_bytes())
                    artifacts["enclosure_drawing"] = str(target)
            except Exception as exc:
                errors.append(f"enclosure drawing: {exc}")
    except ImportError as exc:
        errors.append(f"drawing module unavailable: {exc}")

    success = len(artifacts) > 0
    return StageResult(
        name="drawings", success=success,
        artifacts=artifacts,
        error="; ".join(errors) if errors else None,
        elapsed_s=time.monotonic() - t0,
    )


def _stage_combined_assembly(
    outer_step: str | None,
    enc_step: str | None,
    offset_mm: tuple[float, float, float],
    output_dir: Path,
    name: str,
) -> StageResult:
    """Combine outer MCAD + inner enclosure into one STEP/STL assembly.

    Uses ariaOS's Assembler — keeps both parts as named instances at their
    relative positions. Without this, the "specific place to put it" in the
    workflow spec is unmet (parts existed but were never joined).
    """
    t0 = time.monotonic()
    if not outer_step or not Path(outer_step).is_file():
        return StageResult(
            name="combined_assembly", success=False,
            error="outer MCAD not available",
            elapsed_s=time.monotonic() - t0,
        )
    if not enc_step or not Path(enc_step).is_file():
        return StageResult(
            name="combined_assembly", success=False,
            error="inner enclosure not available",
            elapsed_s=time.monotonic() - t0,
        )

    asm_out = output_dir / "assembly"
    asm_out.mkdir(parents=True, exist_ok=True)

    try:
        import cadquery as cq
        from cadquery import Assembly

        outer_shape = cq.importers.importStep(outer_step)
        enc_shape = cq.importers.importStep(enc_step)

        assy = Assembly(name=name)
        # Outer at origin
        assy.add(outer_shape, name="outer_mcad",
                 loc=cq.Location(cq.Vector(0, 0, 0)))
        # Enclosure at the requested offset (default: above the plate)
        assy.add(enc_shape, name="inner_enclosure",
                 loc=cq.Location(cq.Vector(*offset_mm)))

        step_path = asm_out / f"{name}_assembly.step"
        stl_path = asm_out / f"{name}_assembly.stl"
        assy.export(str(step_path), exportType="STEP")
        assy.export(str(stl_path), exportType="STL")

        return StageResult(
            name="combined_assembly", success=True,
            artifacts={
                "step_path": str(step_path),
                "stl_path": str(stl_path),
            },
            elapsed_s=time.monotonic() - t0,
            details={
                "outer_position": [0, 0, 0],
                "enclosure_position": list(offset_mm),
                "n_parts": 2,
            },
        )
    except Exception as exc:
        return StageResult(
            name="combined_assembly", success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


def _stage_renders(
    outer_step: str | None,
    enc_artifacts: dict[str, str] | None,
    output_dir: Path,
    *,
    assembly_step: str | None = None,
) -> StageResult:
    """Render PNG previews of every STEP file from every stage.

    Iterates over all step_* keys in the enclosure stage's artifacts (so both
    bottom and top halves get rendered) plus the outer plate and combined
    assembly.
    """
    t0 = time.monotonic()
    artifacts: dict[str, str] = {}
    errors: list[str] = []
    renders_out = output_dir / "renders"
    renders_out.mkdir(parents=True, exist_ok=True)

    targets: list[tuple[str, str]] = []
    if outer_step:
        targets.append(("outer", outer_step))

    # Render every STEP from the enclosure stage — bottom AND top, not just first
    if enc_artifacts:
        for key, p in enc_artifacts.items():
            if not p or not Path(p).is_file():
                continue
            if key == "step_path":
                continue  # avoid duplicate of step_bottom
            if key.startswith("step_"):
                label = "enclosure_" + key[len("step_"):]
                targets.append((label, p))
        # Always include the primary step_path if no step_* was found
        if not any(t[0].startswith("enclosure") for t in targets):
            primary = enc_artifacts.get("step_path")
            if primary and Path(primary).is_file():
                targets.append(("enclosure", primary))

    if assembly_step:
        targets.append(("assembly", assembly_step))

    for label, step_path in targets:
        if not step_path or not Path(step_path).is_file():
            continue
        try:
            stl_candidate = step_path.replace(".step", ".stl")
            if not Path(stl_candidate).is_file():
                # Convert STEP -> STL via cadquery
                try:
                    import cadquery as cq
                    shape = cq.importers.importStep(step_path)
                    cq.exporters.export(shape, stl_candidate, exportType="STL")
                except Exception as exc:
                    errors.append(f"{label} STL convert: {exc}")
                    continue
            from aria_os.visual_verifier import _render_views
            paths, _labels = _render_views(stl_candidate, label, renders_out)
            for i, p in enumerate(paths):
                target = renders_out / f"{label}_{i}.png"
                if Path(p).is_file() and str(p) != str(target):
                    target.write_bytes(Path(p).read_bytes())
                    artifacts[f"{label}_view_{i}"] = str(target)
                elif Path(p).is_file():
                    artifacts[f"{label}_view_{i}"] = str(p)
        except Exception as exc:
            errors.append(f"{label} render: {exc}")

    success = len(artifacts) > 0
    return StageResult(
        name="renders", success=success, artifacts=artifacts,
        error="; ".join(errors) if errors else None,
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# BOM construction
# ---------------------------------------------------------------------------

def _build_mcad_bom(
    name: str, outer_step: str | None, enc_step: str | None, ecad_spec: str,
) -> dict[str, Any]:
    """Build the mechanical BOM — outer MCAD + enclosure + standard fasteners."""
    try:
        from aria_os.assembly_bom import generate_bom
    except ImportError:
        return {"error": "assembly_bom module unavailable"}

    parts: list[dict[str, Any]] = []
    if outer_step:
        parts.append({"id": "outer_mcad", "step": outer_step})
    if enc_step:
        parts.append({"id": "inner_enclosure", "step": enc_step})

    # Add standard mounting fasteners — guess M3 from the demo spec, can be
    # overridden by the caller in a future refinement
    for i in range(4):
        parts.append({"id": f"mount_bolt_{i}", "component": "M3x16_12.9"})
    for i in range(4):
        parts.append({"id": f"mount_nut_{i}", "component": "M3_hex_nut_8"})

    config = {"name": name, "parts": parts}
    try:
        return generate_bom(config)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _build_ecad_bom(ecad_artifacts: dict[str, str] | None) -> dict[str, Any]:
    """Read the ECAD BOM JSON produced by ecad_generator."""
    if not ecad_artifacts:
        return {"error": "no ECAD stage outputs", "total_components": 0}
    bom_path = ecad_artifacts.get("bom_json")
    if not bom_path or not Path(bom_path).is_file():
        return {"error": "ECAD BOM JSON not found", "total_components": 0}
    try:
        bom = json.loads(Path(bom_path).read_text(encoding="utf-8"))
        # ECAD bom shape: {"components": [...], "summary": {...}} or list
        if isinstance(bom, dict):
            total = len(bom.get("components", []))
            bom["total_components"] = total
            return bom
        elif isinstance(bom, list):
            return {"components": bom, "total_components": len(bom)}
        else:
            return {"error": f"unexpected ECAD BOM shape: {type(bom).__name__}",
                    "total_components": 0}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "total_components": 0}


# ---------------------------------------------------------------------------
# HTML index page
# ---------------------------------------------------------------------------

def _build_html_index(result: MultiDomainResult, output_dir: Path) -> str:
    """Generate an HTML page that previews every artifact."""
    out = output_dir / "index.html"

    def _section(title: str, body: str) -> str:
        return f'<section><h2>{title}</h2>{body}</section>'

    def _artifact_link(label: str, path: str) -> str:
        try:
            rel = Path(path).resolve().relative_to(output_dir.resolve())
        except ValueError:
            rel = Path(path).name
        return f'<li><a href="{rel}">{label}</a> <code>{rel}</code></li>'

    def _img(path: str, alt: str) -> str:
        try:
            rel = Path(path).resolve().relative_to(output_dir.resolve())
        except ValueError:
            rel = Path(path).name
        return f'<figure><img src="{rel}" alt="{alt}"><figcaption>{alt}</figcaption></figure>'

    stages_html = "<ol>"
    for s in result.stages:
        status = "✅" if s.success else "❌"
        err = f' — <span class="err">{s.error}</span>' if s.error else ""
        stages_html += f'<li>{status} <strong>{s.name}</strong> ({s.elapsed_s:.1f}s){err}</li>'
    stages_html += "</ol>"

    renders_stage = result.stage("renders")
    renders_html = ""
    if renders_stage and renders_stage.artifacts:
        renders_html = "<div class='gallery'>" + "".join(
            _img(p, label) for label, p in renders_stage.artifacts.items()
        ) + "</div>"
    else:
        renders_html = "<p class='warn'>No renders produced.</p>"

    drawings_stage = result.stage("drawings")
    drawings_html = ""
    if drawings_stage and drawings_stage.artifacts:
        drawings_html = "<ul>" + "".join(
            _artifact_link(label, p) for label, p in drawings_stage.artifacts.items()
        ) + "</ul>"
    else:
        drawings_html = "<p class='warn'>No drawings produced.</p>"

    mcad_bom = result.bom_mcad or {}
    mcad_summary = mcad_bom.get("summary", {})
    mcad_html = (
        f"<p>{mcad_summary.get('total_parts', 0)} parts total — "
        f"{mcad_summary.get('fabricated_count', 0)} fabricated, "
        f"{mcad_summary.get('purchased_count', 0)} purchased "
        f"(${mcad_summary.get('total_purchased_cost_usd', 0):.2f}, "
        f"{mcad_summary.get('total_mass_g', 0):.1f}g)</p>"
        if mcad_summary else "<p class='warn'>MCAD BOM unavailable.</p>"
    )

    ecad_bom = result.bom_ecad or {}
    ecad_html = (
        f"<p>{ecad_bom.get('total_components', 0)} electronic components</p>"
        if ecad_bom and not ecad_bom.get("error")
        else f"<p class='warn'>{ecad_bom.get('error', 'ECAD BOM unavailable')}</p>"
    )

    css = """
      body { font-family: -apple-system, system-ui, sans-serif; max-width: 1200px;
             margin: 2rem auto; padding: 0 1rem; color: #222; line-height: 1.5; }
      h1 { font-size: 1.6rem; }
      h2 { font-size: 1.2rem; border-bottom: 2px solid #333; padding-bottom: 0.3rem;
           margin-top: 2.5rem; }
      .gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                 gap: 1rem; }
      figure { margin: 0; border: 1px solid #ddd; padding: 0.5rem;
               background: #fafafa; }
      figure img { width: 100%; height: auto; display: block; }
      figure figcaption { font-size: 0.85rem; color: #555;
                          text-align: center; padding-top: 0.3rem; }
      code { background: #f0f0f0; padding: 0 0.3rem; border-radius: 3px;
             font-size: 0.85rem; }
      .err { color: #c33; font-size: 0.9rem; }
      .warn { color: #a60; font-style: italic; }
      ol li { margin: 0.3rem 0; }
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{result.name} — Multi-domain Design</title>
  <style>{css}</style>
</head>
<body>
  <h1>{result.name}</h1>
  <p>Generated in {result.elapsed_s:.1f}s — overall: <strong>{
    'PASS' if result.success else ('PARTIAL' if result.partial_success else 'FAIL')}</strong></p>

  {_section("Pipeline Stages", stages_html)}
  {_section("3D Renders", renders_html)}
  {_section("GD&T Drawings", drawings_html)}
  {_section("Mechanical BOM", mcad_html)}
  {_section("Electronic BOM", ecad_html)}

  <p><small>Output dir: <code>{output_dir}</code></small></p>
</body>
</html>"""
    out.write_text(html, encoding="utf-8")
    return str(out)


def _slug(text: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return s.strip("_")[:50] or "multi_domain"


def _extract_board_dims_from_spec(
    ecad_spec: str, ecad_artifacts: dict[str, str],
) -> tuple[float, float]:
    """Extract board width x height from spec text or BOM JSON.

    Tries (in order): "WxHmm" pattern in spec, "board_w/board_h" in BOM JSON,
    then defaults to 36x36mm.
    """
    import re
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*mm", ecad_spec
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    bom_path = ecad_artifacts.get("bom_json")
    if bom_path and Path(bom_path).is_file():
        try:
            data = json.loads(Path(bom_path).read_text(encoding="utf-8"))
            for k in ("board_size", "board_dims", "board_dimensions"):
                if k in data:
                    val = data[k]
                    if isinstance(val, dict):
                        w = val.get("w") or val.get("width") or val.get("width_mm")
                        h = val.get("h") or val.get("height") or val.get("height_mm")
                        if w and h:
                            return float(w), float(h)
            w = data.get("board_w_mm") or data.get("board_width_mm")
            h = data.get("board_h_mm") or data.get("board_height_mm")
            if w and h:
                return float(w), float(h)
        except Exception:
            pass
    return 36.0, 36.0
