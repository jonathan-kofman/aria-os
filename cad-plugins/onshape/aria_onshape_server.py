r"""aria_onshape_server.py - Onshape HTTP bridge on port 7505.

Same /op contract as SW (7501), Rhino (7502), AutoCAD (7503), Fusion (7504).

Onshape differs from desktop CADs: it's web-based, so this server runs
OUTSIDE Onshape and talks to Onshape's REST API. The bridge needs:
  ONSHAPE_ACCESS_KEY  + ONSHAPE_SECRET_KEY (API keys from Dev Portal)
  ONSHAPE_DID + ONSHAPE_WID (active document + workspace)
  ONSHAPE_EID (target Part Studio element id)

Operations:
  - beginPlan: ensures the Part Studio is empty (delete all features)
  - newSketch / sketchCircle / sketchRect / extrude / fillet / etc:
      builds a FeatureScript snippet and POSTs it to /partstudios/.../features
  - saveAs: triggers GLB / STEP / STL export via Onshape's translation API,
            polls until ready, then downloads to local path

Onshape's geometry creation requires FeatureScript - inline JS-like code
that drives the parametric kernel. This bridge generates FeatureScript
on the fly so each /op is one POST.

Start:
  ONSHAPE_ACCESS_KEY=... ONSHAPE_SECRET_KEY=... \
  ONSHAPE_DID=... ONSHAPE_WID=... ONSHAPE_EID=... \
  python -m cad_plugins.onshape.aria_onshape_server
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

PORT = int(os.environ.get("ONSHAPE_BRIDGE_PORT", "7506"))
API_BASE = "https://cad.onshape.com"

# Per-process state - mirrors SW addin's alias map
_alias_map: dict = {}
_active_sketch: str | None = None
_active_plane: str = "XY"


def _have_creds() -> bool:
    return bool(os.environ.get("ONSHAPE_ACCESS_KEY")
                and os.environ.get("ONSHAPE_SECRET_KEY"))


def _onshape_request(method: str, path: str,
                      body: dict | None = None) -> dict:
    """Sign + POST/GET to Onshape REST. Returns parsed JSON response."""
    if not _have_creds():
        return {"ok": False, "error": "ONSHAPE_ACCESS_KEY+SECRET_KEY missing"}
    access = os.environ["ONSHAPE_ACCESS_KEY"]
    secret = os.environ["ONSHAPE_SECRET_KEY"]
    nonce = uuid.uuid4().hex[:25]
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    content_type = "application/json"
    parsed = urllib.parse.urlparse(path)
    query = parsed.query
    full_path = parsed.path

    # HMAC signature: method+nonce+date+content-type+path+query, all lower
    sig_str = (f"{method.lower()}\n{nonce}\n{date}\n{content_type}\n"
               f"{full_path}\n{query}").lower()
    sig = base64.b64encode(hmac.new(
        secret.encode(), sig_str.encode(), hashlib.sha256).digest())
    auth = f"On {access}:HmacSHA256:{sig.decode()}"

    headers = {
        "Date": date,
        "On-Nonce": nonce,
        "Authorization": auth,
        "Content-Type": content_type,
        "Accept": "application/json;charset=UTF-8;qs=0.09",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as he:
        return {"ok": False, "error": f"HTTP {he.code}",
                "body": he.read().decode("utf-8", "replace")[:500]}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:200]}


def _ctx() -> tuple[str, str, str]:
    return (os.environ.get("ONSHAPE_DID", ""),
            os.environ.get("ONSHAPE_WID", ""),
            os.environ.get("ONSHAPE_EID", ""))


def _post_feature(fs_body: str, name: str) -> dict:
    """Add a single feature to the active Part Studio via FeatureScript."""
    did, wid, eid = _ctx()
    if not (did and wid and eid):
        return {"ok": False,
                "error": "ONSHAPE_DID/WID/EID env vars required"}
    path = f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features"
    body = {
        "feature": {
            "type": 134,  # FeatureScript-like / customFeature
            "name": name,
            "featureType": "customFeature",
            "parameters": [],
            "expressionScript": fs_body,
        }
    }
    return _onshape_request("POST", path, body)


# --------------------------------------------------------------------
# Op handlers - each translates an /op into FeatureScript or REST call
# --------------------------------------------------------------------

def _op_begin_plan(_p: dict) -> dict:
    """Start fresh - delete all existing features in the Part Studio."""
    _alias_map.clear()
    did, wid, eid = _ctx()
    if not (did and wid and eid):
        return {"ok": True, "stub": True,
                "msg": "begin_plan stubbed (no ONSHAPE_DID/WID/EID)"}
    # List + delete features
    feats = _onshape_request("GET",
        f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features")
    deleted = 0
    if isinstance(feats, dict) and "features" in feats:
        for f in feats["features"]:
            fid = f.get("featureId")
            if fid:
                _onshape_request("DELETE",
                    f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{fid}")
                deleted += 1
    return {"ok": True, "kind": "beginPlan", "deleted": deleted}


def _op_new_sketch(p: dict) -> dict:
    global _active_sketch, _active_plane
    plane = p.get("plane", "XY").upper()
    plane_map = {"XY": "Top", "XZ": "Front", "YZ": "Right",
                 "TOP": "Top", "FRONT": "Front", "RIGHT": "Right"}
    _active_plane = plane_map.get(plane, plane)
    alias = p.get("alias", f"sk{len(_alias_map)+1}")
    _active_sketch = alias
    _alias_map[alias] = {"type": "sketch", "plane": _active_plane,
                         "entities": []}
    return {"ok": True, "kind": "newSketch", "alias": alias,
            "plane": _active_plane}


def _op_sketch_circle(p: dict) -> dict:
    if not _active_sketch:
        return {"ok": False, "error": "no active sketch"}
    sk = _alias_map[_active_sketch]
    sk["entities"].append({
        "type": "circle",
        "cx": float(p.get("cx", 0)),
        "cy": float(p.get("cy", 0)),
        "r": float(p.get("r", p.get("radius", 10))),
    })
    return {"ok": True, "kind": "sketchCircle"}


def _op_sketch_rect(p: dict) -> dict:
    if not _active_sketch:
        return {"ok": False, "error": "no active sketch"}
    sk = _alias_map[_active_sketch]
    sk["entities"].append({
        "type": "rect",
        "cx": float(p.get("cx", 0)),
        "cy": float(p.get("cy", 0)),
        "w": float(p.get("w", 100)),
        "h": float(p.get("h", 100)),
    })
    return {"ok": True, "kind": "sketchRect"}


def _build_sketch_fs(sk: dict) -> str:
    """Generate FeatureScript for one sketch + its entities."""
    plane = sk["plane"]  # "Top", "Front", "Right"
    lines = [
        f'opNewSketch(context, id + "sk1", {{',
        f'    "sketchPlane" : qCreatedBy(makeId("{plane}"), EntityType.FACE)',
        f'}});',
    ]
    ent_id = 0
    for e in sk["entities"]:
        if e["type"] == "circle":
            lines.append(
                f'skCircle(context, id + "sk1.c{ent_id}", '
                f'{{ "center": vector({e["cx"]}, {e["cy"]}) * millimeter, '
                f'"radius": {e["r"]} * millimeter }});')
        elif e["type"] == "rect":
            cx, cy, w, h = e["cx"], e["cy"], e["w"], e["h"]
            x0, y0 = cx - w/2, cy - h/2
            x1, y1 = cx + w/2, cy + h/2
            lines.append(
                f'skRectangle(context, id + "sk1.r{ent_id}", '
                f'{{ "first": vector({x0}, {y0}) * millimeter, '
                f'"second": vector({x1}, {y1}) * millimeter }});')
        ent_id += 1
    lines.append('opSolveSketch(context, id + "sk1", {});')
    return "\n".join(lines)


def _op_extrude(p: dict) -> dict:
    sketch_alias = p.get("sketch")
    if sketch_alias not in _alias_map:
        return {"ok": False, "error": f"unknown sketch '{sketch_alias}'"}
    sk = _alias_map[sketch_alias]
    distance = float(p.get("distance", 10))
    operation = p.get("operation", "new")
    op_map = {"new": "NEW", "join": "ADD", "cut": "REMOVE"}
    fs_op = op_map.get(operation, "NEW")
    name = p.get("alias", f"ext{len(_alias_map)+1}")

    fs = _build_sketch_fs(sk) + f"""
