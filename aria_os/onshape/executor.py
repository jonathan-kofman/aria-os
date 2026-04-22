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

    # Stubs for kinds we don't support yet — fail loud rather than silent
    def _op_addParameter(self, p: dict) -> dict:
        return {"skipped": "Onshape configurations not wired yet",
                "name": p.get("name")}

    def _op_fillet(self, p: dict) -> dict:
        # Onshape fillet requires edge queries; similar pattern but
        # needs the edge selection from the named body. MVP skips.
        return {"skipped": "fillet needs edge queries — coming soon"}

    def _op_sketchRect(self, p: dict) -> dict:
        return {"skipped": "sketchRect not yet wired for Onshape"}
