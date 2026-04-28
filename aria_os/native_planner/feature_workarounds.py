r"""feature_workarounds.py - registry of per-feature plan transforms.

When the SW addin (or any CAD bridge) has a known-broken feature, we don't
fight the bridge - we transform the plan upstream into ops we KNOW work.

This module is the registry. The validator consults it after structural
normalization but before dispatch:

    plan = apply_workarounds(plan, ledger=ledger_mod.load())

Each entry: feature_key -> transform(plan, op_index) -> new_plan_segment.

The transform is only invoked if:
  1. The op kind matches the trigger.
  2. The ledger says status in ("needs_workaround", "unsupported", "flaky").
"""
from __future__ import annotations

import math
from typing import Callable

# A workaround transform takes a single op + the ledger entry and returns
# a list of replacement ops. Returning [op] means "no change".
Transform = Callable[[dict, dict], list[dict]]


def _wa_linear_pattern(op: dict, ledger_entry: dict) -> list[dict]:
    """Expand linearPattern op into N explicit copies along the spacing dir.

    Input op shape (planner-emitted):
      {kind: "linearPattern",
       params: {feature: "<seed_alias>", count_x, count_y,
                spacing_x, spacing_y, seed_x, seed_y, seed_r}}
    """
    p = op.get("params", {})
    n_x = int(p.get("count_x", p.get("count", 1)))
    n_y = int(p.get("count_y", 1))
    sx = float(p.get("spacing_x", 0))
    sy = float(p.get("spacing_y", 0))
    base_x = float(p.get("seed_x", 0))
    base_y = float(p.get("seed_y", 0))
    seed_r = float(p.get("seed_r", 0))
    distance = float(p.get("distance", p.get("seed_height", 10)))
    out = []
    for ix in range(n_x):
        for iy in range(n_y):
            if ix == 0 and iy == 0:
                continue  # seed already exists
            x = base_x + ix * sx
            y = base_y + iy * sy
            sk = f"_lp_{ix}_{iy}"
            out.append({"kind": "newSketch",
                        "params": {"plane": "XY", "alias": sk}})
            out.append({"kind": "sketchCircle",
                        "params": {"sketch": sk, "cx": x, "cy": y,
                                   "r": seed_r}})
            out.append({"kind": "extrude",
                        "params": {"sketch": sk, "distance": distance,
                                   "operation": "cut",
                                   "alias": f"_lpc_{ix}_{iy}"}})
    return out  # original op DROPPED; replacement is the explicit cuts


def _wa_mirror_pattern(op: dict, ledger_entry: dict) -> list[dict]:
    """Expand mirror op (about XY/XZ/YZ plane) into a single mirrored copy.

    Input op shape:
      {kind: "mirror",
       params: {feature: "<seed_alias>", plane: "XZ" | "YZ" | "XY",
                seed_x, seed_y, seed_r, seed_height}}
    """
    p = op.get("params", {})
    plane = p.get("plane", "XZ")
    base_x = float(p.get("seed_x", 0))
    base_y = float(p.get("seed_y", 0))
    seed_r = float(p.get("seed_r", 0))
    distance = float(p.get("distance", p.get("seed_height", 10)))
    if plane == "XZ":  # mirror across XZ flips Y
        x, y = base_x, -base_y
    elif plane == "YZ":  # mirror across YZ flips X
        x, y = -base_x, base_y
    else:  # XY mirror flips Z - rare for surface ops
        x, y = base_x, base_y
    out = [
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "_mr"}},
        {"kind": "sketchCircle", "params": {"sketch": "_mr", "cx": x,
                                              "cy": y, "r": seed_r}},
        {"kind": "extrude", "params": {"sketch": "_mr", "distance": distance,
                                        "operation": "cut", "alias": "_mrc"}},
    ]
    return out


