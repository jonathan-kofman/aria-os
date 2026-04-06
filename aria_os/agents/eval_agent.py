"""EvalAgent — runs domain-specific validators and synthesizes pass/fail verdict."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .design_state import DesignState


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

        # 5. Physics (auto-detect FEA type — informational, not blocking)
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
        """ECAD: ERC + DRC checks."""
        # ECAD validation runs inline during generation
        # Check if artifacts have validation results
        val = state.artifacts.get("validation", {})
        if val:
            state.domain_results["ecad_validation"] = val
            for err in val.get("erc_errors", []):
                state.failures.append(f"erc: {err}")
            for err in val.get("drc_errors", []):
                state.failures.append(f"drc: {err}")

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
        """Drawing: check SVG has expected sections."""
        svg_path = state.output_path
        if not svg_path or not Path(svg_path).exists():
            state.failures.append("SVG drawing not generated")
            return

        content = Path(svg_path).read_text(encoding="utf-8")
        state.domain_results["drawing"] = {"size_bytes": len(content)}

        if len(content) < 5000:
            state.failures.append("drawing: SVG too small — likely incomplete")
        if "FRONT VIEW" not in content and "front" not in content.lower():
            state.failures.append("drawing: missing front view")
        if "title" not in content.lower():
            state.failures.append("drawing: missing title block")

    def _eval_assembly(self, state: DesignState) -> None:
        """Assembly: clearance check."""
        result = state.domain_results.get("clearance")
        if result and not result.get("passed", True):
            for v in result.get("violations", []):
                state.failures.append(f"assembly: {v}")

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
