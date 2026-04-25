"""Process-indexed DFM rule engine.

Each `Rule` is (process, name, severity, check_fn) where check_fn takes
(spec, stl_path or None) and returns a list of Issues. Rules are
grouped by process so a sheet-metal part doesn't get hit with FDM
overhang rules and vice versa.

This is the new canonical entry point. The legacy LLM-driven
`aria_os/agents/dfm_agent.py` still works and gets called as one
of the rules when `skip_llm=False`, but isn't required for a
deterministic pass.

Adding a new rule:
    @register("cnc_3axis", severity="warning")
    def deep_pocket_aspect(spec, stl_path):
        depth = spec.get("pocket_depth_mm")
        width = spec.get("pocket_width_mm")
        if depth and width and depth / width > 4.0:
            return [Issue("warning", "deep_pocket",
                          f"Pocket aspect {depth/width:.1f}:1 > 4:1 — "
                          "needs ball end mill, slow.",
                          fix="Widen the pocket or split into 2 ops.")]
        return []
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Issue:
    severity: str   # "critical" | "warning" | "info"
    category: str
    message:  str
    fix:      str = ""

    def to_dict(self) -> dict:
        return {"severity": self.severity, "category": self.category,
                "message": self.message, "fix": self.fix}


@dataclass
class DfmReport:
    passed: bool
    score:  float
    process: str
    issues: list[Issue]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "score": self.score,
                "process": self.process,
                "issues": [i.to_dict() for i in self.issues]}


# --- Rule registry ------------------------------------------------------

_RuleFn = Callable[[dict, str | None], list[Issue]]
_REGISTRY: dict[str, list[tuple[str, _RuleFn]]] = {}


def register(process: str, *, name: str | None = None):
    """Decorator: register a rule for a manufacturing process."""
    def deco(fn: _RuleFn):
        rule_name = name or fn.__name__
        _REGISTRY.setdefault(process, []).append((rule_name, fn))
        return fn
    return deco


def available_processes() -> list[str]:
    return sorted(_REGISTRY.keys())


def run_dfm_rules(spec: dict, stl_path: str | None,
                    *, process: str = "cnc_3axis",
                    skip_llm: bool = True) -> DfmReport:
    """Run all rules registered for `process` against (spec, stl).
    Returns a DfmReport summarizing pass/fail + every issue."""
    rules = _REGISTRY.get(process) or _REGISTRY.get("cnc_3axis", [])
    issues: list[Issue] = []
    for rule_name, fn in rules:
        try:
            issues.extend(fn(spec, stl_path) or [])
        except Exception as exc:
            issues.append(Issue("warning", f"rule_error_{rule_name}",
                                  f"DFM rule {rule_name!r} crashed: "
                                  f"{type(exc).__name__}: {exc}"))
    # Optional LLM second-pass for nuanced advice
    if not skip_llm and stl_path:
        try:
            from aria_os.agents.dfm_agent import run_dfm_analysis
            llm_report = run_dfm_analysis(stl_path,
                                            goal=spec.get("goal", ""),
                                            skip_llm=False)
            for li in (llm_report.get("issues") or []):
                issues.append(Issue(
                    li.get("severity", "info"),
                    li.get("category", "llm_suggestion"),
                    li.get("description", ""),
                    fix=li.get("suggestion", "")))
        except Exception:
            pass

    crit = sum(1 for i in issues if i.severity == "critical")
    warn = sum(1 for i in issues if i.severity == "warning")
    score = max(0.0, 1.0 - 0.4 * crit - 0.1 * warn)
    return DfmReport(passed=(crit == 0), score=round(score, 2),
                       process=process, issues=issues)


# --- Spec / STL inspection helpers -------------------------------------

def _get(spec: dict, *keys: str, default=None):
    """First-non-None lookup across alias keys (e.g. 'wall_mm' OR
    'thickness_mm')."""
    for k in keys:
        v = spec.get(k)
        if v is not None:
            return v
    return default


def _bbox(stl_path: str | None) -> tuple[float, float, float] | None:
    """Best-effort bbox extraction from STL via trimesh."""
    if not stl_path:
        return None
    try:
        import trimesh  # type: ignore
        mesh = trimesh.load(stl_path, force="mesh")
        if mesh is None or mesh.is_empty:
            return None
        ext = mesh.bounding_box.extents
        return (float(ext[0]), float(ext[1]), float(ext[2]))
    except Exception:
        return None


# --- CNC 3-axis rules ---------------------------------------------------

@register("cnc_3axis")
def cnc_min_wall_thickness(spec, stl_path):
    """ISO 2768-m + general machinability: AL 1.5mm min, steel 1.0mm."""
    wall = _get(spec, "wall_mm", "thickness_mm", "wall_thickness_mm")
    if wall is None:
        return []
    mat = (spec.get("material") or "").lower()
    min_t = 1.5 if "al" in mat or "aluminum" in mat or "aluminium" in mat \
        else 1.0
    if float(wall) < min_t:
        return [Issue("critical", "wall_too_thin",
                       f"Wall {wall}mm below CNC min {min_t}mm "
                       f"for material {mat or '(unspecified)'}.",
                       fix=f"Increase to ≥{min_t * 1.3:.1f}mm preferred.")]
    return []


@register("cnc_3axis")
def cnc_inside_corner_radius(spec, stl_path):
    """Inside corners require an end mill of matching radius. The
    smallest practical mill is 1mm; less means EDM territory."""
    r = _get(spec, "inside_radius_mm", "fillet_r_mm")
    if r is None:
        return []
    if float(r) < 0.5:
        return [Issue("warning", "inside_corner_tight",
                       f"Inside radius {r}mm requires sub-1mm end mill "
                       "(fragile + slow).",
                       fix="Use ≥1mm fillet, or call out EDM.")]
    return []


@register("cnc_3axis")
def cnc_deep_pocket_aspect(spec, stl_path):
    """Pocket depth-to-width ratio > 4:1 needs special tooling."""
    d = _get(spec, "pocket_depth_mm")
    w = _get(spec, "pocket_width_mm")
    if d is None or w is None:
        return []
    aspect = float(d) / max(float(w), 1e-6)
    if aspect > 4.0:
        return [Issue("warning", "deep_pocket",
                       f"Pocket aspect {aspect:.1f}:1 exceeds 4:1.",
                       fix="Widen, split the pocket, or specify EDM/wire-EDM.")]
    return []


@register("cnc_3axis")
def cnc_hole_to_edge_distance(spec, stl_path):
    """Holes too close to a free edge break out. Min = hole Ø."""
    edge_off = _get(spec, "edge_offset_mm")
    hole_d = _get(spec, "bolt_dia_mm", "hole_dia_mm")
    if edge_off is None or hole_d is None:
        return []
    if float(edge_off) < float(hole_d):
        return [Issue("critical", "hole_to_edge",
                       f"Hole-to-edge {edge_off}mm < hole Ø {hole_d}mm "
                       "— wall will break out during drilling.",
                       fix=f"Move hole inboard so offset ≥ {hole_d}mm "
                          f"(prefer {2*float(hole_d):.0f}mm).")]
    return []


# --- Sheet metal rules --------------------------------------------------

@register("sheet_metal")
def sm_min_bend_radius(spec, stl_path):
    """Bending tighter than min causes outer-fibre cracking."""
    r = _get(spec, "bend_radius_mm", "inside_radius_mm")
    t = _get(spec, "thickness_mm", "wall_mm")
    if r is None or t is None:
        return []
    try:
        from aria_os.engineering.bend_table import min_bend_radius
        mat = spec.get("material", "1018 mild steel")
        rmin = min_bend_radius(mat, float(t))
        if float(r) < rmin:
            return [Issue("critical", "bend_radius_too_tight",
                           f"Inside bend radius {r}mm < material min "
                           f"{rmin:.2f}mm for {mat} at {t}mm thickness.",
                           fix=f"Increase R to ≥{rmin:.2f}mm or change "
                              "material to softer alloy.")]
    except Exception:
        pass
    return []


@register("sheet_metal")
def sm_hole_to_bend_distance(spec, stl_path):
    """Holes within (3 × t + R) of a bend distort during forming."""
    h2b = _get(spec, "hole_to_bend_mm")
    t = _get(spec, "thickness_mm", "wall_mm")
    r = _get(spec, "bend_radius_mm", "inside_radius_mm")
    if h2b is None or t is None:
        return []
    rr = float(r) if r is not None else float(t)
    minimum = 3 * float(t) + rr
    if float(h2b) < minimum:
        return [Issue("warning", "hole_near_bend",
                       f"Hole {h2b}mm from bend < {minimum:.1f}mm "
                       "(3t + R) — will distort during bending.",
                       fix=f"Move hole ≥{minimum:.1f}mm from bend line.")]
    return []


@register("sheet_metal")
def sm_min_flange_length(spec, stl_path):
    """Flange shorter than (2 × t + R) can't be formed in a brake."""
    flange_len = _get(spec, "flange_length_mm")
    t = _get(spec, "thickness_mm", "wall_mm")
    r = _get(spec, "bend_radius_mm", "inside_radius_mm")
    if flange_len is None or t is None:
        return []
    rr = float(r) if r is not None else float(t)
    minimum = 2 * float(t) + rr
    if float(flange_len) < minimum:
        return [Issue("critical", "flange_too_short",
                       f"Flange {flange_len}mm < {minimum:.1f}mm "
                       "(2t + R) — brake can't form it.",
                       fix=f"Lengthen flange to ≥{minimum:.1f}mm.")]
    return []


