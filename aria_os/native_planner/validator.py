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
    # Editable lattice — same idea as the implicit ops but the SW addin
    # records SDF parameters on user-parameters and re-bakes on change.
    "latticeFeature",
    # Assembly
    "asmBegin", "addComponent", "joint",
    # W4: real assembly mates + motion drivers
    "mateConcentric", "mateCoincident", "mateDistance",
    "mateAngle", "mateGear", "mateSlider", "mateSlot",
    "motionRevolute", "motionPrismatic", "motionContact",
    # Drawing
    "beginDrawing", "newSheet", "addView", "addTitleBlock",
    "drawingAutoDim",
    # W6: extended drawing vocabulary — section/detail/broken views,
    # full GD&T callout chain, weld symbols, revision + BOM tables.
    # Aligned with ASME Y14.5 and ISO 128.
    "sectionView", "detailView", "brokenView",
    "autoDimension", "linearDimension", "angularDimension",
    "diameterDimension", "radialDimension", "ordinateDimension",
    "gdtFrame", "datumLabel", "surfaceFinishCallout",
    "weldSymbol", "revisionTable", "bomTable",
    "centerlineMark", "balloon",
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
    # W7: verification gate
    "verifyPart",
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
    # Editable lattice — re-bakes the SDF on SW user-parameter change.
    # `target` = host body alias; bbox is auto-derived from the host
    # body bounds at bake time so callers don't need to recompute.
    # alias names the resulting lattice body so a fillet / shell op
    # later in the plan can reference it.
    "latticeFeature":   {"target", "pattern", "cell_mm", "wall_mm",
                           "operation", "alias"},
    # W5: sheet metal — body-creating ops mark saw_new_body via the
    # main loop; flange/bend/louver attach to existing edges/faces.
    "sheetMetalBend":    {"edges", "angle"},
    "sheetMetalLouver":  {"face", "n_louvers", "size_mm"},
    "sheetMetalHem":     {"edges", "type"},
    "sheetMetalUnfold":  {"body"},
    "sheetMetalCutout":  {"sketch", "operation"},
    "exportFlatPattern": {"body", "format"},
    # W7: verification gate. Runs DFM + tolerance + drawing audit + (opt)
    # FEA against the parts produced by the rest of the plan. The
    # planner auto-appends one of these per plan unless suppressed.
    "verifyPart":  {"process"},
    # W6: drawing ops — most reference an existing view alias declared
    # by addView. The view alias scope is checked in the main loop.
    "sectionView":      {"sheet", "source_view", "section_line"},
    "detailView":       {"sheet", "source_view", "center", "radius"},
    "brokenView":       {"sheet", "source_view", "break_line"},
    "autoDimension":    {"view"},
    "linearDimension":  {"view", "from", "to"},
    "angularDimension": {"view", "edges"},
    "diameterDimension": {"view", "edge"},
    "radialDimension":  {"view", "edge"},
    "ordinateDimension": {"view", "from_origin", "to"},
    "gdtFrame":         {"view", "feature", "characteristic", "tolerance"},
    "datumLabel":       {"view", "feature", "label"},
    "surfaceFinishCallout": {"view", "feature", "ra"},
    "weldSymbol":       {"view", "edge", "type"},
    "revisionTable":    {"sheet"},
    "bomTable":         {"sheet"},
    "centerlineMark":   {"view", "feature"},
    "balloon":          {"view", "component", "number"},
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


# ---------------------------------------------------------------------------
# Pre-validation normalization
#
# LLM planners drift on naming. Two common drifts hit hard:
#   1. plane spec — "Top", "Front", "horizontal", "X-Y", "XY plane",
#      "+Z" — all valid English, none accepted by the executor which
#      wants strictly "XY"/"XZ"/"YZ".
#   2. param-name aliases — "radius"/"diameter"/"d" instead of "r";
#      "width"/"height" instead of "w"/"h"; "depth"/"length" instead
#      of "distance"; "type"/"mode" instead of "operation".
#
# Reject-and-regenerate works but burns tokens and time on something
# we can fix locally. Per the autonomy-first rule: build the next
# recovery layer into the code. We rewrite ambiguous params to their
# canonical form before validation, so a 1-token slip never aborts a
# full plan. If the rewrite changes a value (e.g. diameter → radius),
# we record it on the op as `_normalized` for downstream tracing.
# ---------------------------------------------------------------------------

