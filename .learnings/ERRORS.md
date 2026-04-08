# Errors & Fixes

<!-- Format: ## Error Title / Date / Description / Fix / Root Cause -->

## 2026-04-06 — cam_collar set screw wrong axis
cq.Workplane("XZ").center(HEIGHT/2, 0) places at X=H/2, Z=0 (wrong).
For mid-height radial hole: center(0, HEIGHT/2) + extrude(OD/2+2, both=True).
XZ plane: center(x_param, z_param). Extrude goes in Y direction.

## 2026-04-06 — phone_case retention clips create floating solid
Clips placed at inner corner positions are inside the cavity (empty space).
union() of a body with no contact creates a second disconnected solid.
Fix: always filter to largest solid after complex multi-union sequences.
Pattern: `solids = result.solids().vals(); if len(solids) > 1: result = cq.Workplane("XY").newObject([max(solids, key=lambda s: s.Volume())])`

## 2026-04-06 — ECAD validator GPIO conflict false positive
The GPIO conflict check used pad NUMBER as the "net name", so pin "1" on STM32
and pin "1" on L298N both "drove net '1'", triggering 21 false errors.
Fix: use net_map values (actual net names) not pad names in the conflict check.
Also skip power/ground nets in conflict detection.

## 2026-04-06 — ECAD regex \bl298\b misses l298n
\bl298\b requires word boundary after "8" — doesn't match if followed by "n".
Always use l298n? or avoid trailing \b when matching IC part numbers with suffixes.

## 2026-04-07 — KiCad pcbnew Python access violation (0xC0000005) when running headlessly
Running `pcbnew_script.py` with KiCad's bundled Python segfaults when exec()'ing GUI-targeted pcbnew scripts.
Root cause: generated pcbnew scripts call `pcbnew.GetBoard()` (returns active board in GUI context) and use complex
API calls (PCB_TEXT, FOOTPRINT, PAD, add_trace) that crash without a full KiCad environment.
Fix attempts:
  1. Patching `pcbnew.GetBoard = lambda: pcbnew.BOARD()` — still crashed
  2. Adding KiCad bin to PATH env — fixed DLL loading but script still crashed
Final fix: abandon headless pcbnew API entirely; generate .kicad_pcb directly as KiCad 10 S-expression text,
then use kicad-cli to render SVG. pcbnew Python scripts in ARIA-OS are designed for the KiCad GUI scripting
console, not headless execution.

## 2026-04-07 — KiCad Python absolute sys.path causes access violation
Adding absolute path to site-packages via sys.path.insert causes 0xC0000005 crash.
Must use relative path "Lib/site-packages" when running with cwd=KiCad bin dir.
Works:  `sys.path.insert(0, "Lib/site-packages")` with `cwd=kicad_bin_dir`
Fails:  `sys.path.insert(0, r"C:\Users\...\KiCad\10.0\bin\Lib\site-packages")` with same cwd

## 2026-04-07 — KiCad 10 S-expression format changes from KiCad 6/7
Several format fields changed or were removed in KiCad 10:
- version field: must be 20241229 (not 20221018)
- Layer numbers changed: F.Cu=0, B.Cu=2, F.SilkS=5, Edge.Cuts=25, F.CrtYd=31
- gr_rect removed: use 4x gr_line with `(stroke (width 0.05) (type solid))` syntax
- gr_text: must NOT include `(stroke ...)` — only `(effects (font ...))`
- fp_rect removed: use 4x fp_line with `(stroke ...)` syntax
- Required new fields: `(generator_version "10.0")`, `(legacy_teardrops no)`, uuid on footprints and properties

## 2026-04-07 — cairosvg raises OSError on Windows (Cairo DLL missing), not ImportError
`except ImportError: pass` does not catch the Cairo DLL failure — must use `except Exception: pass`.
cairosvg requires `libcairo-2.dll` (GTK/Cairo) which is not installed by default on Windows.
SVG files produced by kicad-cli are the real deliverable; PNG is optional visualization only.

## 2026-04-07 — Path("") == Path(".") — agent mode false positive geometry detection
- `Path("")` evaluates to `Path(".")` in Python, so `Path("").exists()` returns True
- When agent loop stalls with no artifacts, `_agent_state.artifacts.get("step_path", "")` returns `""`
- `Path("").exists()` returns True (current directory), causing false "geometry produced" signal
- Fix: use `.is_file()` instead of `.exists()`, and check `if _raw_step` before constructing Path

## 2026-04-07 — Agent mode no-fallback when LLM unavailable
- When Ollama runs but the available model (gemma4) requires a remote GPU, the agent stalls
- The agent returns with no artifacts but the orchestrator returned a broken session instead of falling back to templates
- Fix: after agent loop, check if step/stl are real files; if not, reset _use_agents=False to fall through to legacy template path
