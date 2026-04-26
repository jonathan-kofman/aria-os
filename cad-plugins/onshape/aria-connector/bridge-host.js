/**
 * bridge-host.js — Onshape outer-iframe bridge host.
 *
 * This script runs in the Onshape application tab (the outer context that has
 * access to the Onshape REST API via an OAuth2 access token). The ARIA React
 * panel lives in a nested iframe; it posts bridge messages via window.parent
 * which arrive here.
 *
 * Flow:
 *   React panel (inner iframe)
 *     → window.parent.postMessage({action, _id, ...payload}, "*")
 *       → bridge-host.js (this file, outer iframe / tab)
 *         → Onshape REST API
 *           → postMessage reply {_id, result|error} back to inner iframe
 *
 * IMPLEMENTED:
 *   getCurrentDocument  -- GET /api/documents/d/{did}/w/{wid}
 *
 * STUBBED (returns {error: "not implemented"}):
 *   getSelection, insertGeometry (see note), updateParameter,
 *   getFeatureTree, exportCurrent, showNotification, openFile
 *
 * insertGeometry note:
 *   Onshape does NOT allow arbitrary 3D import from a third-party URL via
 *   client-side script. To insert geometry you must:
 *     1. POST the binary to POST /api/blobelements/d/{did}/w/{wid}  (import endpoint)
 *        with the file bytes and MIME type (model/step or model/stl).
 *     2. The stub below shows the skeleton. Full implementation requires a
 *        server-side proxy to fetch the STEP/STL binary and forward it because
 *        browser CORS blocks direct binary fetch from arbitrary origins.
 *
 * Usage:
 *   Include this file in the Onshape tab HTML page (the outer iframe).
 *   It self-initialises on load. Set ARIA_INNER_ORIGIN to the origin of the
 *   ARIA panel iframe for postMessage security (defaults to "*" for dev).
 */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // Configuration
  // -------------------------------------------------------------------------

  // Replace with your panel's origin in production, e.g. "https://aria.example.com"
  const INNER_ORIGIN = window.ARIA_INNER_ORIGIN || "*";

  // Onshape REST API base. Onshape itself provides the base URL in the tab context.
  const API_BASE = "https://cad.onshape.com";

  // The OAuth2 access token. In production this comes from your OAuth callback
  // handler and should be stored in sessionStorage or an in-memory variable.
  // Never expose it to the inner iframe.
  let _accessToken = window.ARIA_ACCESS_TOKEN || null;

  // The Onshape document context (did, wid, eid) injected by Onshape when it
  // opens your tab. Onshape passes these as URL query params on the tab URL.
  const _ctx = parseOnshapeContext();

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function parseOnshapeContext() {
    const p = new URLSearchParams(window.location.search);
    return {
      did: p.get("documentId") || p.get("did") || null,
      wid: p.get("workspaceId") || p.get("wid") || null,
      eid: p.get("elementId") || p.get("eid") || null,
    };
  }

  async function apiGet(path) {
    if (!_accessToken) throw new Error("No access token — complete OAuth flow first");
    const resp = await fetch(`${API_BASE}${path}`, {
      headers: {
        Authorization: `Bearer ${_accessToken}`,
        Accept: "application/json;charset=UTF-8;qs=0.09",
      },
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Onshape API ${resp.status}: ${text}`);
    }
    return resp.json();
  }

  async function apiPost(path, body) {
    if (!_accessToken) throw new Error("No access token — complete OAuth flow first");
    const resp = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${_accessToken}`,
        Accept: "application/json;charset=UTF-8;qs=0.09",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Onshape API ${resp.status}: ${text}`);
    }
    return resp.json();
  }

  // Forward a reply to the inner ARIA iframe.
  function replyTo(source, id, result, error) {
    const payload = error ? { _id: id, error } : { _id: id, result };
    source.postMessage(payload, INNER_ORIGIN);
  }

  // -------------------------------------------------------------------------
  // Bridge action implementations
  // -------------------------------------------------------------------------

  // REAL: getCurrentDocument
  async function getCurrentDocument() {
    const { did, wid } = _ctx;
    if (!did || !wid) throw new Error("Onshape document context not available");
    const doc = await apiGet(`/api/documents/d/${did}/w/${wid}`);
    return {
      name: doc.name,
      id: doc.id,
      units: "mm",           // Onshape internal unit is always mm
      type: "OnshapeDocument",
      defaultWorkspace: doc.defaultWorkspace?.id ?? wid,
    };
  }

  // STUBBED stubs ---------------------------------------------------------

  async function getSelection() {
    // Onshape exposes selections via the Onshape App Integration API
    // (window.OS.Application.getSelectedEntities) but that is only available
    // from within Onshape's own glassbox context, not via REST.
    // Implement via the Onshape glassbox postMessage API when running inside
    // the Onshape client.
    throw new Error("not implemented");
  }

  async function insertGeometry(payload) {
    // Skeleton: POST binary to the blob elements import endpoint.
    //   POST /api/blobelements/d/{did}/w/{wid}
    //   Content-Type: multipart/form-data
    //   file: <binary STEP/STL>
    //
    // Full implementation requires:
    //   1. A server-side proxy to fetch `payload.url` (CORS barrier).
    //   2. Repackage as multipart/form-data with encodingType + filename.
    //   3. POST to the import endpoint with OAuth2 bearer token.
    // See README for step-by-step guide.
    throw new Error("not implemented — see README insertGeometry section");
  }

  async function updateParameter(_payload) {
    throw new Error("not implemented");
  }

  async function getFeatureTree() {
    throw new Error("not implemented");
  }

  async function exportCurrent(_payload) {
    throw new Error("not implemented");
  }

  function showNotification(_payload) {
    // No native Onshape notification API from iframe context.
    // Could append a toast element to the outer iframe DOM.
    throw new Error("not implemented");
  }

  async function openFile(_payload) {
    throw new Error("not implemented");
  }

  // -------------------------------------------------------------------------
  // Native feature-tree execution — Onshape REST API
  //
  // Each op maps to a POST /api/partstudios/d/{did}/w/{wid}/e/{eid}/features
  // with a btMXxx feature JSON (Onshape's wire format for features).
  // On success, the new feature appears live in the Part Studio's feature
  // list (the real Onshape feature tree).
  //
  // Onshape uses METERS internally, so every mm value multiplies by 0.001.
  // -------------------------------------------------------------------------

  const _onshapeRegistry = Object.create(null);
  let _onshapeLastFeatureId = null;

  function _requireCtx() {
    const { did, wid, eid } = _ctx;
    if (!did || !wid || !eid)
      throw new Error("Onshape context missing — open the connector inside a Part Studio");
    return { did, wid, eid };
  }

  function _mm(v) { return Number(v) * 0.001; }  // mm → m

  async function _postFeature(feature) {
    const { did, wid, eid } = _requireCtx();
    const body = { feature };
    const result = await apiPost(
      `/api/partstudios/d/${did}/w/${wid}/e/${eid}/features`, body);
    _onshapeLastFeatureId = result.feature?.featureId
                          || result.featureId
                          || result.feature?.nodeId
                          || null;
    return result;
  }

  // Build an Onshape "BTMParameterQuantity" (length with unit)
  function _lengthParam(id, valueMm) {
    return {
      btType: "BTMParameterQuantity-147",
      parameterId: id,
      expression: `${Number(valueMm)} mm`,
      value: _mm(valueMm),
      units: "meter",
      isInteger: false,
    };
  }

  function _enumParam(id, namespaceStr, enumName, value) {
    return {
      btType: "BTMParameterEnum-145",
      parameterId: id,
      enumName,
      namespace: namespaceStr || "",
      value,
    };
  }

  function _queryParam(id, query) {
    return {
      btType: "BTMParameterQueryList-148",
      parameterId: id,
      queries: query,
    };
  }

  async function _opBeginPlan(_p) {
    _requireCtx();  // fail fast if no context
    for (const k in _onshapeRegistry) delete _onshapeRegistry[k];
    _onshapeLastFeatureId = null;
    return { ok: true, registry_cleared: true };
  }

  async function _opNewSketch(p) {
    const planeMap = {
      "XY": "JCC", // TOP plane (default mate connector for Part Studio)
      "XZ": "JCD", // FRONT plane
      "YZ": "JCE", // RIGHT plane
    };
    const plane = (p.plane || "XY").toUpperCase();
    const planeId = planeMap[plane] || "JCC";
    const alias = p.alias;
    const name = p.name || `ARIA Sketch ${Object.keys(_onshapeRegistry).length + 1}`;
    const feature = {
      btType: "BTMSketch-151",
      featureType: "newSketch",
      name,
      parameters: [
        _queryParam("sketchPlane", [{
          btType: "BTMIndividualQuery-138",
          deterministicIds: [planeId],
        }]),
      ],
      entities: [],
    };
    const reply = await _postFeature(feature);
    _onshapeRegistry[alias] = {
      kind: "sketch",
      featureId: _onshapeLastFeatureId,
      plane,
      entities: [],
    };
    return { ok: true, id: alias, kind: "sketch", name, onshape: reply.feature?.featureId };
  }

  async function _opSketchCircle(p) {
    const sk = _onshapeRegistry[p.sketch];
    if (!sk) throw new Error(`Unknown sketch alias: ${p.sketch}`);
    const cx = Number(p.cx || 0);
    const cy = Number(p.cy || 0);
    const r = Number(p.r);
    // Onshape sketch circles are added via a feature-update (edit sketch)
    // call against the sketch's featureId. This is the standard pattern.
    const updated = {
      btType: "BTMSketch-151",
      featureId: sk.featureId,
      entities: [
        ...sk.entities,
        {
          btType: "BTMSketchCurve-4",
          geometry: {
            btType: "BTCurveGeometryCircle-115",
            xCenter: _mm(cx),
            yCenter: _mm(cy),
            radius: _mm(r),
          },
          centerId: `${p.sketch}_c${sk.entities.length}`,
          entityId: `${p.sketch}_circle_${sk.entities.length}`,
        },
      ],
    };
    sk.entities = updated.entities;
    const { did, wid, eid } = _requireCtx();
    await apiPost(
      `/api/partstudios/d/${did}/w/${wid}/e/${eid}/features/featureid/${sk.featureId}`,
      { feature: updated });
    return { ok: true, kind: "circle", r_mm: r, cx_mm: cx, cy_mm: cy };
  }

  async function _opSketchRect(p) {
    const sk = _onshapeRegistry[p.sketch];
    if (!sk) throw new Error(`Unknown sketch alias: ${p.sketch}`);
    const w = Number(p.w), h = Number(p.h);
    const cx = Number(p.cx || 0), cy = Number(p.cy || 0);
    const pts = [
      [cx - w/2, cy - h/2],
      [cx + w/2, cy - h/2],
      [cx + w/2, cy + h/2],
      [cx - w/2, cy + h/2],
    ];
    const base = sk.entities.length;
    const newLines = pts.map((pt, i) => {
      const next = pts[(i + 1) % 4];
      return {
        btType: "BTMSketchCurveSegment-155",
        geometry: {
          btType: "BTCurveGeometryLine-117",
          pntX: _mm(pt[0]), pntY: _mm(pt[1]),
          dirX: 1, dirY: 0,
        },
        startPointId: `${p.sketch}_rect${base}_p${i}`,
        endPointId:   `${p.sketch}_rect${base}_p${(i + 1) % 4}`,
        entityId:     `${p.sketch}_rect${base}_l${i}`,
        startParam: 0,
        endParam: Math.hypot(next[0] - pt[0], next[1] - pt[1]) * 0.001,
      };
    });
    sk.entities = [...sk.entities, ...newLines];
    const updated = {
      btType: "BTMSketch-151",
      featureId: sk.featureId,
      entities: sk.entities,
    };
    const { did, wid, eid } = _requireCtx();
    await apiPost(
      `/api/partstudios/d/${did}/w/${wid}/e/${eid}/features/featureid/${sk.featureId}`,
      { feature: updated });
    return { ok: true, kind: "rect", w_mm: w, h_mm: h };
  }

  async function _opExtrude(p) {
    const sk = _onshapeRegistry[p.sketch];
    if (!sk) throw new Error(`Unknown sketch alias: ${p.sketch}`);
    const dist = Number(p.distance);
    const alias = p.alias || `extrude_${Object.keys(_onshapeRegistry).length + 1}`;
    const opMap = {
      "new":  "NEW",
      "cut":  "REMOVE",
      "join": "ADD",
      "intersect": "INTERSECT",
    };
    const opEnum = opMap[p.operation] || "NEW";
    const feature = {
      btType: "BTMFeature-134",
      featureType: "extrude",
      name: alias,
      parameters: [
        _queryParam("entities", [{
          btType: "BTMIndividualSketchRegionQuery-140",
          featureId: sk.featureId,
        }]),
        _enumParam("endBound", "BoundingType", "BLIND", "BLIND"),
        _enumParam("bodyType", "ExtendedToolBodyType",
                    opEnum === "NEW" ? "SOLID" : "SOLID", "SOLID"),
        _enumParam("operationType", "NewBodyOperationType",
                    opEnum, opEnum),
        _lengthParam("depth", Math.abs(dist)),
        {
          btType: "BTMParameterBoolean-144",
          parameterId: "oppositeDirection",
          value: dist < 0,
        },
      ],
    };
    const reply = await _postFeature(feature);
    _onshapeRegistry[alias] = {
      kind: "extrude",
      featureId: _onshapeLastFeatureId,
      operation: opEnum,
    };
    return { ok: true, id: alias, kind: "extrude",
              distance_mm: dist, operation: p.operation,
              onshape: reply.feature?.featureId };
  }

  async function _opCircularPattern(p) {
    const src = _onshapeRegistry[p.feature];
    if (!src) throw new Error(`Unknown feature alias: ${p.feature}`);
    const count = Number(p.count || 2);
    const axis = (p.axis || "Z").toUpperCase();
    const axisId = { "X": "JCE", "Y": "JCD", "Z": "JCC" }[axis] || "JCC";
    const alias = p.alias || `pattern_${Object.keys(_onshapeRegistry).length + 1}`;
    const feature = {
      btType: "BTMFeature-134",
      featureType: "pattern",
      name: alias,
      parameters: [
        _enumParam("patternType", "PatternType", "CIRCULAR", "CIRCULAR"),
        _enumParam("patternBodyType", "PatternBodyType", "PART", "PART"),
        _queryParam("axis", [{
          btType: "BTMIndividualQuery-138",
          deterministicIds: [axisId],
        }]),
        _queryParam("instanceFunction", [{
          btType: "BTMIndividualQuery-138",
          deterministicIds: [src.featureId],
        }]),
        { btType: "BTMParameterQuantity-147",
          parameterId: "instanceCount",
          expression: `${count}`, value: count, isInteger: true },
        _lengthParam("angle", 360 / count),
      ],
    };
    const reply = await _postFeature(feature);
    _onshapeRegistry[alias] = {
      kind: "pattern",
      featureId: _onshapeLastFeatureId,
    };
    return { ok: true, id: alias, kind: "circular_pattern",
              count, axis, onshape: reply.feature?.featureId };
  }

  async function _opFillet(p) {
    const body = _onshapeRegistry[p.body];
    if (!body) throw new Error(`Unknown body alias: ${p.body}`);
    const r = Number(p.r);
    const alias = p.alias || `fillet_${Object.keys(_onshapeRegistry).length + 1}`;
    const feature = {
      btType: "BTMFeature-134",
      featureType: "fillet",
      name: alias,
      parameters: [
        _queryParam("entities", [{
          btType: "BTMIndividualQuery-138",
          deterministicIds: [body.featureId],
        }]),
        _enumParam("filletType", "FilletType", "EDGE", "EDGE"),
        _lengthParam("radius", r),
      ],
    };
    const reply = await _postFeature(feature);
    _onshapeRegistry[alias] = {
      kind: "fillet",
      featureId: _onshapeLastFeatureId,
    };
    return { ok: true, id: alias, kind: "fillet", r_mm: r,
              onshape: reply.feature?.featureId };
  }

  // --- Onshape-native leverage: configurations, branches, FeatureScript ---

  async function _opCreateConfiguration(p) {
    const { did, wid, eid } = _requireCtx();
    const name = p.name || `ARIA Config ${Date.now()}`;
    // Onshape REST: POST /api/partstudios/.../configuration
    // to set named configuration parameters. Each ARIA refinement
    // iteration becomes a configuration of the same Part Studio.
    const body = {
      configurationParameters: [
        {
          btType: "BTMConfigurationParameterEnum-105",
          parameterId: "__ariaIter",
          defaultValue: name,
          optionName: name,
        },
      ],
    };
    const result = await apiPost(
      `/api/partstudios/d/${did}/w/${wid}/e/${eid}/configuration`,
      body);
    return { ok: true, kind: "configuration", name, result };
  }

  async function _opCreateBranch(p) {
    const { did, wid } = _requireCtx();
    const branchName = p.name || `aria_iter_${Date.now()}`;
    // Onshape branches let RefinerAgent commit each try as its own
    // branch off the current workspace — users compare visually.
    const result = await apiPost(
      `/api/documents/d/${did}/workspaces/${wid}/branch`,
      { name: branchName, description: p.description || "ARIA refinement" });
    return { ok: true, kind: "branch",
              name: branchName, workspaceId: result.id };
  }

  async function _opEmitFeatureScript(p) {
    // Return a FeatureScript snippet the user can paste into a
    // Feature Studio tab. This is Onshape's unique programmable-
    // feature language. MVP emits a parametric flange as FS.
    const params = p.params || {};
    const fs = `
FeatureScript 2134;
import(path : "onshape/std/geometry.fs", version : "2134.0");

annotation { "Feature Type Name" : "ARIA Flange" }
export const ariaFlange = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Outer diameter" }
        isLength(definition.od, LENGTH_BOUNDS);
        annotation { "Name" : "Bore diameter" }
        isLength(definition.bore, LENGTH_BOUNDS);
        annotation { "Name" : "Thickness" }
        isLength(definition.thickness, LENGTH_BOUNDS);
        annotation { "Name" : "Number of bolt holes" }
        isInteger(definition.n_bolts, { (unitless) : [1, 4, 32] } as IntegerBoundSpec);
        annotation { "Name" : "Bolt circle radius" }
        isLength(definition.bolt_r, LENGTH_BOUNDS);
        annotation { "Name" : "Bolt hole diameter" }
        isLength(definition.bolt_dia, LENGTH_BOUNDS);
    }
    {
        var sk = newSketchOnPlane(context, id + "sk", {
            "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE)
        });
        skCircle(sk, "c1", { "center" : vector(0, 0) * meter,
                              "radius" : definition.od / 2 });
        skSolve(sk);
        opExtrude(context, id + "ext", {
            "entities" : qSketchRegion(id + "sk"),
            "direction" : vector(0, 0, 1),
            "endBound" : BoundingType.BLIND,
            "endDepth" : definition.thickness,
        });
        // Bolt holes
        var holeSk = newSketchOnPlane(context, id + "holeSk", {
            "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE)
        });
        skCircle(holeSk, "boltHole",
            { "center" : vector(definition.bolt_r / meter, 0) * meter,
              "radius" : definition.bolt_dia / 2 });
        skSolve(holeSk);
        opExtrude(context, id + "cutBolts", {
            "entities" : qSketchRegion(id + "holeSk"),
            "direction" : vector(0, 0, 1),
            "endBound" : BoundingType.THROUGH_ALL,
            "operationType" : NewBodyOperationType.REMOVE,
        });
        opPattern(context, id + "patBolts", {
            "patternType" : PatternType.PART,
            "instanceFunction" : qCreatedBy(id + "cutBolts"),
            "axis" : Z_AXIS,
            "instanceCount" : definition.n_bolts,
            "angle" : 360 * degree,
        });
        // Bore
        var boreSk = newSketchOnPlane(context, id + "boreSk", {
            "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE)
        });
        skCircle(boreSk, "bore", { "center" : vector(0, 0) * meter,
                                     "radius" : definition.bore / 2 });
        skSolve(boreSk);
        opExtrude(context, id + "cutBore", {
            "entities" : qSketchRegion(id + "boreSk"),
            "direction" : vector(0, 0, 1),
            "endBound" : BoundingType.THROUGH_ALL,
            "operationType" : NewBodyOperationType.REMOVE,
        });
    });
`.trim();
    return { ok: true, kind: "feature_script",
              featureScript: fs, name: p.name || "ariaFlange" };
  }

  const _OP_HANDLERS = {
    beginPlan:       _opBeginPlan,
    newSketch:       _opNewSketch,
    sketchCircle:    _opSketchCircle,
    sketchRect:      _opSketchRect,
    extrude:         _opExtrude,
    circularPattern: _opCircularPattern,
    fillet:          _opFillet,
    // Onshape-native leverage
    createConfiguration: _opCreateConfiguration,
    createBranch:        _opCreateBranch,
    emitFeatureScript:   _opEmitFeatureScript,
  };

  async function executeFeature(kind, params) {
    const handler = _OP_HANDLERS[kind];
    if (!handler) throw new Error(`Unknown feature kind: ${kind}`);
    return await handler(params || {});
  }

  // -------------------------------------------------------------------------
  // Message dispatch
  //
  // Plan ops must execute in arrival order — the React panel posts
  // newSketch → sketchCircle → extrude as a single semantic chain, and
  // running them concurrently lets the extrude beat its sketchCircle to
  // the Onshape API and fail. We serialize every incoming message via a
  // promise chain. Same fix applied to the Rhino + SolidWorks bridges.
  // -------------------------------------------------------------------------

  let _opChain = Promise.resolve();

  window.addEventListener("message", (event) => {
    // Security: in production replace "*" check with origin allowlist.
    const data = typeof event.data === "string"
      ? JSON.parse(event.data)
      : event.data;

    const { action, _id: id, ...payload } = data || {};
    if (!action || !id) return;

    const src = event.source;
    _opChain = _opChain.then(() => _processMessage(src, id, action, payload));
  });

  async function _processMessage(src, id, action, payload) {
    try {
      let result;
      switch (action) {
        case "getCurrentDocument":
          result = await getCurrentDocument();
          break;
        case "getSelection":
          result = await getSelection();
          break;
        case "insertGeometry":
          result = await insertGeometry(payload);
          break;
        case "updateParameter":
          result = await updateParameter(payload);
          break;
        case "getFeatureTree":
          result = await getFeatureTree();
          break;
        case "exportCurrent":
          result = await exportCurrent(payload);
          break;
        case "showNotification":
          result = showNotification(payload);
          break;
        case "openFile":
          result = await openFile(payload);
          break;
        case "executeFeature":
          result = await executeFeature(payload.kind, payload.params);
          break;
        default:
          throw new Error(`unknown action: ${action}`);
      }
      replyTo(src, id, result);
    } catch (err) {
      replyTo(src, id, undefined, err.message);
    }
  }

  // -------------------------------------------------------------------------
  // OAuth callback handler
  // -------------------------------------------------------------------------

  // Called by your OAuth redirect page after exchanging the code for a token.
  // In production: set this from your backend redirect handler via postMessage
  // or a shared session cookie — do NOT pass the token through the inner iframe.
  window.ariaSetAccessToken = function (token) {
    _accessToken = token;
    console.log("[ARIA bridge-host] Access token set.");
  };

  console.log("[ARIA bridge-host] Loaded. Document context:", _ctx);
})();