# Plane name + axis shorthand -> canonical XY/XZ/YZ. Aligned with the
# SW addin's plane mapping (SwPlaneName in AriaSwAddin.cs):
#   XY -> Front Plane   (normal Z, "lay flat" orientation)
#   XZ -> Top Plane     (normal Y, looking down)
#   YZ -> Right Plane   (normal X, side view)
_PLANE_ALIASES = {
    # Direct canonical (idempotent)
    "XY": "XY", "XZ": "XZ", "YZ": "YZ",
    "X-Y": "XY", "X-Z": "XZ", "Y-Z": "YZ",
    "XY PLANE": "XY", "XZ PLANE": "XZ", "YZ PLANE": "YZ",
    # SW / Fusion / Onshape canonical plane names
    "FRONT": "XY", "FRONT PLANE": "XY",
    "TOP":   "XZ", "TOP PLANE":   "XZ",
    "RIGHT": "YZ", "RIGHT PLANE": "YZ",
    "SIDE":  "YZ", "SIDE PLANE":  "YZ",
    # English orientation hints
    "HORIZONTAL": "XZ", "GROUND": "XZ", "FLOOR": "XZ",
    "VERTICAL":   "XY",
    "LATERAL":    "YZ",
    # Axis normals — picking the plane whose normal is the named axis
    "+Z": "XY", "-Z": "XY", "Z": "XY",
    "+Y": "XZ", "-Y": "XZ", "Y": "XZ",
    "+X": "YZ", "-X": "YZ", "X": "YZ",
}

# Param-name aliases per op. Each entry says "if you see <alias>, treat
# it as <canonical>; if the value needs unit conversion (e.g. diameter
# halved into radius), the convert callable handles that."
_PARAM_ALIASES_PER_KIND = {
    "sketchCircle": {
        "radius":      ("r", None),
        "rad":         ("r", None),
        "r_mm":        ("r", None),
        "diameter":    ("r", lambda v: v / 2.0),
        "diameter_mm": ("r", lambda v: v / 2.0),
        "dia":         ("r", lambda v: v / 2.0),
        "d":           ("r", lambda v: v / 2.0),
    },
    "sketchRect": {
        "width":     ("w", None),
        "width_mm":  ("w", None),
        "x":         ("w", None),
        "height":    ("h", None),
        "height_mm": ("h", None),
        "y":         ("h", None),
        "length":    ("w", None),  # ambiguous but "length" reads as horizontal
    },
    "extrude": {
        "depth":     ("distance", None),
        "depth_mm":  ("distance", None),
        "length":    ("distance", None),
        "length_mm": ("distance", None),
        "height":    ("distance", None),
        "height_mm": ("distance", None),
        "thickness": ("distance", None),
        "type":      ("operation", None),
        "mode":      ("operation", None),
    },
    "fillet": {
        "radius":   ("r", None),
        "rad":      ("r", None),
        "r_mm":     ("r", None),
    },
    "circularPattern": {
        "n":           ("count", None),
        "instances":   ("count", None),
        "occurrences": ("count", None),
    },
    "addComponent": {
        "name":   ("id", None),
        "kind":   ("type", None),
    },
}

# Normalize the value side of `operation` in extrude ops.
_EXTRUDE_OPERATION_ALIASES = {
    "add":      "new",  "boss":   "new",  "create": "new",
    "remove":   "cut",  "subtract": "cut", "hole":  "cut",
    "merge":    "join",
    "join":     "join",
    "intersect": "intersect",
    "new":      "new",
    "cut":      "cut",
}


# Ops that MUST reference a sketch alias. When the LLM forgets the
# `sketch` field but emitted a fresh sketch immediately before, we can
# infer it from context. Saves the "Op #4 (extrude): missing params
# ['sketch']" failure mode that exhausted all 3 LLM tiers in live tests.
_SKETCH_REFERENCING_OPS = {
    "extrude", "revolve", "sweep", "rib", "gearFeature",
    "sketchCircle", "sketchRect", "sketchSpline", "sketchPolyline",
    "sketchTangentArc", "sketchOffset", "sketchProjection",
    "sketchEquationCurve", "sheetMetalBase", "sheetMetalCutout",
}


