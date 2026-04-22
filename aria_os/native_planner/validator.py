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
}


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

    if not saw_new_body:
        issues.append("No operation='new' extrude — the plan never "
                      "creates a body")

    return (not issues), issues
