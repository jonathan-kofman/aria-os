"""Derive GD&T tolerance values from real part geometry.

Replaces the boilerplate FCF the addin's enrichDrawing was emitting
("⌀ 0.20 Ⓜ A B C, FLATNESS 0.05, PERPENDICULARITY 0.10 A") with
numbers that scale to the actual part:

  * Bbox-derived datum assignment: largest face = A, second = B, third = C
  * Position tolerance: scales with the smallest hole diameter (rule of
    thumb — diameter / 30, clamped to [0.10, 0.50] mm)
  * Flatness: 0.0008 × longest edge length, clamped to [0.02, 0.30] mm
    (rough analogue to typical machining flatness specs)
  * Perpendicularity: 1.5× flatness (datum-A wall must hold tighter than
    free-form face)
  * General tolerance bracket: ISO 2768-mK by default (medium / fine):
      ±0.5 mm linear, ±0.5° angular when no part-level override

Used by `dashboard/aria_server.py` before calling /op enrichDrawing on
the SW addin: orchestrator computes the spec from the STEP geometry +
build_config, passes the derived numbers as params, addin places the
notes verbatim. Means each part gets GD&T sized to its own envelope,
not a copy-paste boilerplate.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class GdtSpec:
    """Per-part GD&T values to inject into enrichDrawing notes."""
    position_tolerance_mm: float = 0.20
    flatness_mm:           float = 0.05
    perpendicularity_mm:   float = 0.10
    general_linear_mm:     float = 0.5
    general_angular_deg:   float = 0.5
    primary_datum:         str   = "A"
    secondary_datum:       str   = "B"
    tertiary_datum:        str   = "C"
    standard:              str   = "ASME Y14.5-2018"
    iso_class:             str   = "ISO 2768-mK"
    material_label:        str   = "AS NOTED"
    finish_label:          str   = "AS NOTED"
    note_lines: list[str]        = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # rendering hint: the addin uses these line strings verbatim so
        # the planner can override formatting without changing the addin.
        if d.get("note_lines") is None:
            d["note_lines"] = [
                f"GENERAL TOL: ±{self.general_linear_mm:g} mm  "
                f"ANGULAR ±{self.general_angular_deg:g}°  ({self.iso_class})",
                f"GD&T PER {self.standard}  RFS UNLESS NOTED",
                f"MATERIAL: {self.material_label}  FINISH: {self.finish_label}",
            ]
        return d


# --------------------------------------------------------------------------- #
# Recipe cache (rec #8): persistent per-(STEP, build_config) GdtSpec store.
# A cold derive_from_step on a 5MB drone-frame STEP costs ~1.5s for
# bbox + face scan; on a 100-part bundle that's the difference between
# instant and 2.5 min for re-runs. We cache aggressively because the
# inputs (mtime + size + material_label + finish_label) capture every
# axis on which the spec could change.
# --------------------------------------------------------------------------- #

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_INIT = False


def _cache_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / "AriaGDT" / "specs.json"
    return Path.home() / ".cache" / "AriaGDT" / "specs.json"


def _cache_init() -> None:
    global _CACHE, _CACHE_INIT
    if _CACHE_INIT: return
    p = _cache_path()
    try:
        if p.is_file():
            _CACHE = json.loads(p.read_text("utf-8"))
    except Exception:
        _CACHE = {}
    _CACHE_INIT = True


def _cache_save() -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_CACHE, indent=2), "utf-8")
    except Exception:
        pass


def _cache_key(step: Path, part_cfg: dict | None) -> str | None:
    """Key built from (mtime, size, material, finish). If the STEP file
    can't be stat'd, returns None (no caching for that call)."""
    try:
        st = step.stat()
    except OSError:
        return None
    mat = ""
    fin = ""
    if part_cfg:
        mat = (part_cfg.get("material") or part_cfg.get("material_name")
                or "")
        fin = part_cfg.get("finish") or ""
    key_src = f"{step.resolve()}|{int(st.st_mtime)}|{st.st_size}|{mat}|{fin}"
    return hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:24]


def _cache_lookup(key: str | None) -> dict | None:
    if not key: return None
    _cache_init()
    with _CACHE_LOCK:
        v = _CACHE.get(key)
        return dict(v) if isinstance(v, dict) else None


def _cache_store(key: str | None, spec_dict: dict) -> None:
    if not key: return
    _cache_init()
    with _CACHE_LOCK:
        _CACHE[key] = dict(spec_dict)
        _cache_save()


def _spec_from_dict(d: dict) -> GdtSpec:
    """Reverse of GdtSpec.as_dict — used to thaw a cache hit."""
    fields = {k: d[k] for k in (
        "position_tolerance_mm", "flatness_mm", "perpendicularity_mm",
        "general_linear_mm", "general_angular_deg",
        "primary_datum", "secondary_datum", "tertiary_datum",
        "standard", "iso_class", "material_label", "finish_label",
    ) if k in d}
    s = GdtSpec(**fields)
    if "note_lines" in d and isinstance(d["note_lines"], list):
        s.note_lines = list(d["note_lines"])
    return s


def derive_from_step(step_path: str | Path,
                      build_config_part: dict | None = None,
                      *, use_cache: bool = True) -> GdtSpec:
    """Compute a GdtSpec by inspecting the STEP file's bbox + features.

    Best-effort: if the STEP can't be opened (no cadquery / OCP) we fall
    back to the dataclass defaults. The build_config_part dict (one
    entry from build_config.json's parts[]) overrides material if set.

    With `use_cache=True` (default), the result is keyed on the STEP's
    (mtime, size, material, finish) tuple and returned from
    `%LOCALAPPDATA%\\AriaGDT\\specs.json` on a hit. Set False to force a
    fresh derive (e.g. when CadQuery is upgraded and we want to retake
    measurements).

    Returns a GdtSpec ready for `.as_dict()` -> JSON -> enrichDrawing.
    """
    step = Path(step_path)
    if use_cache:
        key = _cache_key(step, build_config_part)
        hit = _cache_lookup(key)
        if hit is not None:
            return _spec_from_dict(hit)
    else:
        key = None

    spec = GdtSpec()

    # 1. material from build_config (drone frame -> 6061 Alloy etc.)
    if build_config_part:
        mat = (build_config_part.get("material")
                or build_config_part.get("material_name"))
        if mat: spec.material_label = mat
        finish = build_config_part.get("finish")
        if finish: spec.finish_label = finish

    # 2. bbox-derived flatness + datum ordering
    try:
        import cadquery as cq  # type: ignore
        shape = cq.importers.importStep(str(step_path))
        bb = shape.val().BoundingBox()
        x = bb.xlen; y = bb.ylen; z = bb.zlen
        edges = sorted([("X", x), ("Y", y), ("Z", z)],
                          key=lambda t: -t[1])
        # datum letter order matches face area order: largest first
        spec.primary_datum   = f"A({edges[0][0]})"
        spec.secondary_datum = f"B({edges[1][0]})"
        spec.tertiary_datum  = f"C({edges[2][0]})"
        # flatness scales with longest edge
        longest = edges[0][1]
        flat = max(0.02, min(0.30, longest * 0.0008))
        spec.flatness_mm = round(flat, 3)
        spec.perpendicularity_mm = round(flat * 1.5, 3)
    except Exception:
        pass  # keep defaults

    # 3. position tolerance from smallest hole (best-effort feature scan)
    try:
        # cadquery's wp.faces() gives us per-face geometry; we look for
        # cylindrical faces whose normal-axis radius is small (holes).
        import cadquery as cq  # type: ignore  # noqa: F811
        shape = cq.importers.importStep(str(step_path))
        smallest_dia = None
        for f in shape.val().Faces():
            try:
                gtype = f.geomType()
                if gtype == "CYLINDER":
                    r = f.radius()
                    if r > 0 and r < 50:  # ignore large outer cylinders
                        d = r * 2
                        if smallest_dia is None or d < smallest_dia:
                            smallest_dia = d
            except Exception:
                continue
        if smallest_dia:
            pt = max(0.10, min(0.50, smallest_dia / 30.0))
            spec.position_tolerance_mm = round(pt, 3)
    except Exception:
        pass

    if use_cache:
        try:
            _cache_store(key, spec.as_dict())
        except Exception:
            pass

    return spec


def derive_for_bundle(bundle_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Compute a GdtSpec for every fabricated part in a system bundle.

    Reads <bundle>/build_config.json + each part's STEP, returns
    {part_id: spec_dict} keyed by build_config part.id.
    """
    bundle = Path(bundle_dir)
    cfg_path = bundle / "build_config.json"
    if not cfg_path.is_file():
        return {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for part in cfg.get("parts", []):
        if not part.get("fabricated", True):
            continue
        step = part.get("step")
        if not step or not Path(step).is_file():
            continue
        spec = derive_from_step(step, part)
        out[part["id"]] = spec.as_dict()
    return out


if __name__ == "__main__":
    # CLI: derive specs for a bundle, print as JSON
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(derive_for_bundle(target), indent=2))