# Body-creating sketch consumers — once one of these takes a sketch,
# emitting another body-creating op without an explicit sketch ref
# means the LLM forgot to start a fresh sketch first. These get
# inferred-from-context only the FIRST time the most-recent sketch
# is used; later body ops still need an explicit ref.
_BODY_CREATING_SKETCH_OPS = {
    "extrude", "revolve", "sweep", "rib", "gearFeature",
    "sheetMetalBase", "sheetMetalCutout",
}


def _expand_circular_pattern_to_explicit_cuts(
    plan: list[dict]) -> list[dict]:
    """Replace each `circularPattern` op with N-1 discrete sketch+extrude
    ops at rotated positions.

    Background: SW2024's COM IDispatch silently rejects
    FeatureCircularPattern5 even with selection state correct (verified
    via probe / addin.log — see feedback_sw2024_idispatch_quirks). The
    architectural fix is to skip the pattern op entirely and emit each
    rotated cut as a discrete sketch+cut sequence — extrude works
    flawlessly across all SW versions and all CAD bridges.

    Looks up the seed feature in the plan to extract:
      - sketch alias the seed used → look up its sketchCircle's center
        (cx, cy) and radius (or sketchRect's center + size)
      - extrude distance + operation
      - extrusion plane

    Then emits N-1 copies at theta = (2π * i / count) for i in 1..N-1.
    The original seed (i=0) stays in place.

    If we can't recover the seed geometry from the plan (e.g. the seed
    is from an external alias map, or uses unsupported sketch primitives),
    fall back to the legacy _heal_dangling_pattern_refs behavior — drop
    the pattern op rather than fail the build.
    """
    import math
    if not plan:
        return plan
    # Build a lookup: feature_alias → (sketch_alias, extrude_op_index)
    extrude_by_alias: dict[str, int] = {}
    sketch_by_alias: dict[str, int] = {}
    for i, op in enumerate(plan):
        kind = op.get("kind")
        params = op.get("params") or {}
        alias = params.get("alias")
        if not alias:
            continue
        if kind == "extrude":
            extrude_by_alias[alias] = i
        elif kind == "newSketch":
            sketch_by_alias[alias] = i
    out: list[dict] = []
    for op in plan:
        kind = op.get("kind")
        params = op.get("params") or {}
        if kind != "circularPattern":
            out.append(op)
            continue
        feat_alias = params.get("feature")
        count = int(params.get("count", 0))
        if not feat_alias or count < 2:
            # Bad pattern op — drop it (legacy behavior).
            op.setdefault("_normalized", []).append(
                f"dropped: circularPattern missing feature/count")
            continue
        # Find the seed extrude op.
        idx = extrude_by_alias.get(feat_alias)
        if idx is None:
            op.setdefault("_normalized", []).append(
                f"dropped: circularPattern references unknown feature "
                f"{feat_alias!r} (no extrude with that alias)")
            continue
        seed_extrude = plan[idx]
        seed_params = seed_extrude.get("params") or {}
        seed_sketch_alias = seed_params.get("sketch")
        operation = seed_params.get("operation", "cut")
        distance = float(seed_params.get("distance", 0.0))
        # Find the seed sketch op (must be a sketchCircle or sketchRect).
        sk_idx = sketch_by_alias.get(seed_sketch_alias)
        if sk_idx is None:
            op.setdefault("_normalized", []).append(
                f"dropped: circularPattern seed sketch "
                f"{seed_sketch_alias!r} not found")
            continue
        plane = (plan[sk_idx].get("params") or {}).get("plane", "XY")
        # Find the FIRST primitive in this sketch (sketchCircle or
        # sketchRect) by scanning forward from sk_idx until we hit the
        # next op that's not a sketch primitive.
        cx0 = cy0 = 0.0
        radius = 0.0
        rect_w = rect_h = None
        primitive_kind = None
        for j in range(sk_idx + 1, idx):
            jop = plan[j]
            jkind = jop.get("kind")
            jparams = jop.get("params") or {}
            if jkind == "sketchCircle":
                cx0 = float(jparams.get("cx", 0.0))
                cy0 = float(jparams.get("cy", 0.0))
                radius = float(jparams.get("r",
                    jparams.get("radius",
                        float(jparams.get("diameter", 0.0)) / 2)))
                primitive_kind = "circle"
                break
            if jkind == "sketchRect":
                cx0 = float(jparams.get("cx", 0.0))
                cy0 = float(jparams.get("cy", 0.0))
                rect_w = float(jparams.get("w", 0.0))
                rect_h = float(jparams.get("h", 0.0))
                primitive_kind = "rect"
                break
        if primitive_kind is None:
            op.setdefault("_normalized", []).append(
                f"dropped: circularPattern seed sketch primitive "
                f"unsupported (only sketchCircle/sketchRect supported)")
            continue
        # Phase angle of the seed.
        phase = math.atan2(cy0, cx0) if (cx0 != 0 or cy0 != 0) else 0.0
        rRot = math.sqrt(cx0 * cx0 + cy0 * cy0)
        # Mark the original op as expanded (for the trace) and emit N-1
        # rotated copies.
        op.setdefault("_normalized", []).append(
            f"expanded: circularPattern × {count} → {count - 1} "
            f"explicit {primitive_kind} cuts (SW2024 IDispatch workaround)")
        # The original seed extrude is already in `out` (we appended
        # everything before this circularPattern op above). Now emit
        # N-1 more.
        for i in range(1, count):
            theta = phase + 2 * math.pi * i / count
            cx = rRot * math.cos(theta)
            cy = rRot * math.sin(theta)
            sk_alias_i = f"{seed_sketch_alias}_p{i}"
            ext_alias_i = f"{feat_alias}_p{i}"
            out.append({
                "kind": "newSketch",
                "params": {"plane": plane, "alias": sk_alias_i,
                            "name": f"ARIA pattern {i+1}/{count}"},
                "label": (f"Pattern {i+1}/{count}: sketch on {plane}"),
            })
            if primitive_kind == "circle":
                out.append({
                    "kind": "sketchCircle",
                    "params": {"sketch": sk_alias_i,
                                "cx": cx, "cy": cy, "r": radius},
                    "label": (f"Pattern {i+1}/{count}: Ø{radius*2:g}mm "
                               f"at ({cx:+.1f}, {cy:+.1f})"),
                })
            else:
                # For rect, rotating the center but keeping the rect
                # axis-aligned — visual approximation; proper rotation
                # would need a rotated polyline.
                out.append({
                    "kind": "sketchRect",
                    "params": {"sketch": sk_alias_i,
                                "cx": cx, "cy": cy,
                                "w": rect_w, "h": rect_h},
                    "label": (f"Pattern {i+1}/{count}: rect {rect_w:g}×"
                               f"{rect_h:g} at ({cx:+.1f}, {cy:+.1f})"),
                })
            out.append({
                "kind": "extrude",
                "params": {"sketch": sk_alias_i, "distance": distance,
                            "operation": operation,
                            "alias": ext_alias_i},
                "label": (f"Pattern {i+1}/{count}: extrude {operation} "
                           f"{distance:g}mm"),
            })
    return out


