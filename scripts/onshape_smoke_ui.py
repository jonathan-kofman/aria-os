"""Onshape smoke UI -- local browser panel for ad-hoc plan testing.

Browser front-end + FastAPI backend in one file. Drop-in replacement
for typing `python scripts/test_onshape_integration.py --plan ... --did
... --wid ... --eid ...` over and over. Reuses the existing
OnshapeExecutor + client (which authenticate via API keys, not OAuth)
so no Onshape Custom App registration is needed.

Usage:
    # 1. Make sure ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY are set
    #    (either env or wherever aria_os.onshape.client.get_client()
    #    picks them up).
    # 2. Run:
    python scripts/onshape_smoke_ui.py
    # 3. Browser opens automatically at http://127.0.0.1:8765
    # 4. Paste DID/WID/EID, pick a plan, hit Run.
    #    Watch features land in your Onshape tab next to it.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Lazy-imported on first run; importing aria_os at startup pulls in
# heavy deps (cadquery, trimesh) which the smoke UI doesn't need.
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


PLAN_DIR = REPO_ROOT / "aria_os" / "native_planner" / "fewshots"


def _list_plans() -> list[dict]:
    """Enumerate available few-shot plans with op counts for the UI."""
    out = []
    for p in sorted(PLAN_DIR.glob("*.json")):
        if p.name.startswith("auto_") or p.name == "__init__.py":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            ops = data.get("plan") if isinstance(data, dict) else data
            n_ops = len(ops) if isinstance(ops, list) else 0
            out.append({"id": p.stem, "name": p.stem, "path": str(p),
                         "n_ops": n_ops})
        except Exception:
            out.append({"id": p.stem, "name": p.stem, "path": str(p),
                         "n_ops": 0})
    return out


# -----------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------

app = FastAPI(title="ARIA Onshape Smoke UI")


class RunRequest(BaseModel):
    plan_id: str
    did: str
    wid: str
    eid: str
    reset: bool = False


@app.get("/api/plans")
def get_plans():
    return {"plans": _list_plans()}


@app.post("/api/run")
def run_plan(req: RunRequest):
    plan_path = PLAN_DIR / f"{req.plan_id}.json"
    if not plan_path.is_file():
        raise HTTPException(404, f"Plan {req.plan_id} not found")

    OnshapeExecutor, get_client = _lazy_imports()

    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    if isinstance(plan_data, dict) and "plan" in plan_data:
        ops = plan_data["plan"]
    elif isinstance(plan_data, list):
        ops = plan_data
    else:
        raise HTTPException(400, f"Plan {req.plan_id} malformed")

    try:
        client = get_client(repo_root=REPO_ROOT)
    except Exception as exc:
        raise HTTPException(500,
            f"Onshape client setup failed: {type(exc).__name__}: {exc}. "
            f"Make sure ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY are set.")

    reset_count = 0
    if req.reset:
        from scripts.test_onshape_integration import _reset_studio
        try:
            reset_count = _reset_studio(client, req.did, req.wid, req.eid)
        except Exception as exc:
            return JSONResponse({"ok": False,
                "error": f"Reset failed: {type(exc).__name__}: {exc}"},
                status_code=500)

    executor = OnshapeExecutor(req.did, req.wid, req.eid, client=client)
    results = []
    n_passed = 0
    n_failed = 0
    failed_at = -1
    t_total = time.time()

    for i, op in enumerate(ops, start=1):
        kind = (op.get("kind") if isinstance(op, dict) else None) or "?"
        params = (op.get("params") if isinstance(op, dict) else {}) or {}
        rec = {"seq": i, "kind": kind, "ok": False,
                "error": None, "elapsed_s": 0.0}
        t0 = time.time()
        try:
            executor.execute(kind, params)
            rec["ok"] = True
            n_passed += 1
        except Exception as exc:
            rec["error"] = (f"{type(exc).__name__}: {exc}\n"
                            + traceback.format_exc()[-500:])
            n_failed += 1
            if failed_at < 0:
                failed_at = i
        rec["elapsed_s"] = round(time.time() - t0, 2)
        results.append(rec)
        if not rec["ok"]:
            break  # halt on first failure (matches CLI runner)

    return {
        "ok": n_failed == 0,
        "plan_id": req.plan_id,
        "n_ops_total": len(ops),
        "n_executed": len(results),
        "n_passed": n_passed,
        "n_failed": n_failed,
        "failed_at": failed_at,
        "reset_count": reset_count,
        "elapsed_total_s": round(time.time() - t_total, 2),
        "ops": results,
        "onshape_url":
            f"https://cad.onshape.com/documents/{req.did}/w/{req.wid}/e/{req.eid}",
    }


# -----------------------------------------------------------------------
# Inline HTML (no separate file, no build step)
# -----------------------------------------------------------------------

INDEX_HTML = """\
<!doctype html>
<html><head>
<meta charset="utf-8">
<title>ARIA Onshape Smoke UI</title>
<style>
  body { font: 14px/1.5 -apple-system,Segoe UI,sans-serif;
         background: #1a1a1a; color: #e8e8e8; margin: 0; padding: 24px;
         max-width: 720px; }
  h1 { color: #ff7a1a; font-size: 18px; margin: 0 0 16px; }
  label { display: block; margin: 12px 0 4px; color: #aaa;
          font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  input, select { width: 100%; padding: 8px 10px; box-sizing: border-box;
          background: #2a2a2a; color: #fff; border: 1px solid #444;
          border-radius: 4px; font-family: inherit; font-size: 14px; }
  input:focus, select:focus { outline: none; border-color: #ff7a1a; }
  .row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
  .check input { width: auto; }
  button { margin-top: 20px; padding: 10px 20px; background: #ff7a1a;
           color: #000; border: none; border-radius: 4px; font-weight: 600;
           cursor: pointer; font-size: 14px; }
  button:disabled { background: #555; color: #999; cursor: not-allowed; }
  #log { margin-top: 24px; background: #0e0e0e; padding: 16px;
         border-radius: 4px; font-family: Consolas,Menlo,monospace;
         font-size: 12px; max-height: 400px; overflow-y: auto;
         white-space: pre-wrap; min-height: 60px; }
  .pass { color: #4caf50; }
  .fail { color: #ff5252; }
  .meta { color: #888; }
  a { color: #ff7a1a; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head><body>
<h1>ARIA Onshape Smoke UI</h1>

<label>Document ID (DID)</label>
<input id="did" placeholder="e.g. abc123def456...">
<label>Workspace ID (WID)</label>
<input id="wid" placeholder="e.g. 789abc...">
<label>Element ID (EID, the Part Studio)</label>
<input id="eid" placeholder="e.g. fed321...">

<label>Plan</label>
<select id="plan"></select>

<label class="check">
  <input type="checkbox" id="reset">
  Reset studio first (delete every existing feature)
</label>

<button id="run">Run plan</button>

<div id="log">Pick a plan and hit Run.</div>

<script>
const $ = id => document.getElementById(id);
const log = msg => { $('log').innerHTML += msg + '\\n';
                     $('log').scrollTop = $('log').scrollHeight; };
const clear = () => { $('log').innerHTML = ''; };

// Persist DID/WID/EID across reloads -- saves typing during iteration
['did','wid','eid'].forEach(k => {
  const v = localStorage.getItem('aria_smoke_' + k);
  if (v) $(k).value = v;
  $(k).addEventListener('input', e =>
    localStorage.setItem('aria_smoke_' + k, e.target.value));
});

// Populate plan dropdown
fetch('/api/plans').then(r => r.json()).then(d => {
  const sel = $('plan');
  d.plans.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name + ' (' + p.n_ops + ' ops)';
    sel.appendChild(opt);
  });
});

$('run').addEventListener('click', async () => {
  const did = $('did').value.trim();
  const wid = $('wid').value.trim();
  const eid = $('eid').value.trim();
  if (!did || !wid || !eid) { alert('DID/WID/EID required'); return; }
  clear();
  $('run').disabled = true;
  $('run').textContent = 'Running...';
  log('Running ' + $('plan').value + '...');
  try {
    const resp = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        plan_id: $('plan').value, did, wid, eid,
        reset: $('reset').checked,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json();
      log('<span class="fail">REQUEST FAILED:</span> ' + (err.detail || JSON.stringify(err)));
      return;
    }
    const r = await resp.json();
    if (r.reset_count) log('<span class="meta">Reset removed ' + r.reset_count + ' features.</span>');
    r.ops.forEach(op => {
      const tag = op.ok ? '<span class="pass">PASS</span>'
                        : '<span class="fail">FAIL</span>';
      log('  [' + String(op.seq).padStart(3) + '] ' + tag + ' ' +
          op.kind + ' (' + op.elapsed_s + 's)');
      if (!op.ok && op.error) {
        log('       <span class="fail">' + op.error.split('\\n')[0] + '</span>');
      }
    });
    const tag = r.ok ? '<span class="pass">PASS</span>'
                     : '<span class="fail">FAIL</span>';
    log('');
    log(tag + ' ' + r.n_passed + '/' + r.n_ops_total +
        ' ops in ' + r.elapsed_total_s + 's');
    if (r.failed_at > 0) log('First failure: op #' + r.failed_at);
    log('<a href="' + r.onshape_url + '" target="_blank">Open in Onshape -></a>');
  } catch (e) {
    log('<span class="fail">CLIENT ERROR:</span> ' + e.message);
  } finally {
    $('run').disabled = false;
    $('run').textContent = 'Run plan';
  }
});
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

def main():
    port = 8765
    url = f"http://127.0.0.1:{port}"
    print(f"ARIA Onshape Smoke UI -> {url}")
    print(f"  - {len(_list_plans())} plans available in {PLAN_DIR}")
    print( "  - Make sure ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY are set")
    print( "  - Open Onshape in another tab; copy DID/WID/EID from URL")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
