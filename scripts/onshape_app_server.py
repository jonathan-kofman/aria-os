"""Onshape app server -- the outer iframe shell + REST proxy.

This is the backend for the real "ARIA Generate" Onshape tab. It does
two jobs:

  1. Serves the OUTER iframe HTML at GET /panel. That HTML loads
     bridge-host.js (inline below) and embeds the React panel as an
     INNER iframe with ?host=onshape&did=...&wid=...&eid=... so the
     panel's bridge.js detects the Onshape host and starts dispatching
     ops via window.parent.postMessage.

  2. Serves POST /api/onshape/exec -- a thin proxy that takes a single
     {kind, params, did, wid, eid} payload, runs it through the existing
     OnshapeExecutor (which authenticates with ONSHAPE_ACCESS_KEY /
     ONSHAPE_SECRET_KEY via aria_os.onshape.client), and returns the
     result. Bridge-host.js POSTs here instead of calling cad.onshape.com
     directly so we can skip OAuth entirely for personal dev.

Usage:

    # 1. Set API keys
    $env:ONSHAPE_ACCESS_KEY  = "..."
    $env:ONSHAPE_SECRET_KEY  = "..."

    # 2. Run the server
    python scripts/onshape_app_server.py

    # 3a. Local browser test (outside Onshape):
    #     http://127.0.0.1:8765/panel?did=DID&wid=WID&eid=EID
    #     React panel loads, you can type a prompt, watch ops execute
    #     against your real Onshape Part Studio in another tab.

    # 3b. Real "tab inside Onshape":
    #     - cloudflared tunnel --url http://localhost:8765
    #     - register Custom App at https://dev-portal.onshape.com
    #       point the panel URL at https://<tunnel>/panel
    #     - install app to your Onshape document
    #     - open Part Studio -> "+" tab -> ARIA Generate

The server expects the React frontend dev server at http://localhost:5173
(the standard Vite dev port). Override with ARIA_PANEL_URL env var.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Lazy import (cadquery / trimesh are heavy)
_executor_cls = None
_get_client = None


def _lazy_imports():
    global _executor_cls, _get_client
    if _executor_cls is None:
        from aria_os.onshape.executor import OnshapeExecutor
        from aria_os.onshape.client import get_client
        _executor_cls = OnshapeExecutor
        _get_client = get_client
    return _executor_cls, _get_client


PANEL_URL = os.environ.get("ARIA_PANEL_URL",
                           "http://localhost:5173/?host=onshape")


# -----------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------

app = FastAPI(title="ARIA Onshape App Server")

# Per-(did,wid,eid) executor cache so feature alias registries persist
# across calls within a session. Bridge-host.js sends did/wid/eid on
# every exec call (read from the URL query params), so we key on the
# tuple here.
_executor_cache: dict[tuple[str, str, str], "object"] = {}


class ExecRequest(BaseModel):
    did: str
    wid: str
    eid: str
    kind: str
    params: dict | None = None


@app.post("/api/onshape/exec")
def onshape_exec(req: ExecRequest):
    """Dispatch a single op to OnshapeExecutor and return the result.
    Bridge-host.js POSTs here for every executeFeature call."""
    OnshapeExecutor, get_client = _lazy_imports()
    key = (req.did, req.wid, req.eid)
    executor = _executor_cache.get(key)
    if executor is None:
        try:
            client = get_client(repo_root=REPO_ROOT)
        except Exception as exc:
            raise HTTPException(500,
                f"Onshape client setup failed: {type(exc).__name__}: {exc}. "
                f"Set ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY.")
        executor = OnshapeExecutor(req.did, req.wid, req.eid, client=client)
        _executor_cache[key] = executor
    t0 = time.time()
    try:
        result = executor.execute(req.kind, req.params or {})
        return {"ok": True, "kind": req.kind, "result": result,
                "elapsed_s": round(time.time() - t0, 2)}
    except Exception as exc:
        return {"ok": False, "kind": req.kind,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:],
                "elapsed_s": round(time.time() - t0, 2)}


@app.post("/api/onshape/reset")
def onshape_reset(req: ExecRequest):
    """Wipe every existing feature in the part studio. Used by the
    panel's 'Clear before generate' affordance so each prompt starts
    from a clean slate."""
    _, get_client = _lazy_imports()
    try:
        client = get_client(repo_root=REPO_ROOT)
    except Exception as exc:
        raise HTTPException(500, f"client setup: {exc}")
    from scripts.test_onshape_integration import _reset_studio
    n = _reset_studio(client, req.did, req.wid, req.eid)
    _executor_cache.pop((req.did, req.wid, req.eid), None)
    return {"ok": True, "deleted": n}


# -----------------------------------------------------------------------
# Outer iframe HTML -- bridge-host.js modified to proxy through this
# server's /api/onshape/exec instead of calling cad.onshape.com directly.
# This means no OAuth dance, just the API keys the OnshapeExecutor uses.
# -----------------------------------------------------------------------

OUTER_HTML_TEMPLATE = """\
<!doctype html>
<html><head>
<meta charset="utf-8">
<title>ARIA Generate</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: #1a1a1a;
               font: 13px -apple-system,Segoe UI,sans-serif; color: #eee;
               overflow: hidden; }
  body { display: flex; flex-direction: column; }
  #ctx-banner { flex-shrink: 0; padding: 6px 12px; font-size: 11px; color: #888;
                background: #141414; border-bottom: 1px solid #2a2a2a;
                display: flex; gap: 16px; align-items: center; }
  #ctx-banner.warn { color: #ff7a1a; }
  #ctx-banner button { background: #2a2a2a; color: #eee; border: 1px solid #444;
                       border-radius: 4px; padding: 2px 10px; cursor: pointer; font-size: 11px; }
  #ctx-banner button:hover { background: #333; border-color: #555; }
  #panel-frame { flex: 1 1 auto; width: 100%; border: none; min-height: 0; }
  #setup { padding: 40px; max-width: 480px; margin: 60px auto; background: #222;
           border: 1px solid #333; border-radius: 8px; }
  #setup h2 { margin: 0 0 8px; color: #ff7a1a; font-size: 16px; }
  #setup p { color: #aaa; margin: 0 0 20px; }
  #setup label { display: block; margin: 12px 0 4px; font-size: 11px;
                 text-transform: uppercase; color: #888; letter-spacing: 0.5px; }
  #setup input { width: 100%; box-sizing: border-box; padding: 8px 10px;
                 background: #1a1a1a; color: #fff; border: 1px solid #444;
                 border-radius: 4px; font-family: inherit; font-size: 13px; }
  #setup button { margin-top: 20px; padding: 10px 20px; background: #ff7a1a;
                  color: #000; border: none; border-radius: 4px; font-weight: 600;
                  cursor: pointer; }
