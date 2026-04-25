# Onshape Custom App — local setup guide

This wires up the existing Onshape Custom App scaffold so you get an
"ARIA Generate" tab inside any Onshape Part Studio. Type a prompt,
features appear in the feature tree.

There are TWO modes you'll use:

- **Mode A — local browser** (no cloudflared, no dev portal):
  open the panel in a normal browser tab next to Onshape. 60-second
  setup. Same UX, just not visually inside the Onshape tab.

- **Mode B — real Onshape tab** (cloudflared + dev portal):
  the panel lives inside an Onshape Part Studio tab. ~15 minutes
  one-time setup, then it stays there.

Both modes share the same backend (`scripts/onshape_app_server.py`)
and the same React panel — only how you reach it differs.

---

## Prerequisites

```powershell
# 1. Onshape API keys (from https://dev-portal.onshape.com/keys)
$env:ONSHAPE_ACCESS_KEY  = "..."
$env:ONSHAPE_SECRET_KEY  = "..."

# 2. Frontend dev server
cd frontend
npm run dev          # serves http://localhost:5173

# 3. Backend app server (in another terminal)
cd ..
python scripts/onshape_app_server.py
```

The app server listens on `http://127.0.0.1:8765`. Browser opens
automatically to the setup screen.

---

## Mode A — local browser (start here)

1. App server is running (above).
2. In another browser tab, open Onshape, navigate to a Part Studio.
3. Note the URL: `cad.onshape.com/documents/<DID>/w/<WID>/e/<EID>`.
4. Paste the three IDs into the app server's setup screen, click
   "Open panel".
5. Type a prompt (e.g. "100mm OD flange, 4 bolt holes, 6mm thick"),
   hit submit.
6. Watch the Onshape tab — features land in the feature tree as the
   panel streams native_op events back through the bridge.

The IDs are stored in localStorage so you only paste them once.

**"Reset studio" button** at the top of the panel deletes every
existing feature so each prompt starts from a clean slate.

---

## Mode B — real Onshape tab inside the Part Studio

This adds the "Custom App tab" experience. Required pieces:

### B1. Cloudflared tunnel

```powershell
# In a third terminal:
cloudflared tunnel --url http://localhost:8765
```

You'll get a URL like `https://random-words-1234.trycloudflare.com`.
Copy it. The tunnel rotates per restart, so this URL is dev-only.

### B2. Register Custom App at dev-portal.onshape.com

This is a manual one-time step Onshape requires.

1. Go to https://dev-portal.onshape.com/oauthApps.
2. Click "Create application".
3. Fill in:
   - **Name**: ARIA Generate
   - **Description**: ARIA-OS generative CAD panel
   - **Primary format**: leave default
   - **Redirect URLs**: `https://<your-tunnel>.trycloudflare.com/onshape/callback`
     (we don't actually exchange OAuth — see B4 below — but the
     dev portal requires *some* URL here)
   - **OAuth URL**: `https://<your-tunnel>.trycloudflare.com/panel`
   - **Permissions**: check `OAuth2Read`, `OAuth2Write`. Enough for
     the personal dev tool.
4. Save. Copy the generated **OAuth client ID** and **secret** —
   you don't need them for Mode B but the form requires you to view
   them once.

### B3. Add the "Element tab" extension

Still on dev-portal:

1. Open your application -> "Extensions" tab.
2. Click "Add extension" -> "Element tab".
3. Fill in:
   - **Tab name**: ARIA Generate
   - **Action URL**:
     `https://<your-tunnel>.trycloudflare.com/panel?documentId={$documentId}&workspaceId={$workspaceId}&elementId={$elementId}`
   - **Icon URL**: any 16x16 png (optional)
4. Save.

The `{$documentId}` etc. are Onshape template tokens — Onshape
substitutes the live IDs of the document the user has open before
loading the panel. The app server's outer iframe reads them from
the URL query string.

### B4. Install app to your Onshape account

Same dev-portal page -> "Install application to my account".
Confirm.

### B5. Use it

1. Open any Onshape document, navigate to a Part Studio.
2. Click the "+" tab at the bottom -> "ARIA Generate".
3. The panel loads inside the tab. DID/WID/EID auto-populate from
   the URL Onshape passes in.
4. Type a prompt, hit submit. Features appear in the feature tree
   live, exactly like in Rhino.

---

## How it works (no OAuth needed)

The architecture intentionally skips Onshape's OAuth dance. The
outer iframe (served at `/panel` by the app server) talks to the
INNER React panel via `postMessage` and to the BACKEND via fetch.
The backend uses your `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY`
to sign requests via `aria_os.onshape.client.get_client()` -- the
same code path that powers `scripts/test_onshape_integration.py`.

Onshape's dev portal still requires a Custom App registration with
OAuth fields filled in. They're decoration in this setup -- the real
auth is server-side API key signing. For a multi-user public app
you'd need real OAuth; for personal use this is simpler.

---

## File map

| File | Role |
|------|------|
| `scripts/onshape_app_server.py` | FastAPI backend: `/panel` outer iframe + `/api/onshape/exec` proxy |
| `frontend/src/aria/bridge.js` | Detects `host=onshape`, postMessage dispatch with `_id` reply correlation |
| `cad-plugins/onshape/aria-connector/manifest.json` | Onshape Custom App manifest (template, fill in tunnel URLs) |
| `cad-plugins/onshape/aria-connector/bridge-host.js` | Reference outer-iframe bridge (full OAuth path; superseded by inline code in `onshape_app_server.py` for dev) |

---

## Troubleshooting

- **"No CAD host detected"**: bridge.js didn't see `host=onshape`.
  Check the React panel is loaded in an iframe with that query param.
- **`onshape client setup failed`**: `ONSHAPE_ACCESS_KEY` /
  `ONSHAPE_SECRET_KEY` not set in the env that ran `onshape_app_server.py`.
- **Features don't appear**: open browser DevTools on the outer iframe,
  check `/api/onshape/exec` POST responses for `ok: false`. The
  `error` field tells you which op failed.
- **Tunnel keeps rotating**: that's how `trycloudflare.com` free tier
  works. For a stable URL register a real cloudflared tunnel
  (`cloudflared tunnel create aria`) — but you'll need a domain.
