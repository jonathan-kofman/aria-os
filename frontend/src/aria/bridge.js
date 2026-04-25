/**
 * ARIA CAD host bridge — uniform surface for the React panel to talk to
 * whichever CAD is hosting it.
 *
 * Hosts (detected at runtime, in priority order):
 *   - Fusion 360  → window.fusionJavaScriptHandler + adsk.core.HTMLEventArgs
 *   - Rhino       → window.chrome.webview (WebView2) / window.webkit.messageHandlers (Mac WebKit)
 *   - Onshape     → window.parent iframe postMessage (Onshape glassbox)
 *   - SolidWorks  → window.chrome.webview (WebView2 Task Pane)
 *   - Standalone  → no host; download-only mode; null bridge
 *
 * Contract (8 calls, matches the spec in the product brief):
 *
 *   bridge.kind                → "fusion" | "rhino" | "onshape" | "solidworks" | null
 *   bridge.getCurrentDocument() → Promise<{name, id, units, type}>
 *   bridge.getSelection()       → Promise<Array<{id, type, metadata}>>
 *   bridge.insertGeometry(url)  → Promise<{inserted: true, id}>    // STEP/STL at URL
 *   bridge.updateParameter(name, value) → Promise<{ok: true}>
 *   bridge.getFeatureTree()     → Promise<object>  // host-defined structure
 *   bridge.exportCurrent(format)→ Promise<{url}>   // "step" | "stl" | "dxf"
 *   bridge.showNotification(msg, tone)  → void    // tone: "info"|"success"|"error"
 *   bridge.openFile(path)       → Promise<{opened: true}>
 *
 * Every method returns a Promise so the React UI can await them uniformly.
 * When there's no host, methods reject with `{code: "no_host", message: ...}`
 * — callers should catch and fall back to "download file locally" affordance.
 */

const NO_HOST_ERR = () =>
  Object.assign(new Error("No CAD host detected — running standalone"),
    { code: "no_host" });


// --------------------------------------------------------------------
// Host detection
// --------------------------------------------------------------------

function detectHost() {
  if (typeof window === "undefined") return null;
  // Fusion 360 Palette exposes `adsk.fusionSendData(action, json)` as the
  // JS→Python channel. The old `window.fusionJavaScriptHandler` check
  // was wrong — that handler is one WE define for Python→JS replies,
  // not something Fusion injects. Detect via the real Fusion-supplied
  // global instead.
  if (typeof window.adsk?.fusionSendData === "function") return "fusion";
  // Rhino / SolidWorks both expose window.chrome.webview via WebView2
  if (window.chrome?.webview) {
    if (window.ARIA_HOST_HINT === "solidworks") return "solidworks";
    return "rhino";
  }
  if (window.webkit?.messageHandlers?.aria) return "rhino";  // Mac WebKit
  // Onshape panels run in an iframe with parent === Onshape
  if (window.parent !== window && window.ARIA_HOST_HINT === "onshape") {
    return "onshape";
  }
  return null;
}


// --------------------------------------------------------------------
// Fusion 360 adapter — uses adsk.core.HTMLEventArgs round-trip
// --------------------------------------------------------------------

// Install a single `window.fusionJavaScriptHandler` that Fusion calls
// whenever Python does `palette.sendInfoToHTML(eventName, jsonString)`.
// We fan replies out to per-request resolvers keyed on `_id`.
const _fusionPending = new Map();

function _ensureFusionReplyHandler() {
  if (typeof window === "undefined") return;
  if (window.fusionJavaScriptHandler &&
      window.fusionJavaScriptHandler.__aria) return;
  window.fusionJavaScriptHandler = {
    __aria: true,
    handle: function (eventName, dataStr) {
      // We only care about ariaReply. Other event names are ignored.
      if (eventName !== "ariaReply") return;
      try {
        const data = typeof dataStr === "string" ? JSON.parse(dataStr) : dataStr;
        const id = data && data._id;
        const pending = id && _fusionPending.get(id);
        if (!pending) return;
        _fusionPending.delete(id);
        if (data.error) pending.reject(
          Object.assign(new Error(data.error), { code: "host" }));
        else pending.resolve(data.result);
      } catch (err) {
        // Swallow — we don't want a parse failure to crash Fusion's event pump
        if (typeof console !== "undefined") {
          console.error("[ARIA bridge] reply parse failed:", err);
        }
      }
    },
  };
}