def _heal_dangling_pattern_refs(plan: list[dict]) -> list[dict]:
    """Drop circularPattern / mirrorPattern ops that reference a
    feature alias not declared elsewhere in the plan.

    Why: LLMs commonly generate "patternCircular(feature='keyway_cutout')"
    after emitting only a sketch op named keyway_cutout (not a real
    feature) — or after dropping the source feature from the plan
    entirely. Validator catches it but the whole plan ends up rejected.
    Better self-heal: drop the dangling pattern op and continue.
    """
    if not plan:
        return plan
    feature_aliases: set[str] = set()
    for op in plan:
        kind = op.get("kind")
        params = op.get("params") or {}
        # An op declares a feature alias whenever it has alias= AND it's
        # a feature-creating kind (anything that lands in the SW tree).
        alias = params.get("alias")
        if alias and kind not in (
                "newSketch", "sketchCircle", "sketchRect",
                "sketchPolyline", "sketchSpline", "sketchTangentArc",
                "sketchOffset", "sketchProjection",
                "sketchEquationCurve"):
            feature_aliases.add(alias)
    out: list[dict] = []
    for op in plan:
        kind = op.get("kind")
        params = op.get("params") or {}
        if kind in ("circularPattern", "mirrorPattern", "linearPattern"):
            ref = params.get("feature")
            if ref and ref not in feature_aliases:
                op.setdefault("_normalized", []).append(
                    f"dropped: circularPattern references unknown "
                    f"feature {ref!r} — auto-pruned to keep plan valid")
                # Skip emitting this op entirely.
                continue
        out.append(op)
    return out


