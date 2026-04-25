"""Plan validator — catches malformed ops before they're streamed to Fusion.

A bad plan is a partial tree plus an error row. Catching structural issues
here means we can regenerate or repair the plan rather than leaving the
user with a half-built part in their Fusion document.
"""
from __future__ import annotations


_VALID_KINDS = {
    # Core mechanical ops
    "beginPlan", "newSketch", "sketchCircle", "sketchRect",
    "extrude", "circularPattern", "fillet",
    # W1: extended sketch primitives
    "sketchSpline", "sketchPolyline", "sketchTangentArc",
    "sketchOffset", "sketchProjection", "sketchEquationCurve",
    # W1: extended solid features
    "revolve", "sweep", "loft", "helix", "coil",
    "rib", "shell", "draft", "boundarySurface", "thicken",
    # W1: standard-hardware ops
    "threadFeature", "gearFeature",
    # W3: implicit / SDF ops — produce mesh bodies that the host
    # bridge imports and booleans against the parent solid.
    "implicitInfill", "implicitChannel", "implicitLattice",
    "implicitField", "implicitBoolean",
    # W3: mesh-bridge primitive — host-side "import STL + boolean".
    # The server expands implicitInfill/Channel/Lattice into one of
    # these per implicit op before streaming to the host bridge.
    "meshImportAndCombine",
    # Assembly
    "asmBegin", "addComponent", "joint",
    # W4: real assembly mates + motion drivers
    "mateConcentric", "mateCoincident", "mateDistance",
    "mateAngle", "mateGear", "mateSlider", "mateSlot",
    "motionRevolute", "motionPrismatic", "motionContact",
    # Drawing
    "beginDrawing", "newSheet", "addView", "addTitleBlock",
    "drawingAutoDim",
    # Fusion Electronics
    "beginElectronics", "placeSymbol", "placeFootprint",
    "addConnection", "boardOutline",
    # Fusion native-leverage
    "addParameter", "openGenerativeDesign", "createCAMSetup",
    "createMotionStudy", "sheetMetalBase", "sheetMetalFlange",
    # W5: extended sheet metal vocabulary — uses Fusion's Sheet Metal
    # workspace API (FlangeFeatures, BendFeatures, etc.) so flat
    # patterns auto-unfold and bend allowances come from the rule
    # library.
    "sheetMetalBend", "sheetMetalLouver", "sheetMetalHem",
    "sheetMetalUnfold", "sheetMetalCutout", "exportFlatPattern",
    "snapshotVersion",
    # KiCad server-side
    "beginBoard", "setStackup", "addNet", "addTrack", "addVia",
    "addZone", "routeBoard",
}

_REQUIRED_PARAMS = {
    "newSketch":       {"plane", "alias"},
    "sketchCircle":    {"sketch", "r"},
    "sketchRect":      {"sketch", "w", "h"},
    "extrude":         {"sketch", "distance", "operation"},
    "circularPattern": {"feature", "count"},
    "fillet":          {"body", "r"},
    # Extended sketch primitives — every primitive must reference an
    # existing sketch alias and carry enough geometry to be unambiguous.
    "sketchSpline":      {"sketch", "points"},
    "sketchPolyline":    {"sketch", "points"},
    "sketchTangentArc":  {"sketch", "start", "end", "tangent"},
    "sketchOffset":      {"sketch", "source", "distance"},
    "sketchProjection":  {"sketch", "edge"},
    "sketchEquationCurve": {"sketch", "expr", "t_min", "t_max"},
    # Extended solid features
    "revolve":         {"sketch", "axis", "angle", "operation"},
    "sweep":           {"profile_sketch", "path_sketch", "operation"},
    "loft":            {"sections", "operation"},
    "helix":           {"axis", "pitch", "height", "diameter"},
    "coil":            {"axis", "pitch", "turns", "diameter", "section"},
    "rib":             {"sketch", "thickness"},
    "shell":           {"body", "thickness"},
    "draft":           {"body", "faces", "angle"},
    "boundarySurface": {"edges"},
    "thicken":         {"surface", "thickness"},
    # Standard hardware: stamped onto an existing feature/face/body.
    "threadFeature":   {"face", "spec"},
    "gearFeature":     {"sketch", "module", "n_teeth", "thickness"},
    # W3: implicit / SDF ops — every implicit op needs a target body
    # alias (the native solid the implicit will be combined with) and
    # a boolean operation describing how to combine. The actual SDF
    # parameters depend on `pattern` and are validated lazily by the
    # SDF kernel at execution time.
    "implicitInfill":   {"target", "pattern", "operation"},
    "implicitChannel":  {"target", "path", "diameter", "operation"},
    "implicitLattice":  {"target", "cell", "size", "operation"},
    "implicitField":    {"expr", "bounds", "operation"},
    "implicitBoolean":  {"sdf_a", "sdf_b", "op"},
    "meshImportAndCombine": {"stl_path", "target", "operation"},
    # W5: sheet metal — body-creating ops mark saw_new_body via the
    # main loop; flange/bend/louver attach to existing edges/faces.
    "sheetMetalBend":    {"edges", "angle"},
    "sheetMetalLouver":  {"face", "n_louvers", "size_mm"},
    "sheetMetalHem":     {"edges", "type"},
    "sheetMetalUnfold":  {"body"},
    "sheetMetalCutout":  {"sketch", "operation"},
    "exportFlatPattern": {"body", "format"},
    # W4: assembly mates — every mate references at least 2 parts.
    # parts is a list of strings of the form "<component_id>" or
    # "<component_id>.<connector>" (e.g. "sun.axis", "carrier.pin_1").
    "mateConcentric":   {"parts"},
    "mateCoincident":   {"parts"},
    "mateDistance":     {"parts", "distance"},
    "mateAngle":        {"parts", "angle"},
    "mateGear":         {"parts", "ratio"},
    "mateSlider":       {"parts", "axis"},
    "mateSlot":         {"parts"},
    "motionRevolute":   {"joint"},
    "motionPrismatic":  {"joint"},
    "motionContact":    {"parts"},
    # asmBegin / addComponent need their own light schema
    "addComponent":     {"id", "type"},
}

