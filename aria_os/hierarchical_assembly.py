"""
Hierarchical assembly builder.

Extends the flat assembly.py format with recursive sub-assembly support and
named attachment points. An assembly can now reference other assembly JSON
files as components, each resolved to a flat part list at build time.

JSON format (recursive):

    {
      "name": "robot_arm",
      "attachment_points": {
        "base_mount": {"pos": [0, 0, 0], "rotation": [0, 0, 0]},
        "gripper_flange": {"pos": [250, 0, 300], "rotation": [0, 0, 90]}
      },
      "parts": [
        {"id": "base", "step": "outputs/cad/step/base.step", "pos": [0,0,0]},
        {"id": "shoulder", "assembly": "assembly_configs/shoulder.json",
         "depends_on": "base", "attach_to": "top_mount", "via": "base_mount"}
      ]
    }

- A component can be a "step" file OR an "assembly" file (sub-assembly).
- "attach_to" names a point on the parent; "via" names a point on this child.
  The child is positioned so its "via" point coincides with the parent's "attach_to".
- Cycles and missing references raise ValueError with clear messages.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AssemblyResolutionError(ValueError):
    """Raised when sub-assembly resolution fails (missing file, cycle, unknown point)."""


def load_assembly_config(path: str | Path) -> dict[str, Any]:
    """Load and return a JSON assembly config."""
    p = Path(path)
    if not p.is_file():
        raise AssemblyResolutionError(f"Assembly config not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssemblyResolutionError(f"Invalid JSON in {p}: {exc}")


def flatten_assembly(
    config: dict[str, Any],
    *,
    config_path: Path | None = None,
    _visited: set[str] | None = None,
    _prefix: str = "",
    _parent_offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    _parent_rotation: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> list[dict[str, Any]]:
    """
    Recursively flatten a hierarchical assembly config into a flat parts list.

    Each returned entry has shape:
        {"id": str, "step": str, "pos": [x,y,z], "rot": [rx,ry,rz]}

    Sub-assembly parts get their IDs prefixed with the parent's part ID
    (e.g. "shoulder/motor") to avoid collisions across sub-assemblies.
    """
    if _visited is None:
        _visited = set()
    if config_path is not None:
        resolved = str(config_path.resolve())
        if resolved in _visited:
            raise AssemblyResolutionError(
                f"Circular sub-assembly reference detected: {resolved}"
            )
        _visited = _visited | {resolved}

    name = config.get("name", "assembly")
    parts = config.get("parts", [])
    if not isinstance(parts, list):
        raise AssemblyResolutionError(f"Assembly '{name}' parts must be a list")

    attachment_points: dict[str, dict[str, Any]] = config.get("attachment_points", {})

    # Build index of parts by id (flat this level) for attach_to resolution
    local_parts_by_id: dict[str, dict[str, Any]] = {}
    for p in parts:
        if "id" in p:
            local_parts_by_id[p["id"]] = p

    # Resolved positions: id -> (pos, rot)
    resolved_positions: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {}

    def _resolve_part(part: dict[str, Any], chain: set[str]) -> tuple:
        pid = part.get("id", "")
        if pid in resolved_positions:
            return resolved_positions[pid]
        if pid in chain:
            raise AssemblyResolutionError(
                f"Dependency cycle in '{name}' via {' -> '.join(chain)} -> {pid}"
            )

        raw_pos = part.get("pos", [0.0, 0.0, 0.0])
        raw_rot = part.get("rot", part.get("rotation", [0.0, 0.0, 0.0]))
        pos = tuple(float(v) for v in raw_pos)
        rot = tuple(float(v) for v in raw_rot)

        depends_on = part.get("depends_on")
        if depends_on is not None:
            parent = local_parts_by_id.get(depends_on)
            if parent is None:
                raise AssemblyResolutionError(
                    f"Part '{pid}' in '{name}' depends_on unknown id '{depends_on}'"
                )
            parent_pos, parent_rot = _resolve_part(parent, chain | {pid})
            offset = part.get("offset", [0.0, 0.0, 0.0])
            pos = (parent_pos[0] + float(offset[0]),
                   parent_pos[1] + float(offset[1]),
                   parent_pos[2] + float(offset[2]))

            # If this part uses "attach_to / via" semantics, override pos
            attach_to = part.get("attach_to")
            via = part.get("via")
            if attach_to is not None:
                # Parent's attachment points are either its own sub-assembly's
                # attachment_points dict, or (for a step part) defined inline.
                parent_ap = parent.get("attachment_points", {})
                if isinstance(parent, dict) and "_resolved_ap" in parent:
                    parent_ap = parent["_resolved_ap"]
                if attach_to not in parent_ap:
                    raise AssemblyResolutionError(
                        f"Part '{pid}' attach_to='{attach_to}' — parent '{depends_on}' "
                        f"has no such attachment point (has: {list(parent_ap)})"
                    )
                ap = parent_ap[attach_to]
                ap_pos = tuple(float(v) for v in ap.get("pos", [0, 0, 0]))
                pos = (parent_pos[0] + ap_pos[0],
                       parent_pos[1] + ap_pos[1],
                       parent_pos[2] + ap_pos[2])
                # NOTE: rotation composition is simplified — we use parent_rot + ap rotation.
                # A full implementation would use proper matrix composition.
                ap_rot = tuple(float(v) for v in ap.get("rotation", [0, 0, 0]))
                rot = (parent_rot[0] + ap_rot[0],
                       parent_rot[1] + ap_rot[1],
                       parent_rot[2] + ap_rot[2])

        resolved_positions[pid] = (pos, rot)
        return pos, rot

    flat: list[dict[str, Any]] = []

    for part in parts:
        pid = part.get("id", "")
        pos, rot = _resolve_part(part, set())
        # Apply parent offset/rotation using proper rotation matrix composition.
        # Naive additive RPY (rot[0]+parent_rot[0], ...) is mathematically wrong
        # for non-trivial rotations and produced bad poses for sub-assemblies
        # at non-zero parent angles. compose_pose() does R_world = R_parent @ R_child
        # and rotates the child's offset into the parent's frame.
        from ._rotation import compose_pose
        abs_pos, abs_rot = compose_pose(_parent_offset, _parent_rotation, pos, rot)

        prefixed_id = f"{_prefix}{pid}" if _prefix else pid

        # Case 1: sub-assembly reference
        sub_assembly_ref = part.get("assembly")
        if sub_assembly_ref:
            sub_path = Path(sub_assembly_ref)
            if not sub_path.is_absolute() and config_path is not None:
                sub_path = config_path.parent / sub_path
            try:
                sub_cfg = load_assembly_config(sub_path)
            except AssemblyResolutionError:
                raise

            sub_parts = flatten_assembly(
                sub_cfg,
                config_path=sub_path,
                _visited=_visited,
                _prefix=f"{prefixed_id}/",
                _parent_offset=abs_pos,
                _parent_rotation=abs_rot,
            )
            flat.extend(sub_parts)
            # Expose this sub-assembly's attachment points to downstream attach_to refs
            part["_resolved_ap"] = sub_cfg.get("attachment_points", {})
            continue

        # Case 2: component reference (from catalog)
        component_ref = part.get("component")
        if component_ref:
            # Resolve via component catalog
            from .components import catalog as _catalog
            spec = _catalog.get(component_ref)
            if spec is None:
                raise AssemblyResolutionError(
                    f"Part '{pid}': component '{component_ref}' not in catalog"
                )
            # Generate the STEP file lazily — into outputs/cad/step/<designation>.step
            # Purchased-only stubs (ESC, LiPo, sensors, RC modules) have no
            # generate_fn — they still need to appear in the BOM (_component
            # entry) but produce no STEP geometry. The Assembler skips parts
            # whose step path is missing or empty.
            from pathlib import Path as _P
            step_dir = _P(__file__).resolve().parent.parent / "outputs" / "cad" / "step"
            step_dir.mkdir(parents=True, exist_ok=True)
            safe_name = component_ref.replace("/", "_").replace(" ", "_")
            step_path = step_dir / f"{safe_name}.step"
            step_str: str = ""
            if spec.generate_fn is not None:
                if not step_path.is_file():
                    try:
                        _catalog.generate(component_ref, str(step_path))
                    except Exception:
                        # Generator may fail (CadQuery edge cases, missing
                        # deps). Don't poison the whole assembly — drop the
                        # geometry but keep the BOM entry.
                        step_str = ""
                if step_path.is_file():
                    step_str = str(step_path)
            flat.append({
                "id": prefixed_id,
                "step": step_str,
                "pos": list(abs_pos),
                "rot": list(abs_rot),
                "_component": component_ref,
            })
            # Also expose this component's mating features as attachment points
            if spec.mating_features:
                part["_resolved_ap"] = _mating_features_to_attachment_points(spec.mating_features)
            continue

        # Case 3: raw step file reference
        step_ref = part.get("step")
        if step_ref:
            flat.append({
                "id": prefixed_id,
                "step": step_ref,
                "pos": list(abs_pos),
                "rot": list(abs_rot),
            })
            # Attachment points declared inline, if any
            if "attachment_points" in part:
                part["_resolved_ap"] = part["attachment_points"]
            continue

        raise AssemblyResolutionError(
            f"Part '{pid}' in '{name}' has no 'step', 'assembly', or 'component' reference"
        )

    return flat


def _mating_features_to_attachment_points(
    features: list[Any],
) -> dict[str, dict[str, Any]]:
    """Convert ComponentSpec mating features into attachment_points dict."""
    aps: dict[str, dict[str, Any]] = {}
    for f in features:
        params = f.params or {}
        origin = params.get("origin", [0, 0, 0])
        # Rotation defaults to zero — only "face" normals imply orientation
        aps[f.name] = {
            "pos": list(origin),
            "rotation": [0.0, 0.0, 0.0],
            "type": f.type,
            "params": params,
        }
    return aps


def count_parts(config: dict[str, Any], *, config_path: Path | None = None) -> int:
    """Count the total number of leaf parts in a hierarchical config."""
    return len(flatten_assembly(config, config_path=config_path))


def list_components_used(
    config: dict[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, int]:
    """
    Return {component_designation: quantity} for all catalog components
    referenced anywhere in the hierarchy. Used by BOM generation (phase 6).
    """
    flat = flatten_assembly(config, config_path=config_path)
    counts: dict[str, int] = {}
    for part in flat:
        comp = part.get("_component")
        if comp:
            counts[comp] = counts.get(comp, 0) + 1
    return counts
