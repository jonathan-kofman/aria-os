# ARIA Panel — Fusion 360 Add-in

Natural-language generative CAD inside Fusion 360. Opens a dockable panel
that loads the ARIA-OS React frontend and bridges it to Fusion via 8
JavaScript↔Python calls.

## Install (dev)

Copy this folder (`aria_panel/`) to Fusion's add-ins directory:

- **Windows:** `%AppData%\Autodesk\Autodesk Fusion 360\API\AddIns\aria_panel\`
- **Mac:** `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/aria_panel/`

Then in Fusion 360 → **Utilities** → **Scripts and Add-Ins** → **Add-Ins**
tab, locate "ARIA Panel" and click **Run**. A dock button labeled
"ARIA Generate" will appear under **SOLID → CREATE**.

## Run the React panel

The add-in loads `http://localhost:5173/?host=fusion` by default. Start
the Vite dev server from the repo root:

```bash
cd aria-os-export/frontend
npm run dev
```

The `?host=fusion` query param tells the React app (via
`src/aria/bridge.js`) to use the Fusion bridge adapter.

## Config

- `ARIA_PANEL_URL` env var overrides the panel source URL. For a deployed
  panel: `ARIA_PANEL_URL=https://aria.yourdomain.com/panel/?host=fusion`
  (set in Fusion's environment before launch, or hardcode in
  `aria_panel.py` for a permanent build).

## Bridge contract (8 calls)

The React app calls these via `window.fusionJavaScriptHandler.handle()`;
Fusion replies by dispatching `ariaFusionReply` custom events. Full
surface lives in `src/aria/bridge.js`:

| JS call | Python handler | Returns |
|---|---|---|
| `bridge.getCurrentDocument()` | `_get_current_document()` | `{name, id, units, type}` |
| `bridge.getSelection()` | `_get_selection()` | `[{id, type, metadata}, …]` |
| `bridge.insertGeometry(url)` | `_insert_geometry(url)` | Downloads + imports STEP/STL into active design |
| `bridge.updateParameter(name, value)` | `_update_parameter()` | Sets a `userParameter.expression` |
| `bridge.getFeatureTree()` | `_get_feature_tree()` | Recursive component/feature tree |
| `bridge.exportCurrent(format)` | `_export_current(fmt)` | STEP/STL file URL |
| `bridge.showNotification(msg, tone)` | `_show_notification()` | Fusion `messageBox` |
| `bridge.openFile(path)` | `_open_file(path)` | Opens a file in Fusion |

## Round-trip demo (the killer flow)

1. User types a prompt in the panel: *"bracket 80×60×40mm, 4× M6 holes on 60mm pitch"*
2. Panel POSTs to `{API_BASE}/api/generate` — ARIA-OS backend generates STEP
3. Backend returns `{step_url: ".../part.step"}`
4. Panel calls `bridge.insertGeometry(step_url)` — Fusion downloads + imports
5. Bracket appears in the active design. User keeps working.

No login, no file download, no context switch.

## Known limitations

- Feature tree is read-only — roundtripping edits through ARIA-OS is
  Plugin.2+ scope.
- No authentication yet; users pass credentials through the panel UI.
- Mac testing pending — WebKit rather than WebView2 for the HTML host;
  may need adjustment to `sendInfoToHTML()` plumbing.

## Plugin.2 roadmap

Once this Fusion flow is demo-ready, copy the same bridge contract to:
- **Rhino** — WebView2 panel via `Rhino.UI.Forms.WebView2Panel`
- **Onshape** — glassbox iframe with postMessage
- **SolidWorks** — WebView2 Task Pane

The JS bridge (`bridge.js`) already has host-detection for all four.