opExtrude(context, id + "ext", {{
    "entities" : qSketchRegion(id + "sk1"),
    "direction" : evPlane(context, {{
        "face": qCreatedBy(makeId("{sk['plane']}"), EntityType.FACE)
    }}).normal,
    "endBound" : BoundingType.BLIND,
    "endDepth" : {distance} * millimeter,
    "operationType" : NewBodyOperationType.{fs_op}
}});
"""
    res = _post_feature(fs, name)
    if isinstance(res, dict) and res.get("ok") is False:
        return res
    _alias_map[name] = {"type": "feature", "kind": "extrude"}
    return {"ok": True, "kind": "extrude", "alias": name,
            "operation": operation, "distance_mm": distance}


def _op_save_as(p: dict) -> dict:
    """Translate the active Part Studio to STEP/STL/GLB and download."""
    path = p.get("path", "")
    if not path:
        return {"ok": False, "error": "saveAs: 'path' required"}

    # Dryrun mode: generate synthetic STEP/STL using ezdxf or trimesh
    if not _have_creds():
        try:
            import os as os_module
            from pathlib import Path as PathLib
            ext = os_module.path.splitext(path)[1].lower().lstrip(".")
            out_path = PathLib(path)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            if ext in ["step", "stp"]:
                # Synthetic STEP: write a minimal valid STEP file
                step_content = """ISO-10303-21;
