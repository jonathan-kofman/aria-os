# ARIA SolidWorks Add-In

WebView2-hosted Task Pane that loads the same React frontend used by the
Rhino / Fusion / Onshape panels. Implements the same `executeFeature`
bridge contract so the ARIA backend can drive any of the four CADs with
identical op streams.

## Layout

```
AriaSW/
  AriaSwAddin.cs   ISwAddin entry; ExecuteFeature dispatch + Task Pane registration
  AriaPanel.cs     COM-visible WinForms UserControl hosting WebView2
  AriaBridge.cs    WebView2 message dispatch -> SW API + ExecuteFeature
  AriaSW.csproj    .NET Framework 4.8 build with WebView2 + SW interop refs
```

## How the panel renders

1. SolidWorks loads the add-in at startup (`LoadAtStartup = true`).
2. `ConnectToSW` calls `swApp.CreateTaskpaneView3` and adds the
   `AriaSW.AriaPanelHost` UserControl by ProgID.
3. The UserControl creates a `WebView2`, navigates to
   `http://localhost:5173/?host=solidworks`, and injects
   `window.ARIA_HOST_HINT = 'solidworks'` so `bridge.js` `detectHost()`
   reports SolidWorks (the JS treats it as a Rhino-compatible WebView2
   transport — see `frontend/src/aria/bridge.js`).
4. JS calls `chrome.webview.postMessage({action, _id, ...})`.
5. C# bridge dispatches the action against the SW API and replies via
   `PostWebMessageAsJson({_id, result})`.

## Bridge contract status

| Action | SW API | Status |
|---|---|---|
| `getCurrentDocument` | `IActiveDoc2`, `LengthUnit`, `GetType` | Real |
| `getSelection` | `SelectionMgr.GetSelectedObject6` | Real |
| `insertGeometry` | download STEP/STL -> `OpenDoc6` | Real |
| `showNotification` | `SetStatusBarText` | Real |
| `executeFeature` | delegates to `AriaSwAddin.ExecuteFeature` | Real (op stubs below) |
| `updateParameter` | Equation Manager | Stub |
| `getFeatureTree` | FeatureManager traversal | Stub |
| `exportCurrent` | `ModelDoc.SaveAs3` | Stub |
| `openFile` | `OpenDoc6` | Stub |

`ExecuteFeature` op stubs (sketch / extrude / pattern / fillet / param,
plus SW-unique Toolbox / Weldments / DimXpert / eDrawings) return
`{ok: false, todo: "..."}` and need a SolidWorks install to wire up.

## SolidWorks-unique features to leverage

1. **Toolbox** — drop in standard ISO/ANSI hardware (M6 bolts, bearings,
   washers) instead of sketching extrude-cut primitives.
2. **Weldments** — steel structure frames use SW's weldment profiles
   with automatic trim/extend behaviour.
3. **Sheet Metal** — class-leading flange/bend/unfold commands.
4. **DimXpert** — automatic dimensioning for Model-Based Definition
   drawings.
5. **eDrawings export** — share-friendly viewer format for non-CAD
   users.

## Build

Needs Visual Studio 2022 (or `dotnet` SDK with .NET Framework 4.8
targeting pack) and a SolidWorks install whose `api\redist\CLR2`
folder contains the interop DLLs.

```bash
# From repo root:
dotnet build cad-plugins/solidworks/AriaSW/AriaSW.csproj -c Release
```

If the SolidWorks SDK lives somewhere other than the default path,
override:

```bash
dotnet build cad-plugins/solidworks/AriaSW/AriaSW.csproj -c Release \
  -p:SolidWorksInstallDir="D:\SOLIDWORKS Corp\SOLIDWORKS"
```

The `Release` build runs `regasm /codebase` automatically. For Debug,
run it manually from an elevated shell:

```cmd
regasm /codebase AriaSW.dll
```

## Install

1. Copy `bin\Release\net48\` to a stable location (e.g.
   `%ProgramData%\SolidWorks\Add-Ins\AriaSW\`).
2. From an elevated shell:
   ```cmd
   regasm /codebase AriaSW.dll
   ```
3. Launch SolidWorks -> **Tools** -> **Add-Ins** -> tick "ARIA".
4. Open any part/assembly. Click the ARIA icon on the right-hand Task
   Pane rail to open the panel.

## Run the React panel (dev)

```bash
cd frontend
npm run dev
```

Defaults to `http://localhost:5173/?host=solidworks`. Override the URL
the add-in loads with `ARIA_PANEL_URL` env var (set in the SW launch
environment, or hardcode in `AriaPanel.cs` for a packaged build):

```cmd
set ARIA_PANEL_URL=https://aria.yourdomain.com/panel/?host=solidworks
"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe"
```

## Round-trip demo (the killer flow)

1. User types a prompt in the Task Pane:
   *"bracket 80x60x40mm, 4x M6 holes on 60mm pitch"*
2. Panel POSTs to `{API_BASE}/api/generate` -- ARIA-OS backend
   generates STEP.
3. Backend returns `{step_url: ".../part.step"}`.
4. Panel calls `bridge.insertGeometry(step_url)` -- SW downloads +
   opens the STEP as a Part document.
5. User keeps modeling on top of it.

No login, no manual download, no context switch.

## Status

GUI panel + four real bridge ops are live (document/selection/insert/
notify). `executeFeature` op handlers remain stubs pending a SW install
to verify against. The ProgID-based AddControl flow falls back to a
direct-host route on SW versions that support `AddControlEx` -- if
neither path lights up, the status bar shows `Task Pane control not
registered -- run regasm /codebase`.
