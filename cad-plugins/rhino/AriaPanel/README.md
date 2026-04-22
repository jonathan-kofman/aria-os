# ARIA Panel — Rhino 8 Plugin

Dockable WebView2 panel that bridges the ARIA-OS React frontend to Rhino 8.

## Requirements

- Rhino 8 (Windows) — ships with .NET 7 and the WebView2 runtime.
- Visual Studio 2022 or `dotnet` CLI 7+.
- The ARIA-OS frontend Vite dev server (`npm run dev` in `frontend/`) running on port 5173, OR set `ARIA_PANEL_URL` to your production URL.

## Build

```
cd cad-plugins/rhino/AriaPanel
dotnet build -c Release
```

Output: `bin/Release/net7.0-windows/AriaPanel.dll` (+ `AriaPanel.rhp` after packaging, see below).

## Package as .rhp

Rhino plugins are renamed DLLs. After build:

```
copy bin\Release\net7.0-windows\AriaPanel.dll AriaPanel.rhp
```

Or use `YAK` (Rhino's package manager) to build a proper `.yak` for distribution.

## Install

1. In Rhino 8: `Tools > Options > Plug-ins > Install…`
2. Select `AriaPanel.rhp`.
3. Restart Rhino if prompted.
4. Type `AriaGenerate` in the Rhino command bar, or use `Panels > ARIA Generate`.

## URL Override

By default the panel loads `http://localhost:5173/?host=rhino`.
Override via environment variable before launching Rhino:

```
set ARIA_PANEL_URL=https://aria.example.com/panel/?host=rhino
```

## Bridge Actions

| Action               | Status     | Notes                                              |
|----------------------|------------|----------------------------------------------------|
| getCurrentDocument   | Real       | Doc name, serial number, unit system               |
| getSelection         | Real       | RhinoDoc.Objects.GetSelectedObjects()              |
| insertGeometry       | Real       | Downloads STEP/STL, imports via Rhino.FileIO       |
| showNotification     | Real (lite) | Writes to command line / status bar               |
| updateParameter      | Stubbed    | Returns `{error: "not implemented"}`               |
| getFeatureTree       | Stubbed    | Returns `{error: "not implemented"}`               |
| exportCurrent        | Stubbed    | Returns `{error: "not implemented"}`               |
| openFile             | Stubbed    | Returns `{error: "not implemented"}`               |

## File Map

| File            | Role                                                           |
|-----------------|----------------------------------------------------------------|
| `AriaPlugin.cs` | `PlugIn`-derived entry point; registers panel + command        |
| `AriaPanel.cs`  | WPF `UserControl` hosting WebView2; `AriaGenerateCommand`      |
| `AriaBridge.cs` | Dispatches bridge actions; implements 3 real + 4 stubs         |
| `AriaPanel.csproj` | .NET 7 project; pulls RhinoCommon + WebView2 NuGet refs    |
