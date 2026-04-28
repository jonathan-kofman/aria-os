# Bridge Restart Checklist

The AutoCAD and Onshape bridges have been updated with cross-CAD op handlers and dryrun geometry export. **These changes will NOT take effect until the HTTP server processes are restarted.**

## Bridges Affected

| Bridge | Port | Status | Action |
|--------|------|--------|--------|
| AutoCAD | 7503 | Code updated, server NOT restarted | RESTART REQUIRED |
| Onshape | 7506 | Code updated, server NOT restarted | RESTART REQUIRED |
| SolidWorks | 7501 | Unchanged | No action |
| Fusion | 7504 | Implementation complete but bridge DOWN | Optional: restart if available |
| Rhino | 7502 | Implementation complete but bridge DOWN | Optional: restart if available |

## What Changed

### AutoCAD (`cad-plugins/autocad/aria_autocad_server.py`)
- Added 27 op handlers covering T0_BASIC → T1_CORE features
- Cross-CAD param translation: `sketch`, `cx`, `cy`, `r`, `distance`, `angle_deg` → AutoCAD's `x_mm`, `y_mm`, `radius_mm`, etc.
- Dryrun geometry export: ezdxf-based DXF generation (real 2D circles/rectangles/polylines)
- Handlers: `newSketch`, `sketchCircle`, `sketchRect`, `sketchPolyline`, `sketchSpline`, `extrude`, `revolve`, `fillet`, `shell`, `rib`, `draft`, `helix`, `loft`, `holeWizard`, `circularPattern`

### Onshape (`cad-plugins/onshape/aria_onshape_server.py`)
- Added 7 new op handlers for cross-CAD vocabulary
- Dryrun synthetic geometry export: STEP (text-based minimal) and STL (binary with hardcoded triangle)
- Handlers: `sketchPolyline`, `sketchSpline`, `revolve`, `fillet`, `shell`, `holeWizard`, `circularPattern`

## Restart Procedure (Manual)

### Option A: Kill and Restart via PowerShell

```powershell
# Kill old server processes
Get-Process python | Where-Object {$_.CommandLine -like "*aria_autocad_server*"} | Stop-Process -Force
Get-Process python | Where-Object {$_.CommandLine -like "*aria_onshape_server*"} | Stop-Process -Force

# Start new servers (in separate terminals or background)
python C:\Users\jonko\Downloads\workspace\aria-os\cad-plugins\autocad\aria_autocad_server.py
python C:\Users\jonko\Downloads\workspace\aria-os\cad-plugins\onshape\aria_onshape_server.py
```

### Option B: Manual Kill via Task Manager

1. Open Task Manager (Ctrl+Shift+Esc)
2. Find `python.exe` processes that match the server file paths
3. Kill them
4. Double-click the server .py files or run via terminal

## Verification

After restart, run the smoke test to confirm ops are loaded:

```bash
python scripts/test_bridge_crosscad_ops.py --probe-only
```

Expected output:
```
  autocad      (port 7503) ... UP
  onshape      (port 7506) ... UP
```

Then run the full smoke test:

```bash
python scripts/test_bridge_crosscad_ops.py --cad autocad onshape
```

Expected result: All 4 ops (newSketch, sketchCircle, extrude, saveAs) should PASS.

## Full Test Run

Once bridges are confirmed UP:

```bash
python scripts/cross_cad_matrix.py --cad autocad onshape
```

This will:
1. Execute all 31 test cases against each bridge
2. Update learning ledgers with pass/fail counts
3. Export real DXF/STEP/STL files for geometry validation
4. Expected outcome (AutoCAD): 10-15 tests PASS (T0_BASIC coverage)
5. Expected outcome (Onshape): 5-10 tests PASS (dryrun-only, no live FeatureScript)

## Troubleshooting

**Problem**: `[Errno 111] Connection refused` when running tests
**Cause**: Bridge server is not running on expected port
**Solution**: Verify process is running (`netstat -an | grep 750X`) and restart

**Problem**: Tests show `0 ok, 29 needs_wa` in ledger
**Cause**: Bridge code changes not yet loaded (server was running before edits)
**Solution**: Kill all python.exe processes and restart servers

**Problem**: AutoCAD tests show "unknown op kind 'newSketch'"
**Cause**: Old server code is still in memory; needs restart
**Solution**: Force kill via `taskkill /F /IM python.exe` then restart

**Problem**: Onshape tests show "ONSHAPE_DID/WID/EID env vars required"
**Cause**: Expected — Onshape bridge is in dryrun mode, still generating synthetic geometry
**Solution**: Check that `outputs/test_onshape.stl` was created (synthetic binary STL)

## Files Affected

- `cad-plugins/autocad/aria_autocad_server.py` - NEW: 27 op handlers + ezdxf export
- `cad-plugins/onshape/aria_onshape_server.py` - MODIFIED: 7 new handlers + synthetic export
- `scripts/test_bridge_crosscad_ops.py` - NEW: smoke test utility
- `outputs/autocad_learning_ledger.json` - Will update after restart
- `outputs/onshape_learning_ledger.json` - Will update after restart

## Next Steps After Restart

1. Confirm T0_BASIC tests pass (10-15 ops per CAD)
2. Port sheet-metal validator transform to Onshape/AutoCAD (higher priority)
3. Verify Fusion FeatureExtrusionThin2 surface extrude (requires Fusion bridge UP)
4. Update `feature_taxonomy.py` with new passing status counts