def _wa_sheet_metal_base_flange(op: dict, ledger_entry: dict) -> list[dict]:
    """Emit a thin solid extrude in place of a true sheet-metal base flange.

    SW2024 IDispatch silently returns null from InsertSheetMetalBaseFlange[2]
    even when reflection finds the method (same wall as Shell). For a flat
    base flange with no edge flanges/hems the geometry is identical to a
    thin extrude of thickness=t in the sheet-metal-thickness direction.

    The bend_radius_mm and k_factor params are recorded in the alias name
    so a later op (or a downstream sheet-metal-aware tool) can rebuild the
    bend table from the flat extrude if needed. Today nothing consumes
    that, but the metadata is preserved for the future.

    Input op shape:
      {kind: "sheetMetalBaseFlange",
       params: {sketch: "<alias>", thickness_mm, bend_radius_mm?,
                k_factor?, alias?}}
    """
    p = op.get("params", {})
    sketch = p.get("sketch")
    thickness_mm = float(p.get("thickness_mm", 1.5))
    alias = p.get("alias", "smbase")
    if not sketch:
        return [op]
    return [{
        "kind": "extrude",
        "params": {
            "sketch": sketch,
            "distance": thickness_mm,
            "operation": "new",
            "alias": alias,
            # Preserve sheet-metal metadata on the resulting body so a
            # downstream tool that understands SM can flatten/unfold it.
            "sm_thickness_mm": thickness_mm,
            "sm_bend_radius_mm": float(p.get("bend_radius_mm", 1.0)),
            "sm_k_factor": float(p.get("k_factor", 0.5)),
        },
    }]


def _wa_hole_wizard(op: dict, ledger_entry: dict) -> list[dict]:
    """Emit explicit cut-extrudes in place of HoleWizard5.

    HoleWizard requires a face selection + sketch point + complex args
    that vary per SW interop. The geometry produced is identical to:
      - drill: 1 cylindrical cut at (x,y) of given diameter+depth
      - cbore: stepped cut — larger cylinder on top + smaller drill below
      - csk:   chamfer-mouth cut (approximated as a stepped 2-cut)

    The semantic info (size class, fit, thread) is preserved on the alias
    name so a future SM-aware exporter could rebuild thread callouts.

    Input op shape:
      {kind: "holeWizard",
       params: {x, y, diameter, depth, type: "drill|cbore|csk",
                cbore_diameter?, cbore_depth?,
                csk_diameter?, csk_angle_deg?,
                alias?, plane?}}
    """
    p = op.get("params", {})
    htype = (p.get("type") or "drill").lower()
    plane = p.get("plane", "XY")
    x = float(p.get("x", 0))
    y = float(p.get("y", 0))
    drill_d = float(p.get("diameter", 5))
    drill_h = float(p.get("depth", 10))
    alias = p.get("alias", "hw_hole")
    out: list[dict] = []
    # Counterbore step (executed first — top of hole)
    if htype in ("cbore", "counterbore"):
        cbore_d = float(p.get("cbore_diameter", drill_d * 1.6))
        cbore_h = float(p.get("cbore_depth", drill_h * 0.3))
        sk_cbore = f"_hw_cbore_sk_{alias}"
        out.append({"kind": "newSketch",
                     "params": {"plane": plane, "alias": sk_cbore}})
        out.append({"kind": "sketchCircle",
                     "params": {"sketch": sk_cbore, "cx": x, "cy": y,
                                "r": cbore_d / 2.0}})
        out.append({"kind": "extrude",
                     "params": {"sketch": sk_cbore, "distance": cbore_h,
                                "operation": "cut",
                                "alias": f"{alias}_cbore",
                                "hw_kind": "counterbore",
                                "hw_diameter_mm": cbore_d,
                                "hw_depth_mm": cbore_h}})
    elif htype in ("csk", "countersink"):
        # Approximate countersink mouth as a shallow flat cut, the bevel
        # would need a chamfer feature for a true countersink. Two-cut
        # approximation: shallow wide cut + drill cut.
        csk_d = float(p.get("csk_diameter", drill_d * 1.8))
        csk_h = float(p.get("csk_depth", drill_d * 0.3))
        sk_csk = f"_hw_csk_sk_{alias}"
        out.append({"kind": "newSketch",
                     "params": {"plane": plane, "alias": sk_csk}})
        out.append({"kind": "sketchCircle",
                     "params": {"sketch": sk_csk, "cx": x, "cy": y,
                                "r": csk_d / 2.0}})
        out.append({"kind": "extrude",
                     "params": {"sketch": sk_csk, "distance": csk_h,
                                "operation": "cut",
                                "alias": f"{alias}_csk",
                                "hw_kind": "countersink",
                                "hw_diameter_mm": csk_d,
                                "hw_depth_mm": csk_h}})
    # Drill (always, including for cbore + csk types — the through hole)
    sk_drill = f"_hw_drill_sk_{alias}"
    out.append({"kind": "newSketch",
                 "params": {"plane": plane, "alias": sk_drill}})
    out.append({"kind": "sketchCircle",
                 "params": {"sketch": sk_drill, "cx": x, "cy": y,
                            "r": drill_d / 2.0}})
    out.append({"kind": "extrude",
                 "params": {"sketch": sk_drill, "distance": drill_h,
                            "operation": "cut",
                            "alias": alias,
                            "hw_kind": htype,
                            "hw_diameter_mm": drill_d,
                            "hw_depth_mm": drill_h}})
    return out


