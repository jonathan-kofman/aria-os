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
    # Assembly
    "asmBegin", "addComponent", "joint",
    # Drawing
    "beginDrawing", "newSheet", "addView", "addTitleBlock",
    "drawingAutoDim",
    # Fusion Electronics
    "beginElectronics", "placeSymbol", "placeFootprint",
    "addConnection", "boardOutline",
    # Fusion native-leverage
    "addParameter", "openGenerativeDesign", "createCAMSetup",
    "createMotionStudy", "sheetMetalBase", "sheetMetalFlange",
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
}

# Operation modes accepted by ops that combine with existing geometry.
_BOOLEAN_OPS = ("new", "cut", "join", "intersect")


def validate_plan(plan: list[dict]) -> tuple[bool, list[str]]:
    """Structural check. Returns (ok, list_of_issues). Empty issues == ok."""
    issues: list[str] = []
    if not plan:
        return False, ["Plan is empty"]
    if plan[0].get("kind") != "beginPlan":
        issues.append(
            f"First op must be beginPlan (got {plan[0].get('kind')!r})")

    sketch_aliases: set[str] = set()
    feature_aliases: set[str] = set()
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
            else:  # coil — needs profile section
                if params.get("section") not in sketch_aliases:
                    issues.append(
                        f"Op #{i}: coil section {params.get('section')!r} unknown")
            alias = params.get("alias")
            if alias:
                feature_aliases.add(alias)

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