def _heal_missing_sketch_refs(plan: list[dict]) -> list[dict]:
    """If an op like extrude/revolve/sketchCircle is missing its `sketch`
    field, infer it from the most-recent newSketch alias declared
    earlier in the plan.

    Why: LLMs in fast/balanced tiers commonly emit extrude immediately
    after newSketch and forget the back-reference field. The intent is
    obvious — use the sketch that was just created. Without this layer
    the planner exhausts all 3 tiers, the user sees nothing built, and
    has to retry by hand. See live test "stepped shaft 200mm long..."
    where 3 of the 11 extrudes hit this exact failure.
    """
    if not plan:
        return plan
    last_sketch_alias: str | None = None
    # Track which sketches have already been consumed by a body-creating
    # op (extrude / revolve / etc.). sketchCircle / sketchRect / etc.
    # POPULATE a sketch — they don't consume it, so re-using the same
    # alias for the eventual extrude is correct CAD semantics.
    body_consumed: set[str] = set()
    for op in plan:
        kind = op.get("kind")
        params = op.get("params")
        if not isinstance(params, dict):
            continue
        if kind == "newSketch":
            alias = params.get("alias")
            if isinstance(alias, str) and alias:
                last_sketch_alias = alias
            continue
        if kind in _SKETCH_REFERENCING_OPS:
            if params.get("sketch"):
                if kind in _BODY_CREATING_SKETCH_OPS:
                    body_consumed.add(str(params["sketch"]))
                continue
            if last_sketch_alias is None:
                continue
            # For body-creating ops, only infer if the candidate sketch
            # hasn't already been consumed by an earlier body op.
            if kind in _BODY_CREATING_SKETCH_OPS \
                    and last_sketch_alias in body_consumed:
                continue
            params["sketch"] = last_sketch_alias
            op["params"] = params
            op.setdefault("_normalized", []).append(
                f"sketch<-{last_sketch_alias} (auto-inferred)")
            if kind in _BODY_CREATING_SKETCH_OPS:
                body_consumed.add(last_sketch_alias)
    return plan


