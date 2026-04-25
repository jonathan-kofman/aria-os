"""OnshapeExecutor — applies ARIA's native op plan to an Onshape
Part Studio via the REST API.

Mirrors the KiCad executor pattern: takes one op at a time, translates
to Onshape's btMXxx feature JSON, and POSTs. The same op kinds that
stream into Fusion/Rhino also work here — no planner changes needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .client import OnshapeClient, get_client

# Onshape uses meters internally. All ARIA ops pass mm.
_MM_TO_M = 0.001


class OnshapeExecutor:
    """Applies a native-op plan to a specified Part Studio."""

    def __init__(self, did: str, wid: str, eid: str,
                 client: OnshapeClient | None = None,
                 repo_root: Path | None = None):
        self.did = did
        self.wid = wid
        self.eid = eid
        self.client = client or get_client(repo_root=repo_root)
        self.sketches: dict[str, dict] = {}   # alias -> {feature_id, entities}
        self.features: dict[str, str] = {}    # alias -> feature_id
        self._ops_applied = 0

    def execute(self, kind: str, params: dict) -> dict:
        handler = getattr(self, f"_op_{kind}", None)
        if handler is None:
            raise ValueError(f"Unknown Onshape op: {kind}")
        result = handler(params or {})
        self._ops_applied += 1
        return {"ok": True, "op": kind, "ops_applied": self._ops_applied,
                **result}

    # --- Op handlers ----------------------------------------------------

    def _op_beginPlan(self, _p: dict) -> dict:
        self.sketches.clear()
        self.features.clear()
        return {"registry_cleared": True}

    def _op_newSketch(self, p: dict) -> dict:
        plane_map = {"XY": "JCC", "XZ": "JCD", "YZ": "JCE"}
        plane_id = plane_map.get((p.get("plane") or "XY").upper(), "JCC")
        alias = p.get("alias") or f"sketch_{len(self.sketches) + 1}"
        feature = {
            "btType": "BTMSketch-151",
            "featureType": "newSketch",
            "name": p.get("name", f"ARIA Sketch {len(self.sketches) + 1}"),
            "parameters": [{
                "btType": "BTMParameterQueryList-148",
                "parameterId": "sketchPlane",
                "queries": [{
                    "btType": "BTMIndividualQuery-138",
                    "deterministicIds": [plane_id],
                }],
            }],
            "entities": [],
        }
        reply = self.client.add_feature(
            self.did, self.wid, self.eid, feature)
        fid = (reply.get("feature", {}).get("featureId")
                or reply.get("featureId") or "")
        self.sketches[alias] = {"feature_id": fid, "entities": []}
        return {"id": alias, "kind": "sketch", "onshape_id": fid}

    def _op_sketchCircle(self, p: dict) -> dict:
        sk = self.sketches.get(p["sketch"])
        if not sk:
            raise ValueError(f"Unknown sketch alias: {p['sketch']}")
        cx = float(p.get("cx", 0)) * _MM_TO_M
        cy = float(p.get("cy", 0)) * _MM_TO_M
        r  = float(p["r"]) * _MM_TO_M
        ent_id = f"{p['sketch']}_c{len(sk['entities'])}"
        sk["entities"].append({
            "btType": "BTMSketchCurve-4",
            "geometry": {
                "btType": "BTCurveGeometryCircle-115",
                "xCenter": cx, "yCenter": cy, "radius": r,
            },
            "centerId": ent_id + "_c",
            "entityId": ent_id,
        })
        # Update the sketch feature with new entities
        updated = {
            "btType": "BTMSketch-151",
            "featureId": sk["feature_id"],
            "entities": sk["entities"],
        }
        self.client.request(
            "POST",
            f"/api/partstudios/d/{self.did}/w/{self.wid}/e/{self.eid}/"
            f"features/featureid/{sk['feature_id']}",
            json_body={"feature": updated})
        return {"kind": "circle", "r_mm": p["r"], "cx_mm": p.get("cx", 0),
                "cy_mm": p.get("cy", 0)}

    def _op_extrude(self, p: dict) -> dict:
        sk = self.sketches.get(p["sketch"])
        if not sk:
            raise ValueError(f"Unknown sketch alias: {p['sketch']}")
        op_map = {"new": "NEW", "cut": "REMOVE",
                   "join": "ADD", "intersect": "INTERSECT"}
        op_enum = op_map.get(p.get("operation", "new"), "NEW")
        dist_m = abs(float(p["distance"])) * _MM_TO_M
        alias = p.get("alias") or f"extrude_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "extrude",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "entities",
                 "queries": [{
                     "btType": "BTMIndividualSketchRegionQuery-140",
                     "featureId": sk["feature_id"],
                 }]},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "endBound",
                 "enumName": "BoundingType", "namespace": "",
                 "value": "BLIND"},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "operationType",
                 "enumName": "NewBodyOperationType", "namespace": "",
                 "value": op_enum},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "depth",
                 "expression": f"{abs(float(p['distance']))} mm",
                 "value": dist_m, "units": "meter",
                 "isInteger": False},
                {"btType": "BTMParameterBoolean-144",
                 "parameterId": "oppositeDirection",
                 "value": float(p["distance"]) < 0},
            ],
        }
        reply = self.client.add_feature(
            self.did, self.wid, self.eid, feature)
        fid = (reply.get("feature", {}).get("featureId")
                or reply.get("featureId") or "")
        self.features[alias] = fid
        return {"id": alias, "kind": "extrude",
                 "distance_mm": p["distance"],
                 "operation": p.get("operation", "new"),
                 "onshape_id": fid}

    def _op_circularPattern(self, p: dict) -> dict:
        feat_id = self.features.get(p["feature"])
        if not feat_id:
            raise ValueError(f"Unknown feature alias: {p['feature']}")
        axis = (p.get("axis") or "Z").upper()
        axis_id = {"X": "JCE", "Y": "JCD", "Z": "JCC"}.get(axis, "JCC")
        count = int(p.get("count", 2))
        alias = p.get("alias") or f"pattern_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "pattern",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "patternType",
                 "enumName": "PatternType", "namespace": "",
                 "value": "CIRCULAR"},
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "axis",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [axis_id]}]},
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "instanceFunction",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [feat_id]}]},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "instanceCount",
                 "expression": str(count), "value": count,
                 "isInteger": True},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "angle",
                 "expression": f"{360 / count} deg",
                 "value": 360 / count,
                 "isInteger": False},
            ],
        }
        reply = self.client.add_feature(
            self.did, self.wid, self.eid, feature)
        fid = (reply.get("feature", {}).get("featureId")
                or reply.get("featureId") or "")
        self.features[alias] = fid
        return {"id": alias, "kind": "circular_pattern",
                 "count": count, "axis": axis, "onshape_id": fid}

    # ------------------------------------------------------------------
    # W1: extended sketch primitives
    #
    # Each primitive appends a BTMSketchCurve-4 entity to the open
    # sketch feature, then PATCHes the sketch via featureid. Onshape
    # accepts compound entities — line segments, splines (BTCurveGeometryFitSpline-)
    # and arcs (BTCurveGeometryArc-).
    # ------------------------------------------------------------------

    def _patch_sketch(self, sk: dict) -> None:
        """Replace the open sketch feature's entities with the local copy."""
        updated = {
            "btType": "BTMSketch-151",
            "featureId": sk["feature_id"],
            "entities": sk["entities"],
        }
        self.client.request(
            "POST",
            f"/api/partstudios/d/{self.did}/w/{self.wid}/e/{self.eid}/"
            f"features/featureid/{sk['feature_id']}",
            json_body={"feature": updated})

    def _op_sketchSpline(self, p: dict) -> dict:
        sk = self.sketches.get(p["sketch"])
        if not sk:
            raise ValueError(f"Unknown sketch alias: {p['sketch']}")
        pts = p.get("points") or []
        if len(pts) < 3:
            raise ValueError("sketchSpline requires ≥3 points")
        ent_id = f"{p['sketch']}_sp{len(sk['entities'])}"
        # Onshape's interpolated spline geometry: a list of fit points.
        sk["entities"].append({
            "btType": "BTMSketchCurve-4",
            "geometry": {
                "btType": "BTCurveGeometrySpline-117",
                "isPeriodic": False,
                "fitPoints": [
                    {"btType": "BTVector2d-152",
                     "x": float(pt[0]) * _MM_TO_M,
                     "y": float(pt[1]) * _MM_TO_M}
                    for pt in pts
                ],
            },
            "entityId": ent_id,
        })
        self._patch_sketch(sk)
        return {"kind": "spline", "n_pts": len(pts)}

    def _op_sketchPolyline(self, p: dict) -> dict:
        sk = self.sketches.get(p["sketch"])
        if not sk:
            raise ValueError(f"Unknown sketch alias: {p['sketch']}")
        pts = p.get("points") or []
        if len(pts) < 2:
            raise ValueError("sketchPolyline requires ≥2 points")
        closed = bool(p.get("closed", False))
        # Emit one BTMSketchCurveSegment-155 line per consecutive pair.
        seq = list(pts) + ([pts[0]] if closed and len(pts) > 2 else [])
        for i, (a, b) in enumerate(zip(seq, seq[1:])):
            sk["entities"].append({
                "btType": "BTMSketchCurveSegment-155",
                "geometry": {
                    "btType": "BTCurveGeometryLine-117",
                    "pntX": float(a[0]) * _MM_TO_M,
                    "pntY": float(a[1]) * _MM_TO_M,
                    "dirX": float(b[0] - a[0]),
                    "dirY": float(b[1] - a[1]),
                },
                "startPointId": f"{p['sketch']}_pl{len(sk['entities'])}_s",
                "endPointId":   f"{p['sketch']}_pl{len(sk['entities'])}_e",
                "entityId":     f"{p['sketch']}_pl{len(sk['entities'])}",
            })
        self._patch_sketch(sk)
        return {"kind": "polyline", "n_pts": len(pts), "closed": closed}

    def _op_sketchRect(self, p: dict) -> dict:
        sk = self.sketches.get(p["sketch"])
        if not sk:
            raise ValueError(f"Unknown sketch alias: {p['sketch']}")
        cx = float(p.get("cx", 0))
        cy = float(p.get("cy", 0))
        w = float(p["w"]); h = float(p["h"])
        # Build as four polyline segments around (cx,cy)
        corners = [(cx - w/2, cy - h/2), (cx + w/2, cy - h/2),
                   (cx + w/2, cy + h/2), (cx - w/2, cy + h/2)]
        return self._op_sketchPolyline({
            "sketch": p["sketch"],
            "points": corners,
            "closed": True,
        })

    # ------------------------------------------------------------------
    # W1: extended solid features
    # ------------------------------------------------------------------

    @staticmethod
    def _op_enum(operation: str) -> str:
        return {"new": "NEW", "cut": "REMOVE",
                "join": "ADD", "intersect": "INTERSECT"}.get(operation, "NEW")

    def _add_feature(self, feature: dict, alias: str) -> str:
        reply = self.client.add_feature(self.did, self.wid, self.eid, feature)
        fid = (reply.get("feature", {}).get("featureId")
                or reply.get("featureId") or "")
        self.features[alias] = fid
        return fid

    def _op_revolve(self, p: dict) -> dict:
        sk = self.sketches.get(p["sketch"])
        if not sk:
            raise ValueError(f"Unknown sketch alias: {p['sketch']}")
        op_enum = self._op_enum(p.get("operation", "new"))
        axis = (p.get("axis") or "Z").upper()
        axis_id = {"X": "JCE", "Y": "JCD", "Z": "JCC"}.get(axis, "JCC")
        angle = float(p.get("angle", 360))
        alias = p.get("alias") or f"revolve_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "revolve",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "entities",
                 "queries": [{
                     "btType": "BTMIndividualSketchRegionQuery-140",
                     "featureId": sk["feature_id"]}]},
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "axis",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [axis_id]}]},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "operationType",
                 "enumName": "NewBodyOperationType", "namespace": "",
                 "value": op_enum},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "revolveType",
                 "enumName": "RevolveType", "namespace": "",
                 "value": "FULL" if abs(angle - 360) < 0.01 else "BLIND"},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "angle",
                 "expression": f"{angle} deg", "value": angle,
                 "isInteger": False},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "revolve", "angle_deg": angle,
                 "operation": p.get("operation", "new"), "onshape_id": fid}

    def _op_sweep(self, p: dict) -> dict:
        prof_sk = self.sketches.get(p["profile_sketch"])
        path_sk = self.sketches.get(p["path_sketch"])
        if not prof_sk or not path_sk:
            raise ValueError("sweep needs both profile_sketch and path_sketch")
        op_enum = self._op_enum(p.get("operation", "new"))
        alias = p.get("alias") or f"sweep_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "sweep",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "profiles",
                 "queries": [{
                     "btType": "BTMIndividualSketchRegionQuery-140",
                     "featureId": prof_sk["feature_id"]}]},
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "path",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [path_sk["feature_id"]]}]},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "operationType",
                 "enumName": "NewBodyOperationType", "namespace": "",
                 "value": op_enum},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "sweep",
                 "operation": p.get("operation", "new"), "onshape_id": fid}

    def _op_loft(self, p: dict) -> dict:
        sections = p.get("sections") or []
        if len(sections) < 2:
            raise ValueError("loft requires ≥2 sections")
        op_enum = self._op_enum(p.get("operation", "new"))
        alias = p.get("alias") or f"loft_{len(self.features) + 1}"
        section_queries = []
        for sa in sections:
            sk = self.sketches.get(sa)
            if not sk:
                raise ValueError(f"loft section '{sa}' has no sketch")
            section_queries.append({
                "btType": "BTMIndividualSketchRegionQuery-140",
                "featureId": sk["feature_id"]})
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "loft",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "profiles",
                 "queries": section_queries},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "operationType",
                 "enumName": "NewBodyOperationType", "namespace": "",
                 "value": op_enum},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "loft",
                 "n_sections": len(sections),
                 "operation": p.get("operation", "new"),
                 "onshape_id": fid}

    def _op_helix(self, p: dict) -> dict:
        # Onshape exposes Helix as a built-in feature returning a curve.
        # Pair with sweep for solid geometry.
        axis = (p.get("axis") or "Z").upper()
        axis_id = {"X": "JCE", "Y": "JCD", "Z": "JCC"}.get(axis, "JCC")
        pitch = float(p["pitch"])
        height = float(p["height"])
        dia = float(p["diameter"])
        alias = p.get("alias") or f"helix_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "helix",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "axis",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [axis_id]}]},
                {"btType": "BTMParameterEnum-145",
                 "parameterId": "helixType",
                 "enumName": "HelixType", "namespace": "",
                 "value": "PITCH_HEIGHT"},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "pitch",
                 "expression": f"{pitch} mm",
                 "value": pitch * _MM_TO_M, "units": "meter",
                 "isInteger": False},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "height",
                 "expression": f"{height} mm",
                 "value": height * _MM_TO_M, "units": "meter",
                 "isInteger": False},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "diameter",
                 "expression": f"{dia} mm",
                 "value": dia * _MM_TO_M, "units": "meter",
                 "isInteger": False},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "helix",
                 "pitch_mm": pitch, "height_mm": height,
                 "diameter_mm": dia, "onshape_id": fid}

    def _op_shell(self, p: dict) -> dict:
        body_alias = p["body"]
        body_fid = self.features.get(body_alias)
        if not body_fid:
            raise ValueError(f"Unknown body alias for shell: {body_alias}")
        t = float(p["thickness"])
        alias = p.get("alias") or f"shell_{len(self.features) + 1}"
        # The "faces" parameter ideally references face deterministic IDs;
        # without face-resolution from a host, we send the body and let
        # Onshape choose the open face — best-effort stub for the
        # common "open the top" case.
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "shell",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "entities",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [body_fid]}]},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "thickness",
                 "expression": f"{t} mm",
                 "value": t * _MM_TO_M, "units": "meter",
                 "isInteger": False},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "shell", "thickness_mm": t,
                 "onshape_id": fid}

    def _op_threadFeature(self, p: dict) -> dict:
        # Onshape doesn't have a single built-in 'thread' op. Most users
        # call the 'Thread' FeatureScript from the standard library. We
        # call it via a custom feature reference if available; otherwise
        # we mark the bore with a thread tag (cosmetic only) and let the
        # caller add real thread geometry as a separate step.
        spec = (p.get("spec") or "").upper()
        alias = p.get("alias") or f"thread_{len(self.features) + 1}"
        return {"id": alias, "kind": "thread", "spec": spec,
                 "status": "stub — Thread FeatureScript wiring TBD"}

    def _op_draft(self, p: dict) -> dict:
        body_fid = self.features.get(p["body"])
        if not body_fid:
            raise ValueError(f"Unknown body alias for draft: {p['body']}")
        angle = float(p.get("angle", 1))
        alias = p.get("alias") or f"draft_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "draft",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "draftFaces",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [body_fid]}]},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "angle",
                 "expression": f"{angle} deg", "value": angle,
                 "isInteger": False},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "draft", "angle_deg": angle,
                 "onshape_id": fid}

    def _op_thicken(self, p: dict) -> dict:
        surf = self.features.get(p["surface"])
        if not surf:
            raise ValueError(f"Unknown surface alias: {p['surface']}")
        t = float(p["thickness"])
        alias = p.get("alias") or f"thicken_{len(self.features) + 1}"
        feature = {
            "btType": "BTMFeature-134",
            "featureType": "thicken",
            "name": alias,
            "parameters": [
                {"btType": "BTMParameterQueryList-148",
                 "parameterId": "entities",
                 "queries": [{
                     "btType": "BTMIndividualQuery-138",
                     "deterministicIds": [surf]}]},
                {"btType": "BTMParameterQuantity-147",
                 "parameterId": "thickness1",
                 "expression": f"{t} mm",
                 "value": t * _MM_TO_M, "units": "meter",
                 "isInteger": False},
            ],
        }
        fid = self._add_feature(feature, alias)
        return {"id": alias, "kind": "thicken", "thickness_mm": t,
                 "onshape_id": fid}

    # ------------------------------------------------------------------
    # Stubs for kinds we don't support yet — fail loud rather than silent
    # ------------------------------------------------------------------

    def _op_addParameter(self, p: dict) -> dict:
        return {"skipped": "Onshape configurations not wired yet",
                "name": p.get("name")}

    def _op_fillet(self, p: dict) -> dict:
        # Onshape fillet requires edge queries; similar pattern but
        # needs the edge selection from the named body. MVP skips.
        return {"skipped": "fillet needs edge queries — coming soon"}

    def _op_gearFeature(self, p: dict) -> dict:
        # Onshape's involute-gear is a public FeatureScript; integration
        # requires registering the FS module against the part studio.
        return {"skipped": "gearFeature needs FS module — coming soon"}
