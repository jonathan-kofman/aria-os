# ARIA Connector — Onshape

Onshape iframe application that bridges the ARIA-OS React panel to Onshape documents.

## Architecture

```
Onshape client
  └── ARIA tab (outer iframe = your hosted page)
        ├── bridge-host.js    <-- this file; talks to Onshape REST API
        └── <iframe> ARIA React panel (?host=onshape)
              └── bridge.js   <-- posts {action, _id, ...payload} to window.parent
```

`bridge.js` detects host = "onshape" via `window.ARIA_HOST_HINT === "onshape"` and routes all
calls through `window.parent.postMessage()`. `bridge-host.js` receives them, calls the Onshape
REST API, and replies with `{_id, result}` or `{_id, error}`.

## Registration

1. Go to https://dev-portal.onshape.com → Applications → Create application.
2. Fill in `manifest.json` placeholders (`clientId`, `clientSecret`, `redirectUri`).
3. Set the tab URL to `https://YOUR_BRIDGE_HOST/panel/?host=onshape`.
4. Publish the application (or leave as dev for testing).

## OAuth Flow

Onshape uses OAuth 2.0 Authorization Code Grant.

1. Onshape calls your redirect URI with `?code=...`.
2. Your backend exchanges the code for a bearer token at `https://oauth.onshape.com/oauth/token`.
3. Store the token server-side (session cookie or encrypted cookie).
4. Inject it into the outer iframe page as `window.ARIA_ACCESS_TOKEN` **or** call
   `window.ariaSetAccessToken(token)` from your page script.
   **Never** pass the token to the inner ARIA iframe.

## insertGeometry — Why It Needs a Server Proxy

Onshape does not allow arbitrary 3D import from a client-side URL fetch due to:

1. CORS — browsers block cross-origin binary fetches from the Onshape iframe context.
2. Onshape's import endpoint requires multipart/form-data with the binary payload.

The correct flow:

```
ARIA panel -> bridge-host.js (stub)
  -> YOUR SERVER (proxy)
       1. fetch(url)  // download STEP/STL bytes
       2. POST /api/blobelements/d/{did}/w/{wid}
          Content-Type: multipart/form-data
          Authorization: Bearer <token>
          file: <binary>
       3. return { blobElementId }
  -> reply to panel
```

Reference API endpoint: `POST https://cad.onshape.com/api/blobelements/d/{did}/w/{wid}`

## Bridge Actions

| Action             | Status     | Notes                                                    |
|--------------------|------------|----------------------------------------------------------|
| getCurrentDocument | Real       | GET /api/documents/d/{did}/w/{wid}                       |
| getSelection       | Stubbed    | Needs Onshape glassbox postMessage API                   |
| insertGeometry     | Stubbed    | Requires server-side proxy (see above)                   |
| updateParameter    | Stubbed    | POST /api/partstudios/d/{did}/w/{wid}/e/{eid}/variables  |
| getFeatureTree     | Stubbed    | GET /api/partstudios/d/{did}/w/{wid}/e/{eid}/features    |
| exportCurrent      | Stubbed    | POST /api/partstudios/d/{did}/w/{wid}/e/{eid}/translations |
| showNotification   | Stubbed    | No native API; implement as DOM toast                    |
| openFile           | Stubbed    | Open a document by ID via Onshape URL                    |

## File Map

| File               | Role                                                            |
|--------------------|-----------------------------------------------------------------|
| `manifest.json`    | Onshape app registration — OAuth, iframe URL, permissions       |
| `bridge-host.js`   | Outer iframe message handler; Onshape REST API calls            |
| `README.md`        | This file                                                       |