def _normalize_plan(plan: list[dict]) -> list[dict]:
    """Mutate each op's params in-place to canonical form. Returns plan
    so callers can chain. Records changes on op["_normalized"]."""
    if not plan:
        return plan
    for op in plan:
        kind = op.get("kind")
        params = op.get("params") or {}
        changes: list[str] = []

        # 1. Plane normalization (only meaningful on newSketch)
        if kind == "newSketch":
            plane = params.get("plane")
            if isinstance(plane, str):
                key = plane.strip().upper()
                canon = _PLANE_ALIASES.get(key)
                if canon and canon != plane:
                    params["plane"] = canon
                    changes.append(f"plane:{plane!r}->{canon!r}")

        # 2. Param-name aliases per kind
        aliases = _PARAM_ALIASES_PER_KIND.get(kind, {})
        for src, (dst, convert) in list(aliases.items()):
            if src in params and dst not in params:
                v = params.pop(src)
                if convert is not None:
                    try:
                        v = convert(float(v))
                    except (TypeError, ValueError):
                        # Leave as-is; the validator will catch it.
                        pass
                params[dst] = v
                changes.append(f"{src}->{dst}")

        # 3. Extrude operation value normalization
        if kind == "extrude" and "operation" in params:
            v = params["operation"]
            if isinstance(v, str):
                canon = _EXTRUDE_OPERATION_ALIASES.get(v.lower())
                if canon and canon != v:
                    params["operation"] = canon
                    changes.append(f"operation:{v!r}->{canon!r}")

        if changes:
            op.setdefault("_normalized", []).extend(changes)
        op["params"] = params

    # 4. Cross-op heal: fill missing sketch back-references from the
    # last-declared newSketch alias. Runs LAST so it sees the
    # post-aliasing param shape.
    plan = _heal_missing_sketch_refs(plan)
    # 5. Auto-expand circularPattern → N-1 explicit cut-extrudes. SW2024
    # silently rejects FeatureCircularPattern5; expanding upstream keeps
    # all CAD bridges happy with one code path. Patterns whose seed
    # geometry can't be recovered fall through to the dangling-ref drop.
    plan = _expand_circular_pattern_to_explicit_cuts(plan)
    # 6. Apply ledger-driven workarounds (linearPattern, mirror -> explicit
    # cuts when ledger marks them needs_workaround). This is the
    # generalization of the circular pattern fix - any op the bridge can't
    # do reliably gets rewritten upstream.
    try:
        from aria_os.native_planner import feature_workarounds
        import json
        from pathlib import Path
        ledger_path = (Path(__file__).resolve().parents[2]
                       / "outputs" / "sw_learning_ledger.json")
        ledger = (json.loads(ledger_path.read_text(encoding="utf-8"))
                  if ledger_path.exists() else {})
        plan = feature_workarounds.apply_workarounds(plan, ledger=ledger)
    except Exception:
        pass  # workarounds are best-effort; never break the validator
    # 7. Drop any remaining dangling pattern refs (mirror / linear,
    # plus circularPattern that the expander couldn't recover).
    plan = _heal_dangling_pattern_refs(plan)
    return plan


