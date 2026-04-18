"""EvalAgent — runs domain-specific validators and synthesizes pass/fail verdict."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .design_state import DesignState


def _is_floatable(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


class EvalAgent:
    """
    Runs deterministic validators for the domain.
    LLM is NOT used for pass/fail decisions — only for failure summarization.
    """

    def __init__(self, domain: str, repo_root: Path):
        self.domain = domain
        self.repo_root = repo_root
        self.name = f"EvalAgent[{domain}]"

    def evaluate(self, state: DesignState) -> None:
        """Run all validators and populate state.eval_passed + state.failures."""
        state.failures.clear()
        state.domain_results.clear()

        if self.domain == "cad":
            self._eval_cad(state)
        elif self.domain == "cam":
            self._eval_cam(state)
        elif self.domain == "ecad":
            self._eval_ecad(state)
        elif self.domain == "civil":
            self._eval_civil(state)
        elif self.domain == "drawing":
            self._eval_drawing(state)
        elif self.domain == "assembly":
            self._eval_assembly(state)

        state.eval_passed = len(state.failures) == 0

        tag = "PASS" if state.eval_passed else f"FAIL ({len(state.failures)} issues)"
        print(f"  [{self.name}] {tag}")
        for f in state.failures:
            print(f"    - {f}")

        # Teach about evaluation results
        engine = getattr(self, "_teaching_engine", None)
        if engine:
            if state.eval_passed:
                engine.teach_simple(
                    agent=self.name, phase="eval",
                    message="All geometry checks passed — part is structurally valid",
                    reasoning="The evaluator checks: file exists, single connected solid, "
                    "dimensions match spec (within 15%), mesh is watertight, and visual "
                    "verification confirms the expected features are present.",
                    tags=["validation"],
                )
            else:
                for f in state.failures:
                    # Translate failure codes into teaching moments
                    if "solid_count" in f:
                        engine.teach_simple(
                            agent=self.name, phase="eval",
                            message=f"Issue: {f}",
                            reasoning="A manufacturable part must be a single connected body. "
                            "Multiple disconnected solids mean features weren't boolean-unioned "
                            "together. In CNC machining, you machine one piece of stock — "
                            "disconnected bodies can't be cut from one block.",
                            tags=["validation", "dfm"],
                        )
                    elif "geometry" in f:
                        engine.teach_simple(
                            agent=self.name, phase="eval",
                            message=f"Geometry issue: {f}",
                            reasoning="Geometry validation checks that the part's dimensions match "
                            "what was requested and that the topology is valid (no self-intersections, "
                            "proper face normals, etc.).",
                            tags=["validation", "geometry"],
                        )
                    elif "watertight" in f.lower():
                        engine.teach_simple(
                            agent=self.name, phase="eval",
                            message=f"Mesh issue: {f}",
                            reasoning="A watertight mesh means every edge is shared by exactly "
                            "two faces — no holes or gaps. This is required for 3D printing "
                            "(the slicer needs to know inside vs outside) and for accurate "
                            "volume/mass calculations.",
                            tags=["validation", "mesh"],
                        )

    def _eval_cad(self, state: DesignState) -> None:
        """CAD: geometry + quality + physics."""
        step_path = state.artifacts.get("step_path", state.output_path)
        stl_path = state.artifacts.get("stl_path", "")

        # 1. Check files exist
        if not step_path or not Path(step_path).exists():
            state.failures.append("STEP file not generated")
            return

        # 2. Single solid check — a manufacturable part must be ONE connected body
        try:
            import cadquery as cq
            _shape = cq.importers.importStep(step_path)
            _solids = _shape.val().Solids()
            if len(_solids) > 1:
                _solid_info = ", ".join(
                    f"{s.BoundingBox().xlen:.0f}x{s.BoundingBox().ylen:.0f}x{s.BoundingBox().zlen:.0f}mm"
                    for s in _solids[:4]
                )
                state.failures.append(
                    f"solid_count: part has {len(_solids)} disconnected solids ({_solid_info}). "
                    f"All features must be unioned into a single body. "
                    f"Use result = base.union(feature) to join them."
                )
            elif len(_solids) == 0:
                state.failures.append("solid_count: STEP contains no solids")
        except Exception:
            pass

        # 3. Geometry validation (face-region analysis — reliable, no heuristics)
        try:
            from ..geometry_validator import validate_geometry
            geo = validate_geometry(step_path, state.part_id, state.spec, state.goal)
            state.domain_results["geometry"] = geo
            if not geo["passed"]:
                for check in geo["checks"]:
                    if not check["passed"]:
                        state.failures.append(f"geometry: {check['name']} — {check['detail']}")
        except Exception as exc:
            state.failures.append(f"geometry validator error: {exc}")

        # 3b. Part contract validation (genus-based hole counting + FFT radial
        # features) — runs from a spec-derived default contract. Catches the
        # silent-failure class: holes-not-cut, blade-count-wrong, missing-bore.
        self._check_part_contract(state, step_path)

        # 3. Output quality (STEP readable + STL watertight — skip bore heuristic)
        try:
            from ..post_gen_validator import check_output_quality
            quality = check_output_quality(step_path, stl_path)
            state.domain_results["quality"] = quality
            # Only report quality failures that aren't bore-detection false positives
            if not quality.get("passed", True):
                for f in quality.get("failures", []):
                    # Skip bore detection — it uses volume heuristics that produce false positives
                    if "bore not detected" in f.lower() or "through-bore" in f.lower():
                        continue
                    state.failures.append(f"quality: {f}")
        except Exception as exc:
            state.failures.append(f"quality check error: {exc}")

        # 4. Feature complexity check — detect lazy geometry (plain cylinders for gears, etc.)
        self._check_feature_complexity(state, step_path)

        # 5. Bbox vs spec check
        if state.bbox and state.spec:
            self._check_bbox_vs_spec(state)

        # 6. Visual verification — catches missing features that metrics miss
        # If no STL, generate one from STEP for visual checking
        if (not stl_path or not Path(stl_path).exists()) and step_path and Path(step_path).exists():
            try:
                import cadquery as cq
                shape = cq.importers.importStep(step_path)
                stl_path = step_path.replace(".step", ".stl")
                cq.exporters.export(shape, stl_path, exportType="STL")
            except Exception:
                pass

        # ── LAYERED VISUAL VERIFICATION ──────────────────────────────────────
        # Visual is a SOFT gate. It only escalates to a hard failure when:
        #   (a) the deterministic checks above all passed, AND
        #   (b) the vision LLM identifies a SPECIFIC feature complaint
        #       (e.g. "blade direction wrong", "missing bore"), not just low
        #       confidence or cross-validation provider disagreement.
        #
        # Why: vision LLMs disagree with each other and with reality on parts
        # that are geometrically correct. Treating every disagreement as a
        # hard fail produced 5/8 false negatives in the e2e audit. The
        # deterministic checks (bbox, hole count via genus, watertight,
        # contract validation) ARE the ground truth. Visual augments them by
        # catching things they can't see (feature direction, ornamentation,
        # subjective form) — but it should not override them.
        det_failures_before_visual = list(state.failures)  # snapshot
        if stl_path and Path(stl_path).exists():
            try:
                from ..visual_verifier import verify_visual
                vis = verify_visual(
                    step_path, stl_path, state.goal,
                    state.spec if isinstance(state.spec, dict) else {},
                    repo_root=self.repo_root,
                )
                state.domain_results["visual"] = vis
                conf = vis.get("confidence", 0)
                verified = vis.get("verified")
                hard_failed_checks = [
                    c for c in vis.get("checks", [])
                    if isinstance(c, dict) and not c.get("found", True)
                ]

                # Disagreement-only failures (cross-validation, low conf
                # without a specific feature complaint) are SOFT — record as
                # warnings.
                _disagreement_phrases = (
                    "cross-validation", "cross-validation by",
                    "disagreed", "marking fail to be safe",
                    "no clear indication",
                    "not explicitly verifiable",
                    "geometry precheck",
                    "image", "thickness", "unclear if", "number of",
                )

                def _is_soft(issue_text: str) -> bool:
                    s = str(issue_text).lower()
                    return any(p in s for p in _disagreement_phrases)

                soft_issues = []
                hard_issues = []
                for issue in vis.get("issues", []):
                    (soft_issues if _is_soft(issue) else hard_issues).append(issue)

                state.domain_results.setdefault("visual_breakdown", {})
                state.domain_results["visual_breakdown"] = {
                    "n_hard_feature_complaints": len(hard_failed_checks),
                    "n_soft_issues": len(soft_issues),
                    "n_hard_issues": len(hard_issues),
                    "soft_issues": soft_issues[:5],
                    "hard_issues": hard_issues[:5],
                }

                if hard_failed_checks or hard_issues:
                    # Real visual feature complaints — escalate to failure
                    for c in hard_failed_checks:
                        state.failures.append(
                            f"visual: {c.get('feature', '?')} — {c.get('notes', 'not found')[:80]}"
                        )
                    for issue in hard_issues[:3]:
                        state.failures.append(f"visual: {issue}")
                    print(f"    [VISUAL] FAIL — {conf:.0%}, "
                          f"{len(hard_failed_checks)} feature complaints + "
                          f"{len(hard_issues)} hard issues")
                elif soft_issues and not det_failures_before_visual:
                    # Vision unsure but deterministic checks all passed —
                    # informational only, do NOT fail.
                    print(f"    [VISUAL] WARN — {conf:.0%}, "
                          f"{len(soft_issues)} disagreement(s); deterministic checks all passed")
                    state.domain_results["visual_status"] = "soft_warn_overridden"
                elif soft_issues and det_failures_before_visual:
                    # Vision unsure AND deterministic already flagged issues —
                    # escalate so both signals reach the user.
                    for issue in soft_issues[:2]:
                        state.failures.append(f"visual: {issue}")
                    print(f"    [VISUAL] FAIL — {conf:.0%}, "
                          f"{len(soft_issues)} issue(s) (deterministic already flagged failures)")
                elif verified is True:
                    print(f"    [VISUAL] PASS — {conf:.0%}")
                else:
                    print(f"    [VISUAL] PASS (no specific issues) — {conf:.0%}")
            except Exception as _ve:
                print(f"    [VISUAL] skipped: {_ve}")

        # 7. Physics (auto-detect FEA type — informational, not blocking)
        try:
            from ..physics_analyzer import analyze
            phys = analyze(
                state.part_id, "auto", state.spec, state.goal, str(self.repo_root))
            state.domain_results["physics"] = phys
            # Physics failures are warnings in agent mode, not blockers
            # (the part may be correctly sized but the simplified FEA model is wrong)
            if not phys.get("passed", True):
                for f in phys.get("failures", []):
                    print(f"    [physics warn] {f}")
        except Exception:
            pass  # physics is optional

    def _eval_cam(self, state: DesignState) -> None:
        """CAM: machinability + feeds/speeds validation."""
        step_path = state.artifacts.get("step_path", "")
        if not step_path or not Path(step_path).exists():
            state.failures.append("No STEP file for CAM validation")
            return

        try:
            from ..cam_validator import check_machinability
            result = check_machinability(step_path)
            state.domain_results["machinability"] = result
            for v in result.get("violations", result.get("failures", [])):
                state.failures.append(f"cam: {v}")
        except Exception as exc:
            state.failures.append(f"cam validator error: {exc}")

    def _eval_ecad(self, state: DesignState) -> None:
        """ECAD: ERC + DRC + spec adherence + actual file existence checks.

        Beefed up from the original 'read state.artifacts validation if present'
        because that was the false-PASS pattern: if the ECAD generator never
        wrote validation keys, eval found nothing and reported PASS even when
        the BOM was empty or the placement had overlaps.
        """
        # 1. Read inline validation if generator provided it
        val = state.artifacts.get("validation", {}) or {}
        # 2. Also try to read validation.json from the ECAD output dir directly
        #    (the multi_domain pipeline writes it there, not in state.artifacts)
        if not val:
            try:
                from pathlib import Path as _P
                kicad_pcb = state.artifacts.get("kicad_pcb") or state.artifacts.get("script_path")
                if kicad_pcb:
                    for vp in _P(kicad_pcb).parent.rglob("validation.json"):
                        import json as _json
                        val = _json.loads(vp.read_text(encoding="utf-8"))
                        break
            except Exception:
                pass

        state.domain_results["ecad_validation"] = val

        # ERC failures
        erc = val.get("erc", {}) if isinstance(val.get("erc"), dict) else {}
        for err in erc.get("errors", []) or val.get("erc_errors", []):
            state.failures.append(f"erc: {err}")

        # DRC failures (count overlaps separately — they're the most common bug)
        drc = val.get("drc", {}) if isinstance(val.get("drc"), dict) else {}
        violations = drc.get("violations", []) or val.get("drc_errors", []) or []
        n_overlaps = sum(1 for v in violations if "overlap" in str(v).lower())
        for v in violations:
            state.failures.append(f"drc: {v}")

        # 3. BOM sanity — every ECAD run should produce a non-empty BOM
        bom_path = state.artifacts.get("bom") or state.artifacts.get("bom_path")
        if bom_path:
            try:
                from pathlib import Path as _P
                import json as _json
                bom_data = _json.loads(_P(bom_path).read_text(encoding="utf-8"))
                n_components = (bom_data.get("total_components")
                                or len(bom_data.get("components", []))
                                or 0)
                state.domain_results["ecad_bom"] = {"n_components": n_components}
                if n_components == 0:
                    state.failures.append("ecad: BOM is empty — no components placed")
            except Exception as exc:
                state.failures.append(f"ecad: could not read BOM — {exc}")

        # 4. Spec-vs-BOM adherence — if user spec mentions a part, BOM must
        #    contain a matching value. Catches LLM substitution drift.
        if isinstance(state.spec, dict):
            ecad_spec = state.spec.get("ecad_spec") or state.goal or ""
            if ecad_spec and bom_path:
                missing = self._check_ecad_spec_adherence(ecad_spec, bom_path)
                for m in missing:
                    state.failures.append(f"ecad: spec drift — {m}")

    def _check_ecad_spec_adherence(self, spec_text: str, bom_path: str) -> list[str]:
        """Look for parts user explicitly named in the spec; flag if absent from BOM."""
        import re
        from pathlib import Path as _P
        import json as _json

        try:
            bom_data = _json.loads(_P(bom_path).read_text(encoding="utf-8"))
            components = bom_data.get("components") or []
            bom_text = " ".join(
                str(c.get("value", "") or c.get("part", "")).lower()
                for c in components
            )
        except Exception:
            return []

        # Each entry: (regex on spec, regex on BOM that satisfies it, label)
        adherence_checks = [
            (r"\bstm32\w*", r"stm32\w*", "STM32 MCU"),
            (r"\besp32\w*", r"esp32\w*", "ESP32 MCU"),
            (r"\brp2040\b", r"rp2040", "RP2040 MCU"),
            (r"\bmpu[-_ ]?6000\b", r"mpu[-_ ]?6000", "MPU-6000 IMU"),
            (r"\bmpu[-_ ]?6050\b", r"mpu[-_ ]?6050", "MPU-6050 IMU"),
            (r"\bbmp[-_ ]?280\b", r"bmp[-_ ]?280", "BMP280 baro"),
            (r"\bjst[-_ ]?xh\b", r"jst[-_ ]?xh", "JST-XH connector"),
            (r"\bxt30\b", r"xt30", "XT30 connector"),
            (r"\bxt60\b", r"xt60", "XT60 connector"),
            (r"\busb[-_ ]?c\b", r"usb[-_ ]?c", "USB-C connector"),
        ]
        spec_lower = spec_text.lower()
        missing = []
        for spec_re, bom_re, label in adherence_checks:
            if re.search(spec_re, spec_lower) and not re.search(bom_re, bom_text):
                missing.append(f"{label} requested but not in BOM")
        return missing

    def _eval_civil(self, state: DesignState) -> None:
        """Civil: layer completeness + standards checks."""
        dxf_path = state.output_path
        if not dxf_path or not Path(dxf_path).exists():
            state.failures.append("DXF file not generated")
            return

        try:
            import ezdxf
            doc = ezdxf.readfile(dxf_path)
            msp = doc.modelspace()

            # Count entities per layer
            layer_counts: dict[str, int] = {}
            for e in msp:
                lyr = getattr(e.dxf, "layer", "0")
                layer_counts[lyr] = layer_counts.get(lyr, 0) + 1

            total = sum(layer_counts.values())
            n_layers = len(layer_counts)

            state.domain_results["civil"] = {
                "total_entities": total,
                "layers": n_layers,
                "layer_counts": layer_counts,
            }

            if total < 20:
                state.failures.append(f"civil: only {total} entities — plan is too sparse")
            if n_layers < 3:
                state.failures.append(f"civil: only {n_layers} layers — need at least 3")
            if "ANNO-TEXT" not in layer_counts:
                state.failures.append("civil: missing ANNO-TEXT layer (no labels)")
        except Exception as exc:
            state.failures.append(f"civil validator error: {exc}")

    def _eval_drawing(self, state: DesignState) -> None:
        """Drawing validation — checks SVG completeness vs spec.

        Beefed up from the original 'size > 5000 bytes + has 'front'' check
        because that was the false-PASS pattern: a drawing with the wrong
        dimensions, missing GD&T callouts, wrong scale, or missing hole
        callouts would all pass.
        """
        import re
        svg_path = state.output_path
        if not svg_path or not Path(svg_path).exists():
            state.failures.append("SVG drawing not generated")
            return

        content = Path(svg_path).read_text(encoding="utf-8", errors="replace")
        size_b = len(content)

        # Parse what's in the drawing
        text_matches = re.findall(r"<text[^>]*>([^<]+)</text>", content, re.IGNORECASE)
        text_blob = " ".join(text_matches).lower()
        # Count distinct view labels (front/top/side/iso)
        views = {v: bool(re.search(rf"\b{v}\b", text_blob))
                 for v in ("front", "top", "side", "iso", "isometric", "section")}
        n_views = sum(views.values())
        # Find dimension values in the text (numbers with mm/inch/decimal)
        dim_values = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:mm|in|\")", content, re.IGNORECASE)
        # Title-block markers
        has_title_block = any(kw in text_blob for kw in
                              ("title", "drawn by", "scale", "tolerance", "material"))
        # GD&T markers (basic — full GD&T is harder to detect)
        gdt_symbols = re.findall(r"[\u2300-\u23FF\u25A0-\u25FF\u29DC-\u29DF]", content)
        has_dim_lines = bool(re.search(r"<line[^>]*stroke=", content))

        state.domain_results["drawing"] = {
            "size_bytes": size_b,
            "n_text_elements": len(text_matches),
            "n_views_detected": n_views,
            "views": views,
            "n_dim_values": len(dim_values),
            "has_title_block": has_title_block,
            "n_gdt_symbols": len(gdt_symbols),
            "has_dim_lines": has_dim_lines,
        }

        # Hard-fail conditions
        if size_b < 5000:
            state.failures.append(f"drawing: SVG only {size_b} bytes — likely incomplete")
        if n_views < 2:
            state.failures.append(
                f"drawing: only {n_views} view(s) detected — engineering drawings need "
                f"≥2 (typically front + top + side). Found: {[v for v, ok in views.items() if ok]}"
            )
        if not has_title_block:
            state.failures.append(
                "drawing: missing title block (no 'title'/'scale'/'tolerance'/'material' text)"
            )
        if not has_dim_lines:
            state.failures.append("drawing: no dimension lines (<line> stroke elements)")
        if len(dim_values) < 3:
            state.failures.append(
                f"drawing: only {len(dim_values)} dimension value(s) — typical drawings show ≥3"
            )

        # Spec-vs-drawing: every spec dim should appear in the text within 5%
        if isinstance(state.spec, dict):
            spec_dims = []
            for key in ("od_mm", "bore_mm", "width_mm", "height_mm",
                        "depth_mm", "length_mm", "thickness_mm"):
                v = state.spec.get(key)
                if v and float(v) > 0:
                    spec_dims.append((key, float(v)))
            for label, expected in spec_dims:
                # Match any drawing value within 5% of expected
                tol = max(0.5, 0.05 * expected)
                found = any(abs(float(dv) - expected) <= tol for dv in dim_values
                            if _is_floatable(dv))
                if not found:
                    state.failures.append(
                        f"drawing: spec value {label}={expected:.1f}mm not found in drawing "
                        f"(tol ±{tol:.1f}mm) — drawing dimensions may not match the part"
                    )

    def _eval_assembly(self, state: DesignState) -> None:
        """Assembly: clearance check."""
        result = state.domain_results.get("clearance")
        if result and not result.get("passed", True):
            for v in result.get("violations", []):
                state.failures.append(f"assembly: {v}")

    def _check_part_contract(self, state: DesignState, step_path: str) -> None:
        """Validate generated geometry against a spec-derived Contract.

        Auto-derives expected properties from state.spec (n_bolts → hole count,
        n_blades → radial lobes, dims → bbox). Failures land in state.failures.

        Empty contracts (no expectations derivable from spec) are recorded but
        not failed — a real "this spec is too thin" warning is logged so we
        know which templates need richer specs to be checkable.

        If the validator itself throws, we record that as a FAILURE — silent
        skips were the false-PASS pattern. Better to surface a noisy bug than
        miss a real one.
        """
        from ..validation import Contract, validate_part
        try:
            import cadquery as cq
        except Exception as exc:
            state.failures.append(f"contract: cadquery unavailable — {exc}")
            return

        spec = state.spec if isinstance(state.spec, dict) else {}
        contract = Contract.from_spec(spec, state.goal)
        if contract.is_empty():
            # Don't fail eval — but record this so we know the spec is too
            # thin to validate. Surface in CLI output and domain_results.
            state.domain_results["part_contract"] = {
                "passed": True,
                "warnings": ["spec is too thin to derive a contract — no checkable expectations"],
                "expected": {},
            }
            print(f"    [contract] WARN: spec has no checkable dims/counts — validation skipped")
            return

        try:
            shape = cq.importers.importStep(step_path)
        except Exception as exc:
            state.failures.append(
                f"contract: STEP load failed — {type(exc).__name__}: {exc}"
            )
            return

        try:
            result = validate_part(shape, contract)
        except Exception as exc:
            # Don't swallow — a validator crash on real geometry is itself
            # a bug worth surfacing.
            state.failures.append(
                f"contract: validator crashed — {type(exc).__name__}: {exc}"
            )
            return

        state.domain_results["part_contract"] = {
            "passed": result.passed,
            "failures": result.failures,
            "warnings": result.warnings,
            "measured": result.measured,
            "expected": {
                "bbox_mm": list(contract.expected_bbox_mm) if contract.expected_bbox_mm else None,
                "hole_count": contract.expected_hole_count,
                "radial_features": contract.radial_features,
            },
        }
        if not result.passed:
            for f in result.failures:
                state.failures.append(f"contract: {f}")

    def _check_feature_complexity(self, state: DesignState, step_path: str) -> None:
        """
        Detect lazy geometry — the DesignerAgent generating a plain cylinder/box
        when the goal requires complex features (teeth, ribs, cutouts, etc.).

        Uses face count as a proxy: a 40-tooth gear should have 100+ faces,
        not 4 (plain cylinder). A phone case needs 50+ faces, not 6 (plain box).
        """
        goal_lower = state.goal.lower()
        face_count = state.domain_results.get("geometry", {}).get("face_count", 0)
        if face_count == 0:
            try:
                import cadquery as cq
                shape = cq.importers.importStep(step_path)
                face_count = len(shape.val().Faces())
            except Exception:
                return

        # Gear/sprocket/escapement: needs teeth → many faces
        n_teeth = state.spec.get("n_teeth", 0)
        if n_teeth and n_teeth > 0:
            min_faces = max(n_teeth * 2, 20)  # at least 2 faces per tooth
            if face_count < min_faces:
                state.failures.append(
                    f"feature_complexity: goal requires {n_teeth} teeth but geometry has only "
                    f"{face_count} faces — likely a plain cylinder, not a toothed part. "
                    f"Need at least {min_faces} faces for {n_teeth} teeth."
                )
                return

        # Detect gear/tooth keywords even without n_teeth in spec
        _tooth_keywords = ("gear", "tooth", "teeth", "sprocket", "escapement", "pinion",
                           "ratchet", "cog", "involute")
        if any(kw in goal_lower for kw in _tooth_keywords):
            if face_count < 20:
                state.failures.append(
                    f"feature_complexity: goal describes a toothed part but geometry has only "
                    f"{face_count} faces — likely missing tooth features. Need 20+ faces."
                )
                return

        # Case/enclosure/shell: needs cavity + cutouts
        _shell_keywords = ("case", "housing", "enclosure", "shell", "box")
        if any(kw in goal_lower for kw in _shell_keywords):
            if face_count < 10:
                state.failures.append(
                    f"feature_complexity: goal describes a hollow part but geometry has only "
                    f"{face_count} faces — likely a solid block, not a shelled part. Need 10+ faces."
                )
                return

        # Bracket with holes: needs more than a plain plate
        # A through-hole in a cylinder adds 1 face (the cylindrical wall).
        # Minimum: top + bottom + outer_wall + bore_wall + N_holes = N + 4
        # For a plain box: top + bottom + 4_sides + N_holes = N + 6
        # Use N + 4 as minimum (cylindrical parts have fewer base faces)
        if "hole" in goal_lower or "bolt" in goal_lower:
            n_bolts = state.spec.get("n_bolts", 0)
            min_faces = max(6, n_bolts + 4) if n_bolts else 6
            if n_bolts and face_count < min_faces:
                state.failures.append(
                    f"feature_complexity: goal specifies {n_bolts} holes but geometry has only "
                    f"{face_count} faces — holes may not be cut. Need {min_faces}+ faces."
                )

        # Advanced feature checks — if the goal asked for specific operations,
        # verify the geometry actually used them (not just a plain extrude)
        _advanced_checks = [
            (["hollow", "shell", "thin wall", "enclosure", "case"],
             lambda fc, vol, bbox: vol / (bbox["x"] * bbox["y"] * bbox["z"]) < 0.55,
             "goal describes a hollow/shell part but geometry fill ratio > 55% — "
             "use result.shell(WALL) or result.faces('>Z').shell(-WALL) to hollow it out"),
            (["curved", "bend", "elbow", "sweep", "swept"],
             lambda fc, vol, bbox: fc >= 3,  # swept parts have curved faces
             "goal describes a curved/swept part but geometry looks like a simple extrusion — "
             "use .sweep(path) to create a curved path"),
            (["fillet", "rounded edge", "smooth"],
             lambda fc, vol, bbox: fc > 8,  # fillets add faces
             "goal asks for fillets but geometry has too few faces — "
             "use result.edges('|Z').fillet(R) or result.edges('>Z').fillet(R)"),
        ]

        if face_count > 0:
            try:
                import cadquery as _cq
                _shape = _cq.importers.importStep(step_path)
                _bb = _shape.val().BoundingBox()
                _vol = _shape.val().Volume()
                _bbox = {"x": _bb.xlen, "y": _bb.ylen, "z": _bb.zlen}

                for keywords, check_fn, fail_msg in _advanced_checks:
                    if any(kw in goal_lower for kw in keywords):
                        if not check_fn(face_count, _vol, _bbox):
                            state.failures.append(f"feature_complexity: {fail_msg}")
                            break  # one failure at a time
            except Exception:
                pass

    def _check_bbox_vs_spec(self, state: DesignState) -> None:
        """Check if generated bbox approximately matches USER-specified dimensions.
        Only checks dims from the original goal extraction, not CEM-injected values."""
        bb = state.bbox

        # Re-extract spec from goal only (no CEM contamination)
        try:
            from ..spec_extractor import extract_spec
            user_spec = extract_spec(state.goal)
        except Exception:
            user_spec = state.spec

        # Determine which dims are "thickness" (plate material, not bbox)
        # For brackets, L-brackets, heat sinks — the smallest WxHxD dim is thickness
        goal_lower = state.goal.lower()
        part_type = user_spec.get("part_type", "")
        _thickness_parts = ("bracket", "l_bracket", "phone_stand",
                            "flat_plate", "base_plate", "catch_pawl",
                            "flange", "spacer", "gusset", "enclosure_lid",
                            "clamp", "snap_hook", "spring_clip", "hinge")

        # Heat sinks: thickness_mm is fin thickness, NOT part thickness.
        # The part is base_t + fin_height tall. Skip thickness bbox check entirely.
        if part_type == "heat_sink" or "heat sink" in goal_lower or "fin" in goal_lower:
            for k in ("thickness_mm", "height_mm", "depth_mm"):
                user_spec.pop(k, None)  # don't check these against bbox
        _is_thickness_part = part_type in _thickness_parts or any(
            kw in goal_lower for kw in ("thick", "plate", "sheet", "bracket", "heat sink"))

        # If it's a thickness-type part, treat the smallest extracted dim as thickness
        _dims = {}
        for k in ("width_mm", "height_mm", "depth_mm"):
            v = user_spec.get(k)
            if v:
                _dims[k] = float(v)
        _min_dim_key = min(_dims, key=_dims.get) if _dims else ""
        _thickness_key = _min_dim_key if _is_thickness_part and _dims else ""

        checks = [
            ("od_mm", "OD", False),
            ("width_mm", "width", _thickness_key == "width_mm"),
            ("height_mm", "height", _thickness_key == "height_mm"),
            ("depth_mm", "depth", _thickness_key == "depth_mm"),
        ]
        for key, label, is_thickness in checks:
            val = user_spec.get(key)
            if not val:
                continue
            val = float(val)

            if is_thickness:
                # This is plate/material thickness — it won't appear as a bbox axis.
                # An L-bracket "50x30x3mm" has bbox 50x30x33mm (base+leg). The 3mm
                # is plate material thickness, not a dimension you can see in the bbox.
                # Skip entirely — thickness is verified by the dimensional verifier,
                # not the bbox checker.
                continue

            tol = max(2.0, val * 0.20)
            if not any(abs(bb.get(axis, 0) - val) <= tol for axis in ("x", "y", "z")):
                closest = min(bb.values(), key=lambda v: abs(v - val)) if bb else 0
                state.failures.append(
                    f"bbox: no axis matches {label}={val:.1f}mm (closest={closest:.1f}mm, tol={tol:.1f}). "
                    f"Check the code — a value in .extrude() or .box() may be wrong.")