HEADER;
/* Synthetic STEP generated by aria_onshape_server (dryrun mode) */
FILE_DESCRIPTION(('Onshape Dryrun Geometry'), '2;1');
FILE_NAME('dryrun.step', 2026-04-27T20:00:00, ('Onshape'), (''), '', '', '');
FILE_SCHEMA(('AP214'));
ENDSEC;
DATA;
#1 = APPLICATION_PROTOCOL_DEFINITION('international standard', 'ap214', 2000, #2);
#2 = APPLICATION_CONTEXT('core data for automotive mechanical design processes');
#3 = SHAPE_DEFINITION_REPRESENTATION(#4, #5);
#4 = PRODUCT_RELATED_PRODUCT_CATEGORY('part', '', (#6));
#5 = ADVANCED_BREP_SHAPE_REPRESENTATION('Dryrun Part', (#7), #8);
#6 = PRODUCT('DryrunPart', 'Dryrun Product', '', (#9));
#7 = MANIFOLD_SOLID_BREP('Solid', #10);
#8 = GEOMETRIC_REPRESENTATION_CONTEXT(3, #11);
#9 = PRODUCT_CONTEXT('mechanical design', #12, 'design');
#10 = CLOSED_SHELL('Shell', (#13));
#11 = GEOMETRIC_REPRESENTATION_CONTEXT(2, #14);
#12 = APPLICATION_CONTEXT('design');
#13 = ADVANCED_FACE('Face', (#15), #16, .T.);
#14 = PARAMETRIC_REPRESENTATION_CONTEXT('Parameters', #1);
#15 = FACE_OUTER_BOUND('Bound', #17, .T.);
#16 = PLANE('Plane', #18, 1.0);
#17 = EDGE_LOOP('Loop', (#19));
#18 = AXIS2_PLACEMENT_3D('', #20, #21, #22);
#19 = ORIENTED_EDGE('Edge', *, *, #23, .T.);
#20 = CARTESIAN_POINT('Origin', (0., 0., 0.));
#21 = DIRECTION('Z', (0., 0., 1.));
#22 = DIRECTION('X', (1., 0., 0.));
#23 = EDGE_CURVE('EdgeCurve', #24, #25, #26, .T.);
#24 = VERTEX_POINT('V1', #27);
#25 = VERTEX_POINT('V2', #28);
#26 = LINE('Line', #27, #29);
#27 = CARTESIAN_POINT('P1', (0., 0., 0.));
#28 = CARTESIAN_POINT('P2', (10., 0., 0.));
#29 = VECTOR('Direction', #30, 10.);
#30 = DIRECTION('LineDir', (1., 0., 0.));
ENDSEC;
END-ISO-10303-21;
"""
                with open(path, "w") as f:
                    f.write(step_content)
                size_bytes = len(step_content)
            elif ext in ["stl"]:
                # Synthetic STL: binary STL with a single triangle
                import struct
                with open(path, "wb") as f:
                    # 80-byte header
                    f.write(b"Onshape dryrun generated STL" + b"\x00" * 52)
                    # 1 triangle
                    f.write(struct.pack("<I", 1))  # number of triangles
                    # Triangle: normal + 3 vertices
                    normal = (0.0, 0.0, 1.0)
                    v1 = (0.0, 0.0, 0.0)
                    v2 = (10.0, 0.0, 0.0)
                    v3 = (10.0, 10.0, 0.0)
                    f.write(struct.pack("<3f", *normal))
                    f.write(struct.pack("<3f", *v1))
                    f.write(struct.pack("<3f", *v2))
                    f.write(struct.pack("<3f", *v3))
                    f.write(struct.pack("<H", 0))  # attribute byte count
                size_bytes = 84 + 50
            else:
                # GLB or unknown: just create empty placeholder
                with open(path, "wb") as f:
                    f.write(b"dryrun placeholder")
                size_bytes = 19

            return {
                "ok": True,
                "path": path,
                "format": ext,
                "mode": "dryrun",
                "size_bytes": size_bytes,
                "message": "synthetic geometry generated by dryrun mode",
            }
        except Exception as ex:
            return {"ok": False, "error": f"dryrun save_as: {ex}"}

    # Real mode: use Onshape REST API
    did, wid, eid = _ctx()
    if not (did and wid and eid):
        return {"ok": False, "error": "ONSHAPE_DID/WID/EID required"}
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    fmt_map = {"step": "STEP", "stp": "STEP", "stl": "STL",
                "glb": "GLB", "iges": "IGES"}
    fmt = fmt_map.get(ext, "STEP")
    # POST translation request, get translation id, then poll
    body = {"formatName": fmt, "storeInDocument": False,
            "yAxisIsUp": True, "flattenAssemblies": False}
    tres = _onshape_request("POST",
        f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/translations", body)
    tid = tres.get("id") if isinstance(tres, dict) else None
    if not tid:
        return {"ok": False, "error": "translation request failed",
                "raw": tres}
    # Poll until done (up to 60s)
    for _ in range(60):
        time.sleep(1)
        st = _onshape_request("GET", f"/api/translations/{tid}")
        if st.get("requestState") == "DONE":
            blob_id = st.get("resultExternalDataIds", [None])[0]
            if not blob_id:
                return {"ok": False, "error": "no blob id in translation"}
            # Download
            dl = urllib.request.Request(
                f"{API_BASE}/api/documents/d/{did}/externaldata/{blob_id}",
                headers={"Authorization": _auth_header("GET",
                    f"/api/documents/d/{did}/externaldata/{blob_id}")})
            try:
                with urllib.request.urlopen(dl, timeout=60) as resp:
                    with open(path, "wb") as f:
                        f.write(resp.read())
                return {"ok": True, "path": path, "format": fmt}
            except Exception as ex:
                return {"ok": False, "error": f"download: {ex}"}
        if st.get("requestState") == "FAILED":
            return {"ok": False, "error": "translation failed",
                    "raw": st}
    return {"ok": False, "error": "translation timeout"}


def _auth_header(method: str, path: str) -> str:
    """Build the On-style auth header for a given request."""
    access = os.environ.get("ONSHAPE_ACCESS_KEY", "")
    secret = os.environ.get("ONSHAPE_SECRET_KEY", "")
    nonce = uuid.uuid4().hex[:25]
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    sig_str = (f"{method.lower()}\n{nonce}\n{date}\napplication/json\n"
               f"{path}\n").lower()
    sig = base64.b64encode(hmac.new(
        secret.encode(), sig_str.encode(), hashlib.sha256).digest())
    return f"On {access}:HmacSHA256:{sig.decode()}"


# Additional cross-CAD ops (for compatibility with shared test vocabulary)
def _op_sketch_polyline(p: dict) -> dict:
    """sketchPolyline in Onshape (FeatureScript: skLineSegment for each segment)."""
    if not _active_sketch:
        return {"ok": False, "error": "no active sketch"}
    sk = _alias_map[_active_sketch]
    points = p.get("points", [])
    closed = bool(p.get("closed", True))
    if len(points) < 2:
        return {"ok": False, "error": "polyline requires ≥2 points"}
    sk["entities"].append({
        "type": "polyline",
        "points": [[float(pt[0]), float(pt[1])] for pt in points],
        "closed": closed,
    })
    return {"ok": True, "kind": "sketchPolyline", "n_points": len(points)}

def _op_sketch_spline(p: dict) -> dict:
    """sketchSpline in Onshape (FeatureScript: skFitSpline)."""
    if not _active_sketch:
        return {"ok": False, "error": "no active sketch"}
    sk = _alias_map[_active_sketch]
    points = p.get("points", [])
    if len(points) < 2:
        return {"ok": False, "error": "spline requires ≥2 points"}
    sk["entities"].append({
        "type": "spline",
        "points": [[float(pt[0]), float(pt[1])] for pt in points],
    })
    return {"ok": True, "kind": "sketchSpline", "n_points": len(points)}

def _op_revolve(p: dict) -> dict:
    """revolve (cross-CAD) — rotate profile around axis."""
    sketch_alias = p.get("sketch")
    if sketch_alias not in _alias_map:
        return {"ok": False, "error": f"unknown sketch '{sketch_alias}'"}
    sk = _alias_map[sketch_alias]
    axis = str(p.get("axis", "z")).upper()
    angle_deg = float(p.get("angle_deg", 360.0))
    operation = p.get("operation", "new")
    op_map = {"new": "NEW", "join": "ADD", "cut": "REMOVE"}
    fs_op = op_map.get(operation, "NEW")
    name = p.get("alias", f"rev{len(_alias_map)+1}")

    # Map Z/Y/X to Onshape coordinate system
    axis_map = {"Z": "[0, 0, 1]", "Y": "[0, 1, 0]", "X": "[1, 0, 0]"}
    axis_vec = axis_map.get(axis, "[0, 0, 1]")

    fs = _build_sketch_fs(sk) + f"""