# --- FDM (3D printing) rules -------------------------------------------

@register("fdm")
def fdm_min_wall(spec, stl_path):
    """FDM walls < 0.8mm (≈2 perimeters @ 0.4mm nozzle) print poorly."""
    wall = _get(spec, "wall_mm", "thickness_mm")
    if wall is None:
        return []
    if float(wall) < 0.8:
        return [Issue("critical", "fdm_wall_too_thin",
                       f"FDM wall {wall}mm < 0.8mm = under 2 perimeters "
                       "on 0.4mm nozzle.",
                       fix="Use ≥1.6mm (4 perimeters preferred).")]
    if float(wall) < 1.6:
        return [Issue("warning", "fdm_wall_marginal",
                       f"FDM wall {wall}mm gets only 2-3 perimeters.",
                       fix="≥1.6mm gives 4+ perimeters and proper bonding.")]
    return []


@register("fdm")
def fdm_overhang_angle(spec, stl_path):
    """Overhangs >45° from vertical need supports."""
    angle = _get(spec, "overhang_angle_deg")
    if angle is None:
        return []
    if float(angle) > 45.0:
        return [Issue("warning", "fdm_overhang",
                       f"Overhang {angle}° exceeds 45° self-support limit.",
                       fix="Add support material or reorient.")]
    return []