def _wa_no_op(op: dict, ledger_entry: dict) -> list[dict]:
    """Identity transform - keeps the op unchanged. Used to register a
    feature_key with the workaround system without actually rewriting it."""
    return [op]


# ---------------------------------------------------------------------------
# Registry: op_kind -> transform_fn
# ---------------------------------------------------------------------------
WORKAROUNDS: dict[str, Transform] = {
    "linearPattern":         _wa_linear_pattern,
    "mirror":                _wa_mirror_pattern,
    # sheetMetalBaseFlange + holeWizard: handled by REAL bridge ops
    # (OpSheetMetalBaseFlange uses InsertSheetMetalBaseFlange2; OpHoleWizard
    # uses HoleWizard5). Both follow the SW SDK macro pattern: sketch
    # opened → AddToDB=true → CreatePoint/Rect → sketch EXITED → re-selected
    # by name → wizard API called. Without that exact sequence, SW2024
    # silently returns null. With it, the real feature shows in the SW tree
    # (Hole Wizard, Sheet Metal) with thread/bend callouts in BOM/drawing.
    # The validator transforms _wa_hole_wizard / _wa_sheet_metal_base_flange
    # remain as available helpers but are NOT registered, so the real ops
    # are reached.
    # circularPattern handled by validator's _expand_circular_pattern_to_explicit_cuts
}

# A status set the workaround triggers on
APPLY_STATUSES = {"needs_workaround", "unsupported", "flaky"}


def apply_workarounds(plan: list[dict],
                       ledger: dict[str, dict] | None = None,
                       force: bool = False) -> list[dict]:
    """Iterate the plan; rewrite any op whose kind is in WORKAROUNDS.

    Registration in WORKAROUNDS is itself the signal that the kind is
    known-broken on at least one bridge — we wouldn't write a transform
    otherwise. The ledger only opts a kind OUT (when status=="ok" the
    transform is skipped because the bridge can handle it natively).

    `force=True` is kept for tests where you want the transform
    regardless of ledger contents.
    """
    ledger = ledger or {}
    out: list[dict] = []
    for op in plan:
        kind = op.get("kind")
        tf = WORKAROUNDS.get(kind)
        if not tf:
            out.append(op)
            continue
        # Default: apply. Only skip if the ledger explicitly says "ok"
        # (or any test_slug variant of the kind says "ok").
        entry = ledger.get(kind) or {}
        ledger_says_ok = entry.get("status") == "ok"
        if not ledger_says_ok:
            for k, v in ledger.items():
                if (kind.lower() in k.lower()
                        and v.get("status") == "ok"):
                    ledger_says_ok = True
                    break
        if force or not ledger_says_ok:
            replacement = tf(op, entry)
            out.extend(replacement)
        else:
            out.append(op)
    return out