# Operation modes accepted by ops that combine with existing geometry.
_BOOLEAN_OPS = ("new", "cut", "join", "intersect")


def validate_plan(plan: list[dict]) -> tuple[bool, list[str]]:
    """Structural check. Returns (ok, list_of_issues). Empty issues == ok."""
    issues: list[str] = []
    if not plan:
        return False, ["Plan is empty"]
    if plan[0].get("kind") not in ("beginPlan", "asmBegin",
                                    "beginDrawing", "beginElectronics",
                                    "beginBoard"):
        issues.append(
            f"First op must be beginPlan / asmBegin / beginDrawing / "
            f"beginElectronics / beginBoard (got {plan[0].get('kind')!r})")

    sketch_aliases: set[str] = set()
    feature_aliases: set[str] = set()
    # W4: assembly scope — components declared via addComponent.
    # Mate ops reference them by id; cross-checked here.
    component_ids: set[str] = set()
    in_asm_mode = False
    # Track the operation mode of each extrude alias so we can catch
    # semantic gotchas like "circularPattern a 'new' body" (which rotates
    # the full part around Z and produces a no-op).
    extrude_op: dict[str, str] = {}
    saw_new_body = False

    for i, op in enumerate(plan, start=1):
        kind = op.get("kind")
        params = op.get("params", {}) or {}

        if kind not in _VALID_KINDS:
            issues.append(f"Op #{i}: unknown kind {kind!r}")
            continue

        required = _REQUIRED_PARAMS.get(kind, set())
        missing = required - set(params.keys())
        if missing:
            issues.append(f"Op #{i} ({kind}): missing params {sorted(missing)}")
            continue

        # Cross-reference checks
        if kind == "newSketch":
            plane = params.get("plane")
            if isinstance(plane, str) and plane.upper() not in ("XY", "XZ", "YZ"):
                issues.append(
                    f"Op #{i}: newSketch plane must be XY/XZ/YZ (got {plane!r})")
            alias = params.get("alias")
            if alias in sketch_aliases:
                issues.append(f"Op #{i}: sketch alias {alias!r} already used")
            sketch_aliases.add(alias)

        elif kind in ("sketchCircle", "sketchRect"):
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: references unknown sketch "
                    f"{params.get('sketch')!r}")

        elif kind == "sheetMetalBase":
            # Sheet metal base flange IS the body-creating op in the
            # sheet-metal workflow — treat it equivalently to an
            # extrude with operation="new" for validator purposes.
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: sheetMetalBase references unknown sketch "
                    f"{params.get('sketch')!r}")
            saw_new_body = True
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                extrude_op[alias] = "new"

        # W5: extended sheet metal ops — all attach to existing
        # geometry (edges/faces of an existing sheet-metal body).
        elif kind == "sheetMetalBend":
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: sheetMetalBend requires an existing "
                    "sheet metal body (emit sheetMetalBase first)")
            try:
                ang = float(params.get("angle"))
                if abs(ang) > 270:
                    issues.append(
                        f"Op #{i}: sheetMetalBend angle {ang}° exceeds "
                        "±270 — physically infeasible for a single bend")
            except (TypeError, ValueError):
                issues.append(
                    f"Op #{i}: sheetMetalBend angle not numeric")

        elif kind == "sheetMetalLouver":
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: sheetMetalLouver requires an existing "
                    "sheet metal body")
            try:
                n = int(params.get("n_louvers"))
                if n < 1 or n > 100:
                    issues.append(
                        f"Op #{i}: sheetMetalLouver n_louvers={n} "
                        "out of [1, 100]")
            except (TypeError, ValueError):
                issues.append(
                    f"Op #{i}: sheetMetalLouver n_louvers not integer")

        elif kind == "sheetMetalHem":
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: sheetMetalHem requires an existing body")
            hem_type = (params.get("type") or "").lower()
            if hem_type not in ("closed", "open", "rolled", "teardrop"):
                issues.append(
                    f"Op #{i}: sheetMetalHem type must be one of "
                    f"closed|open|rolled|teardrop (got {params.get('type')!r})")

        elif kind == "sheetMetalUnfold":
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: sheetMetalUnfold requires an existing body")

        elif kind == "sheetMetalCutout":
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: sheetMetalCutout references unknown sketch "
                    f"{params.get('sketch')!r}")
            op_mode = params.get("operation", "cut")
            if op_mode not in ("cut", "join"):
                issues.append(
                    f"Op #{i}: sheetMetalCutout operation must be cut|join "
                    f"(got {op_mode!r})")

        elif kind == "exportFlatPattern":
            fmt = (params.get("format") or "").lower()
            if fmt not in ("dxf", "dwg", "step"):
                issues.append(
                    f"Op #{i}: exportFlatPattern format must be dxf/dwg/step "
                    f"(got {params.get('format')!r})")
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: exportFlatPattern needs an existing body")

        elif kind == "extrude":
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: extrude references unknown sketch "
                    f"{params.get('sketch')!r}")
            op_mode = params.get("operation")
            if op_mode not in ("new", "cut", "join", "intersect"):
                issues.append(f"Op #{i}: extrude operation must be "
                              f"new/cut/join/intersect (got {op_mode!r})")
            if op_mode == "new":
                saw_new_body = True
            elif not saw_new_body:
                issues.append(
                    f"Op #{i}: cannot {op_mode} before a body exists "
                    "(first extrude must be operation='new')")
            try:
                d = float(params.get("distance"))
                if d == 0:
                    issues.append(f"Op #{i}: extrude distance is 0")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: distance is not numeric")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                extrude_op[alias] = op_mode

        elif kind == "circularPattern":
            src = params.get("feature")
            if src not in feature_aliases:
                issues.append(
                    f"Op #{i}: circularPattern references unknown feature "
                    f"{src!r}")
            # Semantic: patterning a full body around its central axis is
            # a no-op. The source must be a cut/join/intersect feature
            # (a local modification) — not the "new" body itself.
            elif extrude_op.get(src) == "new":
                issues.append(
                    f"Op #{i}: circularPattern source {src!r} is the full "
                    "body (operation='new') — rotating it around the axis "
                    "will produce the same body. Pattern a cut or joined "
                    "feature (e.g. one blade, one bolt hole) instead.")
            try:
                n = int(params.get("count"))
                if n < 2 or n > 500:
                    issues.append(
                        f"Op #{i}: circularPattern count={n} out of range [2,500]")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: count is not an integer")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)

        elif kind == "fillet":
            try:
                r = float(params.get("r"))
                if r <= 0:
                    issues.append(f"Op #{i}: fillet radius {r} must be positive")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: fillet radius is not numeric")

        # W1: extended sketch primitives — all reference an existing sketch.
        elif kind in ("sketchSpline", "sketchPolyline", "sketchTangentArc",
                      "sketchOffset", "sketchProjection", "sketchEquationCurve"):
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: {kind} references unknown sketch "
                    f"{params.get('sketch')!r}")
            if kind in ("sketchSpline", "sketchPolyline"):
                pts = params.get("points") or []
                min_pts = 3 if kind == "sketchSpline" else 2
                if not isinstance(pts, list) or len(pts) < min_pts:
                    issues.append(
                        f"Op #{i}: {kind} needs at least {min_pts} points "
                        f"(got {len(pts) if isinstance(pts, list) else 'non-list'})")

        elif kind == "revolve":
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: revolve references unknown sketch "
                    f"{params.get('sketch')!r}")
            op_mode = params.get("operation")
            if op_mode not in _BOOLEAN_OPS:
                issues.append(
                    f"Op #{i}: revolve operation must be {'/'.join(_BOOLEAN_OPS)} "
                    f"(got {op_mode!r})")
            if op_mode == "new":
                saw_new_body = True
            elif not saw_new_body:
                issues.append(
                    f"Op #{i}: cannot {op_mode} before a body exists")
            try:
                ang = float(params.get("angle"))
                if ang == 0 or abs(ang) > 360:
                    issues.append(
                        f"Op #{i}: revolve angle {ang}° invalid (0 < |a| ≤ 360)")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: revolve angle is not numeric")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                extrude_op[alias] = op_mode

        elif kind == "sweep":
            ps = params.get("profile_sketch")
            pp = params.get("path_sketch")
            if ps not in sketch_aliases:
                issues.append(
                    f"Op #{i}: sweep profile_sketch {ps!r} unknown")
            if pp not in sketch_aliases:
                issues.append(
                    f"Op #{i}: sweep path_sketch {pp!r} unknown")
            if ps == pp and ps is not None:
                issues.append(
                    f"Op #{i}: sweep profile and path cannot be the same sketch")
            op_mode = params.get("operation")
            if op_mode not in _BOOLEAN_OPS:
                issues.append(
                    f"Op #{i}: sweep operation must be {'/'.join(_BOOLEAN_OPS)} "
                    f"(got {op_mode!r})")
            if op_mode == "new":
                saw_new_body = True
            elif not saw_new_body:
                issues.append(
                    f"Op #{i}: cannot {op_mode} before a body exists")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                extrude_op[alias] = op_mode

        elif kind == "loft":
            sections = params.get("sections") or []
            if not isinstance(sections, list) or len(sections) < 2:
                issues.append(
                    f"Op #{i}: loft needs ≥2 sections "
                    f"(got {len(sections) if isinstance(sections, list) else 'non-list'})")
            else:
                for s in sections:
                    if s not in sketch_aliases:
                        issues.append(
                            f"Op #{i}: loft section {s!r} unknown")
            op_mode = params.get("operation")
            if op_mode not in _BOOLEAN_OPS:
                issues.append(
                    f"Op #{i}: loft operation must be {'/'.join(_BOOLEAN_OPS)} "
                    f"(got {op_mode!r})")
            if op_mode == "new":
                saw_new_body = True
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                extrude_op[alias] = op_mode

        elif kind in ("helix", "coil"):
            try:
                pitch = float(params.get("pitch"))
                dia = float(params.get("diameter"))
                if pitch <= 0:
                    issues.append(f"Op #{i}: {kind} pitch must be > 0")
                if dia <= 0:
                    issues.append(f"Op #{i}: {kind} diameter must be > 0")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: {kind} pitch/diameter not numeric")
            if kind == "helix":
                try:
                    h = float(params.get("height"))
                    if h <= 0:
                        issues.append(f"Op #{i}: helix height must be > 0")
                except (TypeError, ValueError):
                    issues.append(f"Op #{i}: helix height not numeric")
            else:  # coil — needs profile section, and IS body-creating
                if params.get("section") not in sketch_aliases:
                    issues.append(
                        f"Op #{i}: coil section {params.get('section')!r} unknown")
                op_mode = params.get("operation", "new")
                if op_mode not in _BOOLEAN_OPS:
                    issues.append(
                        f"Op #{i}: coil operation must be {'/'.join(_BOOLEAN_OPS)} "
                        f"(got {op_mode!r})")
                if op_mode == "new":
                    saw_new_body = True
                elif not saw_new_body:
                    issues.append(
                        f"Op #{i}: cannot {op_mode} before a body exists")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                if kind == "coil":
                    extrude_op[alias] = params.get("operation", "new")

        elif kind == "rib":
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: rib references unknown sketch "
                    f"{params.get('sketch')!r}")
            try:
                t = float(params.get("thickness"))
                if t <= 0:
                    issues.append(f"Op #{i}: rib thickness must be > 0")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: rib thickness not numeric")

        elif kind == "shell":
            try:
                t = float(params.get("thickness"))
                if t <= 0:
                    issues.append(f"Op #{i}: shell thickness must be > 0")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: shell thickness not numeric")
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: shell requires an existing body")

        elif kind == "draft":
            try:
                ang = float(params.get("angle"))
                if abs(ang) > 60:
                    issues.append(
                        f"Op #{i}: draft angle {ang}° unrealistic (>60°)")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: draft angle not numeric")
            if not saw_new_body:
                issues.append(
                    f"Op #{i}: draft requires an existing body")

        elif kind == "boundarySurface":
            edges = params.get("edges") or []
            if not isinstance(edges, list) or len(edges) < 3:
                issues.append(
                    f"Op #{i}: boundarySurface needs ≥3 edge references")

        elif kind == "thicken":
            try:
                t = float(params.get("thickness"))
                if t <= 0:
                    issues.append(f"Op #{i}: thicken thickness must be > 0")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: thicken thickness not numeric")

        elif kind == "threadFeature":
            spec = (params.get("spec") or "").upper()
            # Accept ISO metric (M16, M16x2), UN (1/4-20, 1/4-20-UNC), NPT
            import re as _re
            ok = bool(_re.match(r"^M\d+(\.\d+)?(X\d+(\.\d+)?)?$", spec)) \
                or bool(_re.match(r"^\d+(/\d+)?-\d+(-UNC|-UNF|-UNEF)?$", spec)) \
                or bool(_re.match(r"^\d+(/\d+)?-NPT(F)?$", spec))
            if not ok:
                issues.append(
                    f"Op #{i}: threadFeature spec {params.get('spec')!r} "
                    "not recognized (try 'M8x1.25', '1/4-20-UNC', '1/4-NPT')")

        elif kind in ("implicitInfill", "implicitChannel",
                      "implicitLattice", "implicitField",
                      "implicitBoolean"):
            # All implicit ops must target an existing body (except
            # implicitField/implicitBoolean which produce SDFs that
            # later ops combine — they don't bind to a body directly).
            op_mode = params.get("operation",
                                  "join" if kind != "implicitBoolean" else "")
            if kind == "implicitBoolean":
                bool_op = (params.get("op") or "").lower()
                if bool_op not in ("union", "intersect", "subtract",
                                    "smooth_union", "smooth_subtract"):
                    issues.append(
                        f"Op #{i}: implicitBoolean op must be one of "
                        "union|intersect|subtract|smooth_union|smooth_subtract "
                        f"(got {params.get('op')!r})")
            else:
                if op_mode not in _BOOLEAN_OPS:
                    issues.append(
                        f"Op #{i}: {kind} operation must be "
                        f"{'/'.join(_BOOLEAN_OPS)} (got {op_mode!r})")
                if kind != "implicitField":
                    target = params.get("target")
                    if target not in feature_aliases \
                            and target not in {a.removesuffix("__body")
                                                  for a in feature_aliases}:
                        issues.append(
                            f"Op #{i}: {kind} target {target!r} not a "
                            "known feature alias — emit the parent solid first")
                    if not saw_new_body:
                        issues.append(
                            f"Op #{i}: {kind} requires an existing body")
            # Pattern validation for implicitInfill / implicitLattice
            if kind == "implicitInfill":
                pat = (params.get("pattern") or "").lower()
                known = {"gyroid", "schwarz_p", "schwarz_d", "schwarz_w",
                         "iwp", "neovius", "diamond", "octet_truss",
                         "bcc", "fcc", "kagome", "honeycomb", "stochastic"}
                if pat not in known:
                    issues.append(
                        f"Op #{i}: implicitInfill pattern {params.get('pattern')!r} "
                        f"unknown (try one of {sorted(known)[:6]} …)")
                # Density (when given) must be 0 < d < 1
                if "density" in params:
                    try:
                        d = float(params["density"])
                        if not (0.0 < d < 1.0):
                            issues.append(
                                f"Op #{i}: implicitInfill density={d} "
                                "must be in (0, 1)")
                    except (TypeError, ValueError):
                        issues.append(
                            f"Op #{i}: implicitInfill density not numeric")
            if kind == "implicitChannel":
                try:
                    dia = float(params.get("diameter"))
                    if dia <= 0:
                        issues.append(
                            f"Op #{i}: implicitChannel diameter must be > 0")
                except (TypeError, ValueError):
                    issues.append(
                        f"Op #{i}: implicitChannel diameter not numeric")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)

        # W4: assembly scope ops.
        elif kind == "asmBegin":
            in_asm_mode = True
            saw_new_body = True   # asmBegin counts as having a "body context"

        elif kind == "addComponent":
            cid = params.get("id")
            ctype = params.get("type")
            if not cid or not ctype:
                issues.append(
                    f"Op #{i}: addComponent requires id + type")
            else:
                if cid in component_ids:
                    issues.append(
                        f"Op #{i}: component id {cid!r} already declared")
                component_ids.add(cid)
            saw_new_body = True

        elif kind in ("mateConcentric", "mateCoincident", "mateDistance",
                      "mateAngle", "mateGear", "mateSlider", "mateSlot"):
            parts = params.get("parts") or []
            if not isinstance(parts, list) or len(parts) < 2:
                issues.append(
                    f"Op #{i}: {kind} requires ≥2 parts (got {parts!r})")
            else:
                # Each part is "<component_id>" or "<component_id>.<connector>"
                for part_ref in parts:
                    if not isinstance(part_ref, str):
                        issues.append(
                            f"Op #{i}: {kind} part ref must be string "
                            f"(got {part_ref!r})")
                        continue
                    cid = part_ref.split(".", 1)[0]
                    if component_ids and cid not in component_ids:
                        issues.append(
                            f"Op #{i}: {kind} references unknown "
                            f"component {cid!r} (declared: "
                            f"{sorted(component_ids)[:5]}…)")
            if kind == "mateDistance":
                try:
                    d = float(params.get("distance"))
                except (TypeError, ValueError):
                    issues.append(
                        f"Op #{i}: mateDistance distance not numeric")
            if kind == "mateAngle":
                try:
                    a = float(params.get("angle"))
                    if not (-360 <= a <= 360):
                        issues.append(
                            f"Op #{i}: mateAngle angle {a}° out of [-360, 360]")
                except (TypeError, ValueError):
                    issues.append(
                        f"Op #{i}: mateAngle angle not numeric")
            if kind == "mateGear":
                try:
                    r = float(params.get("ratio"))
                    if r == 0:
                        issues.append(f"Op #{i}: mateGear ratio cannot be 0")
                except (TypeError, ValueError):
                    issues.append(
                        f"Op #{i}: mateGear ratio not numeric")
            if kind == "mateSlider":
                ax = (params.get("axis") or "").upper()
                if ax not in ("X", "Y", "Z") and not isinstance(
                        params.get("axis"), list):
                    issues.append(
                        f"Op #{i}: mateSlider axis must be X/Y/Z or "
                        f"a 3-vector (got {params.get('axis')!r})")

        elif kind in ("motionRevolute", "motionPrismatic", "motionContact"):
            if kind != "motionContact":
                joint_ref = params.get("joint")
                if not isinstance(joint_ref, str) or not joint_ref:
                    issues.append(
                        f"Op #{i}: {kind} requires a joint reference")
                else:
                    cid = joint_ref.split(".", 1)[0]
                    if component_ids and cid not in component_ids:
                        issues.append(
                            f"Op #{i}: {kind} joint {joint_ref!r} "
                            f"references unknown component {cid!r}")
            if kind == "motionContact":
                parts = params.get("parts") or []
                if not isinstance(parts, list) or len(parts) < 2:
                    issues.append(
                        f"Op #{i}: motionContact requires ≥2 parts")

        elif kind == "gearFeature":
            try:
                m = float(params.get("module"))
                n = int(params.get("n_teeth"))
                t = float(params.get("thickness"))
                if m <= 0 or n < 4 or t <= 0:
                    issues.append(
                        f"Op #{i}: gearFeature out of range "
                        f"(module={m}, n_teeth={n}, thickness={t})")
            except (TypeError, ValueError):
                issues.append(
                    f"Op #{i}: gearFeature module/n_teeth/thickness not numeric")
            if params.get("sketch") not in sketch_aliases:
                issues.append(
                    f"Op #{i}: gearFeature references unknown sketch "
                    f"{params.get('sketch')!r}")
            saw_new_body = True
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)
                extrude_op[alias] = "new"

    if not saw_new_body:
        issues.append("No operation='new' extrude — the plan never "
                      "creates a body")

    return (not issues), issues