@register("fdm")
def fdm_min_feature_size(spec, stl_path):
    """FDM resolution ≈ nozzle Ø. Features < 0.4mm don't print."""
    feat = _get(spec, "min_feature_mm")
    if feat is None:
        return []
    if float(feat) < 0.4:
        return [Issue("critical", "fdm_feature_too_small",
                       f"Feature {feat}mm < 0.4mm nozzle Ø.",
                       fix="Use ≥0.4mm features or switch to SLA.")]
    return []


# --- SLA rules ----------------------------------------------------------

@register("sla")
def sla_min_wall(spec, stl_path):
    """SLA walls < 0.5mm are fragile in handling."""
    wall = _get(spec, "wall_mm", "thickness_mm")
    if wall is None:
        return []
    if float(wall) < 0.5:
        return [Issue("warning", "sla_wall_thin",
                       f"SLA wall {wall}mm < 0.5mm — fragile.",
                       fix="Increase to ≥0.8mm for handling robustness.")]
    return []


@register("sla")
def sla_drain_holes(spec, stl_path):
    """Hollow SLA parts need drain holes to evacuate uncured resin."""
    if spec.get("hollow"):
        if not spec.get("drain_holes"):
            return [Issue("critical", "sla_no_drain",
                           "Hollow SLA part has no drain holes — "
                           "trapped resin will cure inside, distort the "
                           "part, fail printing or post-cure.",
                           fix="Add ≥2× Ø3mm drain holes at low points.")]
    return []


# --- Casting rules ------------------------------------------------------

@register("casting")
def cast_min_wall(spec, stl_path):
    """Sand casting min 3mm; investment 1.5mm."""
    wall = _get(spec, "wall_mm", "thickness_mm")
    method = (spec.get("casting_method") or "sand").lower()
    if wall is None:
        return []
    minimum = 1.5 if "investment" in method else 3.0
    if float(wall) < minimum:
        return [Issue("critical", "cast_wall_too_thin",
                       f"{method} casting wall {wall}mm < {minimum}mm min.",
                       fix=f"Increase to ≥{minimum*1.5:.1f}mm preferred.")]
    return []


@register("casting")
def cast_draft_angle(spec, stl_path):
    """Castings need ≥1° draft on every vertical face for mold pull."""
    draft = _get(spec, "draft_deg")
    if draft is None:
        return [Issue("warning", "cast_no_draft",
                       "No draft angle specified — casting needs ≥1°.",
                       fix="Add ≥1° draft on all vertical faces.")]
    if float(draft) < 1.0:
        return [Issue("critical", "cast_draft_insufficient",
                       f"Draft {draft}° < 1° min for mold pull.",
                       fix="Increase draft to ≥1° (3° preferred).")]
    return []


# --- Injection mold rules ----------------------------------------------

@register("injection_mold")
def im_uniform_wall(spec, stl_path):
    """Injection-molded parts need uniform wall thickness; variation
    > 25% causes sink marks."""
    wall = _get(spec, "wall_mm", "thickness_mm")
    wall_max = _get(spec, "wall_max_mm")
    if wall is None or wall_max is None:
        return []
    variation = (float(wall_max) - float(wall)) / float(wall)
    if variation > 0.25:
        return [Issue("warning", "im_wall_variation",
                       f"Wall variation {variation*100:.0f}% > 25%.",
                       fix="Equalize wall thickness or add ribs.")]
    return []


@register("injection_mold")
def im_min_wall(spec, stl_path):
    """Injection min ≈ 0.8mm for ABS, 1.0mm for PC, etc."""
    wall = _get(spec, "wall_mm", "thickness_mm")
    if wall is None:
        return []
    if float(wall) < 0.8:
        return [Issue("critical", "im_wall_too_thin",
                       f"Injection mold wall {wall}mm < 0.8mm — "
                       "won't fill properly.",
                       fix="Increase to ≥1.5mm typical.")]
    return []
