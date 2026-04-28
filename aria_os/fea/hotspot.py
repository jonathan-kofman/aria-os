"""hotspot.py — read FEA stress hot-spots, classify them, suggest a fix.

Used by self_heal v2 to drive smarter remediation than "just thicken
in Z." Output is a `RemediationHint` dict the next iteration's CAD
generator can act on.

Algorithm:
    1. Parse VTU (or .frd directly).
    2. Pick top P% von Mises nodes.
    3. Spatially cluster (DBSCAN-lite via union-find on nearest neighbors).
    4. For each cluster, compute centroid + bbox + alignment with the
       part bounding box.
    5. Classify the cluster:
        - "corner"   — within 5% of a part bbox corner
        - "edge"     — on an edge (one corner-coord ≈ extremum, others not)
        - "face"     — interior of a flat face
        - "thin_section" — bbox z-extent is small, cluster spans full z
    6. Map classification → remediation_kind:
        corner          → fillet at that location
        edge            → fillet/chamfer along that edge
        face            → rib perpendicular to load direction at the face
        thin_section    → thicken locally (or globally if cluster spans most of part)

The hint dict is consumed by self_heal.heal_fea on its next iteration
and (eventually) by the planner to emit a real CAD modification.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
import math
from typing import Optional


@dataclass
class HotSpot:
    """One spatial cluster of high-stress nodes."""
    centroid: tuple             # (x, y, z) in part coords
    bbox_min: tuple
    bbox_max: tuple
    n_nodes: int
    max_stress_mpa: float
    avg_stress_mpa: float
    # Classification
    location: str = "interior"  # "corner" | "edge" | "face" | "interior"
    aligned_axes: tuple = field(default_factory=tuple)  # axes the cluster touches at extreme
    thin_section: bool = False
    # Suggested remediation
    remediation_kind: str = ""  # "fillet" | "chamfer" | "rib" | "thicken_local"
    remediation_params: dict = field(default_factory=dict)


@dataclass
class RemediationHint:
    ok: bool
    n_hotspots: int
    part_bbox_min: tuple
    part_bbox_max: tuple
    hotspots: list = field(default_factory=list)
    primary_action: str = ""  # most-impactful suggestion
    primary_params: dict = field(default_factory=dict)
    notes: str = ""


def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v) if n > 0 else v


def _classify_cluster(c: HotSpot, part_min: tuple, part_max: tuple,
                       tol_pct: float = 0.05):
    """Compare cluster bbox to part bbox; mark which axes hit extremes."""
    extents = tuple(part_max[i] - part_min[i] for i in range(3))
    tol = tuple(max(extents[i] * tol_pct, 0.5) for i in range(3))
    aligned: list[str] = []
    near_min = [False] * 3
    near_max = [False] * 3
    for i, ax in enumerate("xyz"):
        if abs(c.centroid[i] - part_min[i]) <= tol[i]:
            aligned.append(f"-{ax}"); near_min[i] = True
        elif abs(c.centroid[i] - part_max[i]) <= tol[i]:
            aligned.append(f"+{ax}"); near_max[i] = True
    c.aligned_axes = tuple(aligned)
    n_extremes = len(aligned)
    if n_extremes >= 3:
        c.location = "corner"
    elif n_extremes == 2:
        c.location = "edge"
    elif n_extremes == 1:
        c.location = "face"
    else:
        c.location = "interior"
    # Thin-section detection: if cluster's bbox spans >80% of part's
    # smallest dim, it's threading the whole thickness — likely a thin
    # plate failing in bending.
    cluster_extents = tuple(c.bbox_max[i] - c.bbox_min[i] for i in range(3))
    smallest = min(extents)
    smallest_ax = extents.index(smallest)
    if cluster_extents[smallest_ax] >= 0.8 * smallest:
        c.thin_section = True


def _suggest_remediation(c: HotSpot, part_min: tuple, part_max: tuple,
                          load_axis: str = "-z"):
    """Pick a remediation_kind + params based on classification."""
    if c.thin_section:
        c.remediation_kind = "thicken_local"
        c.remediation_params = {
            "axis": load_axis.lstrip("+-"),
            "scale": 1.25,
            "centroid": list(c.centroid),
        }
    elif c.location == "corner":
        c.remediation_kind = "fillet"
        c.remediation_params = {
            "location": list(c.centroid),
            "radius_mm": max(2.0,
                              0.05 * max(part_max[i] - part_min[i]
                                          for i in range(3))),
        }
    elif c.location == "edge":
        c.remediation_kind = "fillet"
        c.remediation_params = {
            "location": list(c.centroid),
            "axes": list(c.aligned_axes),
            "radius_mm": max(1.5,
                              0.04 * max(part_max[i] - part_min[i]
                                          for i in range(3))),
        }
    elif c.location == "face":
        c.remediation_kind = "rib"
        # Rib axis: perpendicular to the face the cluster is on AND
        # perpendicular to the load. If face is "+x", rib runs in y or z.
        face_axis = c.aligned_axes[0] if c.aligned_axes else "+z"
        face_letter = face_axis[-1]
        load_letter = load_axis.lstrip("+-")
        for ax in "xyz":
            if ax != face_letter and ax != load_letter:
                rib_axis = ax
                break
        else:
            rib_axis = "y"
        c.remediation_kind = "rib"
        c.remediation_params = {
            "face": face_axis,
            "rib_axis": rib_axis,
            "anchor": list(c.centroid),
            "thickness_mm": 3.0,
            "height_mm": 0.3 * max(part_max[i] - part_min[i]
                                     for i in range(3)),
        }
    else:
        c.remediation_kind = "thicken_local"
        c.remediation_params = {
            "axis": load_axis.lstrip("+-"),
            "scale": 1.2,
            "centroid": list(c.centroid),
        }


def _cluster_nodes(nodes: dict, vm_per_node: dict,
                    part_min: tuple, part_max: tuple,
                    *, top_pct: float = 0.05,
                    cluster_radius_pct: float = 0.10
                    ) -> list[HotSpot]:
    """Pick top P% by vM, then union-find cluster within radius_pct of
    the largest part dimension. Returns list of HotSpot.
    """
    if not vm_per_node:
        return []
    # Threshold
    vals = sorted(vm_per_node.values(), reverse=True)
    k = max(1, int(len(vals) * top_pct))
    cutoff = vals[k - 1]
    high_nids = [nid for nid, vm in vm_per_node.items() if vm >= cutoff]
    if not high_nids:
        return []
    # Union-find
    parent = {nid: nid for nid in high_nids}
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[rb] = ra
    extents = tuple(part_max[i] - part_min[i] for i in range(3))
    r = cluster_radius_pct * max(extents)
    r2 = r * r
    pts = [nodes.get(nid) for nid in high_nids]
    for i in range(len(high_nids)):
        if pts[i] is None: continue
        for j in range(i + 1, len(high_nids)):
            if pts[j] is None: continue
            dx = pts[i][0] - pts[j][0]
            dy = pts[i][1] - pts[j][1]
            dz = pts[i][2] - pts[j][2]
            if dx*dx + dy*dy + dz*dz < r2:
                union(high_nids[i], high_nids[j])
    # Build clusters
    groups: dict = {}
    for nid in high_nids:
        groups.setdefault(find(nid), []).append(nid)
    out: list[HotSpot] = []
    for grp in groups.values():
        gpts = [nodes[n] for n in grp if nodes.get(n)]
        if not gpts: continue
        cx = sum(p[0] for p in gpts) / len(gpts)
        cy = sum(p[1] for p in gpts) / len(gpts)
        cz = sum(p[2] for p in gpts) / len(gpts)
        bbmin = (min(p[0] for p in gpts), min(p[1] for p in gpts),
                  min(p[2] for p in gpts))
        bbmax = (max(p[0] for p in gpts), max(p[1] for p in gpts),
                  max(p[2] for p in gpts))
        gvals = [vm_per_node[n] for n in grp]
        out.append(HotSpot(
            centroid=(cx, cy, cz),
            bbox_min=bbmin, bbox_max=bbmax,
            n_nodes=len(grp),
            max_stress_mpa=max(gvals),
            avg_stress_mpa=sum(gvals) / len(gvals)))
    out.sort(key=lambda h: -h.max_stress_mpa)
    return out


def analyze_frd(frd_path: str | Path,
                  *,
                  load_axis: str = "-z",
                  top_pct: float = 0.05,
                  cluster_radius_pct: float = 0.10) -> RemediationHint:
    """Read a CCX .frd, find hot-spots, classify, suggest remediation."""
    from aria_os.fea.vtk_export import _parse_frd, _von_mises
    parsed = _parse_frd(Path(frd_path))
    nodes = parsed.get("nodes", {})
    if not nodes:
        return RemediationHint(ok=False, n_hotspots=0,
                                part_bbox_min=(0, 0, 0),
                                part_bbox_max=(0, 0, 0),
                                notes="frd had no nodes")
    vm = {nid: _von_mises(s) for nid, s in parsed.get("S", {}).items()}
    if not vm:
        return RemediationHint(ok=False, n_hotspots=0,
                                part_bbox_min=(0, 0, 0),
                                part_bbox_max=(0, 0, 0),
                                notes="frd had no stress block")
    pts = list(nodes.values())
    part_min = (min(p[0] for p in pts), min(p[1] for p in pts),
                 min(p[2] for p in pts))
    part_max = (max(p[0] for p in pts), max(p[1] for p in pts),
                 max(p[2] for p in pts))
    spots = _cluster_nodes(nodes, vm, part_min, part_max,
                            top_pct=top_pct,
                            cluster_radius_pct=cluster_radius_pct)
    for h in spots:
        _classify_cluster(h, part_min, part_max)
        _suggest_remediation(h, part_min, part_max, load_axis=load_axis)
    primary_action = ""
    primary_params: dict = {}
    if spots:
        primary_action = spots[0].remediation_kind
        primary_params = spots[0].remediation_params
    return RemediationHint(
        ok=True, n_hotspots=len(spots),
        part_bbox_min=part_min, part_bbox_max=part_max,
        hotspots=[asdict(h) for h in spots],
        primary_action=primary_action,
        primary_params=primary_params,
        notes=f"top {top_pct*100:.0f}% vM nodes clustered at "
              f"r={cluster_radius_pct*100:.0f}% of bbox")


__all__ = ["HotSpot", "RemediationHint", "analyze_frd"]