def validate_plan(plan: list[dict]) -> tuple[bool, list[str]]:
    """Structural check. Returns (ok, list_of_issues). Empty issues == ok.

    Calls `_normalize_plan` first so plane/param drift never fails
    validation when the intent was unambiguous.
    """
    plan = _normalize_plan(plan)
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
    # W6: drawing scope — sheet + view aliases tracked so dim/GD&T
    # ops can be checked against the views they reference.
    sheet_aliases: set[str] = set()
    view_aliases: set[str] = set()
    in_drawing_mode = False
    # GD&T characteristic vocabulary — ASME Y14.5 + ISO 1101.
    valid_gdt_chars = {
        "flatness", "straightness", "circularity", "cylindricity",
        "perpendicularity", "parallelism", "angularity",
        "position", "concentricity", "symmetry",
        "profile_of_a_line", "profile_of_a_surface",
        "circular_runout", "total_runout",
    }
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

        # W7: verification gate. Doesn't add a body or feature —
        # just declares a check that runs after geometry is meshed.
        elif kind == "verifyPart":
            proc = (params.get("process") or "").lower()
            try:
                from aria_os.verification.dfm import available_processes
                valid = set(available_processes())
            except Exception:
                valid = {"cnc_3axis", "sheet_metal", "fdm",
                          "sla", "casting", "injection_mold"}
            if proc and proc not in valid:
                issues.append(
                    f"Op #{i}: verifyPart process {proc!r} not in "
                    f"{sorted(valid)[:6]}…")

        # W6: drawing scope ops.
        elif kind == "beginDrawing":
            in_drawing_mode = True
            saw_new_body = True

        elif kind == "newSheet":
            alias = params.get("alias")
            if alias:
                if alias in sheet_aliases:
                    issues.append(
                        f"Op #{i}: sheet alias {alias!r} already declared")
                sheet_aliases.add(alias)

        elif kind == "addView":
            sheet = params.get("sheet")
            if sheet_aliases and sheet not in sheet_aliases:
                issues.append(
                    f"Op #{i}: addView references unknown sheet "
                    f"{sheet!r} — emit newSheet first")
            alias = params.get("alias")
            if alias:
                if alias in view_aliases:
                    issues.append(
                        f"Op #{i}: view alias {alias!r} already declared")
                view_aliases.add(alias)

        elif kind in ("sectionView", "detailView", "brokenView"):
            # Each derived view declares its own alias and references
            # an existing source_view that addView declared earlier.
            src = params.get("source_view")
            if view_aliases and src not in view_aliases:
                issues.append(
                    f"Op #{i}: {kind} source_view {src!r} not declared "
                    f"(known: {sorted(view_aliases)[:5]}…)")
            alias = params.get("alias")
            if alias:
                view_aliases.add(alias)
            if kind == "detailView":
                try:
                    r = float(params.get("radius"))
                    if r <= 0:
                        issues.append(
                            f"Op #{i}: detailView radius must be > 0")
                except (TypeError, ValueError):
                    issues.append(
                        f"Op #{i}: detailView radius not numeric")

        elif kind == "autoDimension":
            v = params.get("view")
            if view_aliases and v not in view_aliases:
                issues.append(
                    f"Op #{i}: autoDimension view {v!r} unknown")

        elif kind in ("linearDimension", "angularDimension",
                      "diameterDimension", "radialDimension",
                      "ordinateDimension"):
            v = params.get("view")
            if view_aliases and v not in view_aliases:
                issues.append(
                    f"Op #{i}: {kind} view {v!r} unknown")

        elif kind == "gdtFrame":
            v = params.get("view")
            if view_aliases and v not in view_aliases:
                issues.append(
                    f"Op #{i}: gdtFrame view {v!r} unknown")
            char = (params.get("characteristic") or "").lower()
            if char not in valid_gdt_chars:
                issues.append(
                    f"Op #{i}: gdtFrame characteristic {params.get('characteristic')!r} "
                    "not in ASME Y14.5 set "
                    f"(valid: {sorted(valid_gdt_chars)[:6]}…)")
            try:
                tol = float(params.get("tolerance"))
                if tol <= 0:
                    issues.append(
                        f"Op #{i}: gdtFrame tolerance must be > 0")
            except (TypeError, ValueError):
                issues.append(
                    f"Op #{i}: gdtFrame tolerance not numeric")

        elif kind == "datumLabel":
            v = params.get("view")
            if view_aliases and v not in view_aliases:
                issues.append(
                    f"Op #{i}: datumLabel view {v!r} unknown")
            label = (params.get("label") or "").strip().upper()
            # ASME datums are single uppercase letters A-Z, sometimes A1/A2 etc
            if not label or not label[0].isalpha() or len(label) > 3:
                issues.append(
                    f"Op #{i}: datumLabel label {params.get('label')!r} "
                    "should be a single letter (A-Z) or letter+digit (A1, A2)")

        elif kind == "surfaceFinishCallout":
            v = params.get("view")
            if view_aliases and v not in view_aliases:
                issues.append(
                    f"Op #{i}: surfaceFinishCallout view {v!r} unknown")
            try:
                ra = float(params.get("ra"))
                if not (0.025 <= ra <= 50):
                    issues.append(
                        f"Op #{i}: Ra {ra}µm out of practical range "
                        "[0.025, 50] (ISO 1302)")
            except (TypeError, ValueError):
                issues.append(
                    f"Op #{i}: surfaceFinishCallout Ra not numeric")

        elif kind == "weldSymbol":
            wt = (params.get("type") or "").lower()
            valid = {"fillet", "groove", "spot", "seam", "plug",
                     "bevel", "j", "u", "v", "square"}
            if wt not in valid:
                issues.append(
                    f"Op #{i}: weldSymbol type {params.get('type')!r} "
                    f"not in AWS A2.4 set (valid: {sorted(valid)[:6]}…)")

        elif kind in ("revisionTable", "bomTable"):
            sheet = params.get("sheet")
            if sheet_aliases and sheet not in sheet_aliases:
                issues.append(
                    f"Op #{i}: {kind} sheet {sheet!r} unknown")

        elif kind == "balloon":
            v = params.get("view")
            if view_aliases and v not in view_aliases:
                issues.append(f"Op #{i}: balloon view {v!r} unknown")
            try:
                n = int(params.get("number"))
                if n < 1:
                    issues.append(f"Op #{i}: balloon number must be ≥ 1")
            except (TypeError, ValueError):
                issues.append(f"Op #{i}: balloon number not integer")

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