</style>
</head><body>

<!-- SETUP screen if DID/WID/EID weren't passed on URL (running outside
     an Onshape tab during local dev). When inside the real Onshape app
     these are populated automatically by Onshape and the setup screen
     is skipped. -->
<div id="setup" style="display:none;">
  <h2>ARIA Generate</h2>
  <p>Open this page from inside an Onshape Part Studio for context to
     auto-populate. For local dev, paste the IDs from your Onshape URL:
     <code>cad.onshape.com/documents/&lt;DID&gt;/w/&lt;WID&gt;/e/&lt;EID&gt;</code></p>
  <label>Document ID</label><input id="setup_did">
  <label>Workspace ID</label><input id="setup_wid">
  <label>Element ID</label><input id="setup_eid">
  <button id="setup_go">Open panel</button>
</div>

<div id="ctx-banner" style="display:none;">
  <span id="ctx-text"></span>
  <button id="ctx-reset">Reset studio</button>
</div>
<iframe id="panel-frame" style="display:none;"></iframe>

<script>
(function() {
  "use strict";

  // --- 1. Resolve DID/WID/EID from URL or setup screen -----------------
  function parseCtx() {
    const p = new URLSearchParams(window.location.search);
    // Reject literal "{$documentId}" tokens that come through when the
    // dev-portal Action URL still has Onshape's old template syntax
    // baked in (Onshape now auto-appends IDs and doesn't substitute).
    function clean(v) {
      if (!v) return null;
      if (v.startsWith("{$") || v.startsWith("{")) return null;
      return v;
    }
    return {
      did: clean(p.get("documentId")) || clean(p.get("did")) || null,
      wid: clean(p.get("workspaceId")) || clean(p.get("wid")) || null,
      eid: clean(p.get("elementId"))  || clean(p.get("eid")) || null,
    };
  }
  let _ctx = parseCtx();

  if (!_ctx.did || !_ctx.wid || !_ctx.eid) {
    // Pre-fill from localStorage so dev iteration is one click.
    document.getElementById("setup").style.display = "block";
    ["did","wid","eid"].forEach(k => {
      const v = localStorage.getItem("aria_os_" + k);
      if (v) document.getElementById("setup_" + k).value = v;
    });
    document.getElementById("setup_go").addEventListener("click", () => {
      const did = document.getElementById("setup_did").value.trim();
      const wid = document.getElementById("setup_wid").value.trim();
      const eid = document.getElementById("setup_eid").value.trim();
      if (!did || !wid || !eid) { alert("All three IDs required"); return; }
      ["did","wid","eid"].forEach(k => localStorage.setItem("aria_os_" + k,
        {did, wid, eid}[k]));
      window.location.search =
        "?did=" + did + "&wid=" + wid + "&eid=" + eid;
    });
    return;
  }

  // --- 2. Mount the React panel as inner iframe ------------------------
  const banner = document.getElementById("ctx-banner");
  const bannerText = document.getElementById("ctx-text");
  banner.style.display = "flex";
  bannerText.textContent =
    "Doc " + _ctx.did.slice(0,8) + "... / WS " + _ctx.wid.slice(0,6) + "... / Elem " + _ctx.eid.slice(0,6) + "...";

  document.getElementById("ctx-reset").addEventListener("click", async () => {
    if (!confirm("Delete every feature in this Part Studio?")) return;
    const r = await fetch("/api/onshape/reset", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(_ctx),
    });
    const d = await r.json();
    alert("Deleted " + (d.deleted || 0) + " features.");
  });

  const frame = document.getElementById("panel-frame");
  frame.style.display = "block";
  // Pass host=onshape so bridge.js detects the iframe context.
  // Pass DID/WID/EID as query so bridge-host (this script) has them
  // when it forwards executeFeature calls.
  frame.src = "__PANEL_URL__" +
    (window.location.search ? "&" + window.location.search.slice(1) : "");

  // --- 3. Bridge-host: receive postMessage from panel, proxy to backend ---
  async function dispatch(action, payload) {
    if (action === "executeFeature") {
      const resp = await fetch("/api/onshape/exec", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          did: _ctx.did, wid: _ctx.wid, eid: _ctx.eid,
          kind: payload.kind, params: payload.params || {},
        }),
      });
      const d = await resp.json();
      if (d.ok) return d.result;
      throw new Error(d.error || "exec failed");
    }
    if (action === "getCurrentDocument") {
      return {
        name: "ARIA Onshape Doc",
        id: _ctx.did,
        units: "mm",
        type: "OnshapeDocument",
        defaultWorkspace: _ctx.wid,
      };
    }
    if (action === "showNotification") {
      console.log("[notify/" + (payload.tone || "info") + "]", payload.msg);
      return { ok: true };
    }
    // Stubs that just no-op so the panel doesn't error. Real
    // implementations require either OAuth tokens or extra REST calls
    // we don't bother with for the personal dev tool.
    if (action === "getSelection")     return [];
    if (action === "getFeatureTree")   return { features: [] };
    if (action === "exportCurrent")    throw new Error("exportCurrent not wired in dev mode");
    if (action === "insertGeometry")   throw new Error("insertGeometry not wired in dev mode");
    if (action === "updateParameter")  return { ok: true };
    if (action === "openFile")         return { opened: false };
    throw new Error("unknown action: " + action);
  }

  window.addEventListener("message", async (event) => {
    if (event.source !== frame.contentWindow) return;  // only from inner panel
    const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
    const { action, _id, ...payload } = data || {};
    if (!action || !_id) return;
    try {
      const result = await dispatch(action, payload);
      frame.contentWindow.postMessage({ _id, result }, "*");
    } catch (err) {
      frame.contentWindow.postMessage(
        { _id, error: err.message || String(err) }, "*");
    }
  });

  console.log("[ARIA outer] mounted; ctx =", _ctx);
})();
</script>
</body></html>
"""


@app.get("/panel", response_class=HTMLResponse)
def panel():
    return OUTER_HTML_TEMPLATE.replace("__PANEL_URL__", PANEL_URL)


@app.get("/healthz")
def healthz():
    return {"ok": True, "panel_url": PANEL_URL}


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

def main():
    port = int(os.environ.get("ARIA_APP_PORT", "8765"))
    url = f"http://127.0.0.1:{port}/panel"
    print(f"ARIA Onshape App Server -> {url}")
    print(f"  Panel iframe URL: {PANEL_URL}")
    print( "  Make sure ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY are set")
    print( "  Open the URL in a browser, paste DID/WID/EID, type a prompt.")
    print()
    print( "To expose for a real Onshape tab:")
    print( "  cloudflared tunnel --url http://localhost:" + str(port))
    print( "  -> https://<random>.trycloudflare.com/panel")
    print( "  Register that URL at https://dev-portal.onshape.com")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