function _fusionCall(action, payload = {}) {
  _ensureFusionReplyHandler();
  return new Promise((resolve, reject) => {
    const id = `fusion_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    _fusionPending.set(id, { resolve, reject });
    try {
      // Correct Fusion 360 Palette outbound channel.
      window.adsk.fusionSendData(
        action, JSON.stringify({ _id: id, ...payload }));
    } catch (err) {
      _fusionPending.delete(id);
      reject(err);
    }
    setTimeout(() => {
      if (_fusionPending.has(id)) {
        _fusionPending.delete(id);
        reject(Object.assign(new Error("Fusion bridge timeout"),
                              { code: "timeout" }));
      }
    }, 30000);
  });
}


// --------------------------------------------------------------------
// Rhino WebView2 / WebKit adapter — uses postMessage with correlation
// --------------------------------------------------------------------

function _rhinoPostMessage(action, payload) {
  const wv = window.chrome?.webview;
  const wk = window.webkit?.messageHandlers?.aria;
  const msg = { action, ...payload };
  if (wv) wv.postMessage(JSON.stringify(msg));
  else if (wk) wk.postMessage(msg);
}

function _rhinoCall(action, payload = {}) {
  return new Promise((resolve, reject) => {
    const id = `rh_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const handler = (e) => {
      try {
        const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
        if (data._id !== id) return;
        window.removeEventListener("message", handler);
        window.chrome?.webview?.removeEventListener?.("message", handler);
        if (data.error) reject(Object.assign(new Error(data.error), { code: "host" }));
        else resolve(data.result);
      } catch (err) { reject(err); }
    };
    window.addEventListener("message", handler);
    window.chrome?.webview?.addEventListener?.("message", handler);
    _rhinoPostMessage(action, { _id: id, ...payload });
    setTimeout(() => {
      window.removeEventListener("message", handler);
      reject(Object.assign(new Error("Rhino bridge timeout"), { code: "timeout" }));
    }, 30000);
  });
}


// --------------------------------------------------------------------
// Onshape iframe adapter — postMessage to window.parent with _id reply
// correlation. The outer iframe runs bridge-host.js (the host shell)
// which receives messages, calls Onshape REST API, and posts replies
// back as { _id, result } or { _id, error }.
// --------------------------------------------------------------------

function _onshapeCall(action, payload = {}) {
  return new Promise((resolve, reject) => {
    const id = `os_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const handler = (e) => {
      try {
        const data = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
        if (!data || data._id !== id) return;
        window.removeEventListener("message", handler);
        if (data.error) reject(Object.assign(new Error(data.error), { code: "host" }));
        else resolve(data.result);
      } catch (err) { reject(err); }
    };
    window.addEventListener("message", handler);
    try {
      window.parent.postMessage({ action, _id: id, ...payload }, "*");
    } catch (err) {
      window.removeEventListener("message", handler);
      reject(err);
      return;
    }
    setTimeout(() => {
      window.removeEventListener("message", handler);
      reject(Object.assign(new Error("Onshape bridge timeout"), { code: "timeout" }));
    }, 30000);
  });
}


// --------------------------------------------------------------------
// Public bridge surface — dispatches to whichever host was detected
// --------------------------------------------------------------------

const _kind = detectHost();

function _dispatch(action, payload = {}) {
  if (_kind === "fusion") return _fusionCall(action, payload);
  if (_kind === "rhino" || _kind === "solidworks") return _rhinoCall(action, payload);
  if (_kind === "onshape") return _onshapeCall(action, payload);
  return Promise.reject(NO_HOST_ERR());
}

export const bridge = {
  kind: _kind,
  isHosted: _kind !== null,

  getCurrentDocument: () => _dispatch("getCurrentDocument"),
  getSelection:       () => _dispatch("getSelection"),
  insertGeometry:     (url, opts = {}) => _dispatch("insertGeometry", { url, ...opts }),
  updateParameter:    (name, value)    => _dispatch("updateParameter", { name, value }),
  getFeatureTree:     () => _dispatch("getFeatureTree"),
  exportCurrent:      (format = "step") => _dispatch("exportCurrent", { format }),
  showNotification:   (msg, tone = "info") => {
    if (_kind) { _dispatch("showNotification", { msg, tone }).catch(() => {}); }
    else { console.log(`[aria notify/${tone}] ${msg}`); }
  },
  openFile:           (path) => _dispatch("openFile", { path }),

  // Native feature-tree execution — streams a single CAD operation into
  // the host's kernel (Fusion's importManager, Rhino's commands, etc.)
  // so it appears as a real entry in the native browser/timeline.
  // `kind` is one of: beginPlan | newSketch | sketchCircle | sketchRect |
  // extrude | circularPattern | fillet. `params` is kind-specific.
  executeFeature:     (kind, params = {}) =>
                        _dispatch("executeFeature", { kind, params }),

  // Record from the default mic via the host add-in. Returns immediately
  // with { session_id } — call pollRecording(session_id) to check status
  // and get the final audio, or stopRecording() to cut short.
  recordAudio:        (duration_s = 30) =>
                        _dispatch("recordAudio", { duration_s }),
  stopRecording:      () => _dispatch("stopRecording"),
  pollRecording:      (session_id) =>
                        _dispatch("pollRecording", { session_id }),

  // Read CURRENT Fusion user parameters so ARIA stays in sync with
  // user edits in the Parameters dialog. Used before every submit to
  // inject CURRENT state into the prompt context.
  getUserParameters:  () => _dispatch("getUserParameters"),
};

export default bridge;