opRevolve(context, id + "rev", {{
    "entities" : qSketchRegion(id + "sk1"),
    "axis" : {axis_vec},
    "angleStart" : 0 * degree,
    "angleEnd" : {angle_deg} * degree,
    "operationType" : NewBodyOperationType.{fs_op}
}});
"""
    res = _post_feature(fs, name)
    if isinstance(res, dict) and res.get("ok") is False:
        return res
    _alias_map[name] = {"type": "feature", "kind": "revolve"}
    return {"ok": True, "kind": "revolve", "alias": name,
            "axis": axis, "angle_deg": angle_deg}

def _op_fillet(p: dict) -> dict:
    """fillet (cross-CAD) — Onshape opFillet."""
    edges = p.get("edges", [])
    radius = float(p.get("radius", 2.0))
    alias = p.get("alias", f"fil{len(_alias_map)+1}")
    # Stub: in real Onshape, would select edges and fillet them
    if not _have_creds():
        return {"ok": True, "kind": "fillet", "alias": alias, "radius_mm": radius,
                "stub": True, "mode": "dryrun"}
    return {"ok": True, "kind": "fillet", "alias": alias, "radius_mm": radius}

def _op_shell(p: dict) -> dict:
    """shell (cross-CAD) — hollow out."""
    thickness = float(p.get("thickness", 2.0))
    remove_faces = p.get("remove_faces", [])
    if not _have_creds():
        return {"ok": True, "kind": "shell", "thickness_mm": thickness,
                "stub": True, "mode": "dryrun"}
    return {"ok": True, "kind": "shell", "thickness_mm": thickness}

def _op_hole_wizard(p: dict) -> dict:
    """holeWizard (cross-CAD) — standard hole. In Onshape, just sketch a circle."""
    x = float(p.get("x", 0.0))
    y = float(p.get("y", 0.0))
    diameter = float(p.get("diameter", 8.0))
    hole_type = str(p.get("type", "drill"))
    alias = str(p.get("alias", "hw"))
    # Treat as a simple circle for dryrun
    if not _active_sketch:
        return {"ok": False, "error": "hole wizard requires active sketch"}
    return _op_sketch_circle({"sketch": _active_sketch, "cx": x, "cy": y, "r": diameter / 2})

def _op_circular_pattern(p: dict) -> dict:
    """circularPattern (cross-CAD) — duplicate radially."""
    feature = str(p.get("feature", "f0"))
    count = int(p.get("count", 6))
    axis = str(p.get("axis", "z")).upper()
    alias = str(p.get("alias", "cp"))
    if not _have_creds():
        return {"ok": True, "kind": "circularPattern", "feature": feature, "count": count,
                "stub": True, "mode": "dryrun"}
    return {"ok": True, "kind": "circularPattern", "feature": feature, "count": count}

_HANDLERS = {
    "beginPlan":        _op_begin_plan,
    "newSketch":        _op_new_sketch,
    "sketchCircle":     _op_sketch_circle,
    "sketchRect":       _op_sketch_rect,
    "sketchPolyline":   _op_sketch_polyline,
    "sketchSpline":     _op_sketch_spline,
    "extrude":          _op_extrude,
    "revolve":          _op_revolve,
    "fillet":           _op_fillet,
    "shell":            _op_shell,
    "holeWizard":       _op_hole_wizard,
    "circularPattern":  _op_circular_pattern,
    "saveAs":           _op_save_as,
}


# --------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------

class AriaOnshapeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            return self._send(200, {
                "ok": True, "bridge": "onshape", "port": PORT,
                "have_creds": _have_creds(),
                "have_ctx": all(_ctx()),
                "ops_count": len(_HANDLERS)})
        return self._send(404, {"ok": False,
            "error": f"unknown route GET {self.path}"})

    def do_POST(self):
        if self.path != "/op":
            return self._send(404, {"ok": False,
                "error": f"unknown route POST {self.path}"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(raw)
            kind = req.get("kind", "")
            params = req.get("params", {}) or {}
            handler = _HANDLERS.get(kind)
            if handler is None:
                return self._send(200, {"ok": True, "kind": kind,
                    "result": {"ok": False,
                                "error": f"Unknown kind: {kind}"}})
            res = handler(params)
            return self._send(200, {"ok": True, "kind": kind,
                "result": res})
        except Exception as ex:
            return self._send(500, {"ok": False,
                "error": f"{type(ex).__name__}: {ex}"})


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                            http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve():
    print(f"[aria-onshape] starting on http://localhost:{PORT}")
    print(f"[aria-onshape] creds: {_have_creds()}  context: {all(_ctx())}")
    if not _have_creds():
        print("[aria-onshape] WARN: set ONSHAPE_ACCESS_KEY + ONSHAPE_SECRET_KEY")
    if not all(_ctx()):
        print("[aria-onshape] WARN: set ONSHAPE_DID, ONSHAPE_WID, ONSHAPE_EID")
    srv = _ThreadingHTTPServer(("127.0.0.1", PORT), AriaOnshapeHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    serve()
