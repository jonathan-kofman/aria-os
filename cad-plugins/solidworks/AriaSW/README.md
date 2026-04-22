# ARIA SolidWorks Add-In

Scaffold for the ARIA SolidWorks integration. Implements the same `executeFeature`
bridge contract used by Fusion/Rhino/Onshape so the backend can target all four
CAD systems with identical op streams.

## SolidWorks-unique features to leverage

1. **Toolbox** — drop in standard ISO/ANSI hardware (M6 bolts, bearings,
   washers) instead of sketching extrude-cut primitives.
2. **Weldments** — steel structure frames use SW's weldment profiles with
   automatic trim/extend behaviour.
3. **Sheet Metal** — class-leading flange/bend/unfold commands.
4. **DimXpert** — automatic dimensioning for Model-Based Definition drawings.
5. **eDrawings export** — share-friendly viewer format for non-CAD users.

## Build

Needs Visual Studio 2022 + SolidWorks SDK. Target `.NET Framework 4.8`.
Key references:

- `SolidWorks.Interop.sldworks`
- `SolidWorks.Interop.swconst`
- `SolidWorks.Interop.swpublished`

The plugin implements `ISwAddin` and exposes ARIA's bridge contract over a
local HTTP loopback — SolidWorks's WebView2 API is less straightforward than
Fusion's Palette, so we dispatch via localhost rather than in-process.

## Status

**NOT YET IMPLEMENTED** — this is a placeholder for when a SolidWorks install
is available to test against. The bridge contract (`executeFeature`,
`recordAudio`, `stopRecording`, `pollRecording`) is stable so a SW plugin
that implements it will drop into the ARIA workflow without further backend
changes.

## Handler names to implement (mirror Fusion + SW-unique)

Shared with Fusion:
- `beginPlan`, `newSketch`, `sketchCircle`, `sketchRect`, `extrude`,
  `circularPattern`, `fillet`
- `asmBegin`, `addComponent`, `joint`
- `beginDrawing`, `newSheet`, `addView`, `addTitleBlock`
- `addParameter`, `createMotionStudy`, `snapshotVersion`

SW-native leverage (not in Fusion):
- `toolboxHardware(type, size, qty)` — drop Toolbox M6, bearings, etc.
- `weldmentProfile(path_alias, profile)` — apply a weldment profile to a 3D sketch
- `dimXpertAuto(part_alias)` — run DimXpert on a drawing
- `exportEdrawings(part_alias, path)` — save `.eprt` for sharing
- `routingWireHarness(start_pin, end_pin)` — SW Routing harness

## Install (once built)

1. Copy `AriaSW.dll` to `%ProgramData%\SolidWorks\Add-Ins\AriaSW\`
2. Register: `regasm /codebase AriaSW.dll`
3. Launch SolidWorks → Tools → Add-Ins → check "ARIA"
