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

## 2026-04-08 — rhino3dm and compute_rhino3d not installed; ImportError on any Rhino Compute call
Both packages are absent from the environment. Any code path that does `import rhino3dm` or
`import compute_rhino3d` will raise ImportError. gh_integration wraps these in try/except so the
pipeline degrades gracefully to artifact-mode (write .py component files) + CadQuery fallback.
Fix: install with `pip install rhino3dm compute-rhino3d` when enabling live Rhino Compute path.
Root cause: packages were never installed — Rhino Compute path was always optional/future work.

## 2026-04-07 — Agent mode no-fallback when LLM unavailable
- When Ollama runs but the available model (gemma4) requires a remote GPU, the agent stalls
- The agent returns with no artifacts but the orchestrator returned a broken session instead of falling back to templates
- Fix: after agent loop, check if step/stl are real files; if not, reset _use_agents=False to fall through to legacy template path

## Ollama gemma4:e4b overconfidence on visual verification (2026-04-09)
- 4B model returns 100% confidence PASS when bolt holes are not actually cut (only 6 faces in geometry)
- Pattern-matches on visual rendering artifacts (dotted circles, reference lines) as "holes"
- Fix: cap Ollama confidence at 0.85 in _call_vision_ollama — local model can catch failures but cannot confirm passes above the 0.90 threshold
- Cloud models (Gemini/Anthropic) are still required to reach the 0.90 pass threshold

## Template routing: wrong part_type overriding goal text (2026-04-09)
- qwen2.5-coder:7b classifies "flanged pipe coupling" as part_type="hex_bolt"
- _find_template_fuzzy was doing exact lookup on part_type BEFORE scanning goal text
- Fix: moved goal keyword scan to Step 1 (before exact lookup) — goal is ground truth, LLM part_type is unreliable
- Also reordered _KEYWORD_TO_TEMPLATE: multi-word phrases (e.g. "flanged coupling") now come before single-word matches ("shaft", "coupling") to prevent premature matches

## groq package installed in wrong Python env (2026-04-09)
- Pipeline uses `C:\Users\jonko\miniforge3\python.exe`
- `pip install groq` from bash tool installs into a different env
- Fix: user must run `pip install groq` in their own terminal session, not via agent bash calls

## Unicode box-drawing chars in CadQuery template strings (2026-04-09)
- `──` chars caused `UnicodeEncodeError: 'charmap' codec can't encode characters` on Windows cp1252
- Fix: replace all `──` and similar Unicode separators with plain ASCII `# ---` or similar

## matplotlib ax.add_collection does not auto-update axis limits (2026-04-09)
- After `ax.add_collection(lc)`, axis limits are not updated automatically
- Must call `ax.autoscale()` or manually set `ax.set_xlim/ylim` after adding the collection
- `ax.autoscale()` works reliably if collection data is set before calling it

## Concurrent pipeline runs fight over agent.step (2026-04-09)
- Two simultaneous pipeline runs both write to the same temp path, causing size oscillation and corrupted output
- Fix: never start multiple pipeline runs simultaneously; the pipeline is not designed for concurrent execution

## Ollama vision 500 errors on cold load (2026-04-09)
- gemma4:e4b returns HTTP 500 on first vision call after being idle (cold VRAM load)
- Subsequent calls work fine once model is warmed up
- Also occurs with large prompts + 3x 800x600 PNG images simultaneously (VRAM saturation on RTX 1000 Ada 6GB)
- Fix: downscale images to 400x300 via PIL before sending to Ollama
- Workaround: Anthropic fallback catches the 500 and continues

## 2026-04-09 — Visual verifier hallucinating isometric view

**Cause**: `_build_vision_prompt()` originally included "Isometric (3D view)" as a view label in the prompt template, even when only 2D wireframe projections (top/front/side) were rendered. Vision models read the label and reported features "visible in the isometric view" that were never in any image.

**Fix**: `_render_views()` now returns the actual `view_labels` list alongside image paths. `_build_vision_prompt()` receives `view_labels` as a parameter and builds the prompt using only those labels — no hardcoded labels. The prompt also contains the explicit instruction: "Only reference the images listed above. Do NOT mention isometric or any other views not in this list."

**Root cause**: prompt was static, decoupled from what the renderer actually produced. Any mismatch between rendered views and prompt-listed views causes hallucinated assessments.

## 2026-04-09 — Groq reporting 100% confidence on visual verify pass

**Cause**: Groq's llama-4-scout returns `confidence: 1.0` (100%) on most PASS responses regardless of actual geometry quality. The model is overconfident — it pattern-matches on rendered lines and labels without doing careful feature counting.

**Fix**: Implemented two mitigations:
1. Cap Groq confidence at 0.92 in `_CONFIDENCE_CAPS` dict — `{"groq": 0.92, ...}`. Raw confidence is replaced with `min(raw_conf, 0.92)` before any threshold checks.
2. Cross-validation: any non-Anthropic provider reporting PASS at ≥0.90 triggers a second provider call. Both must agree on PASS, and `final_conf = min(conf1, cross_conf)` is used. Groq at 0.92 always triggers cross-validation.

**Root cause**: free-tier vision LLMs are calibrated for user-facing confidence display, not for programmatic pass/fail gates. Always apply caps and cross-validate.

## 2026-04-09 — _cq_impeller using hardcoded N=6 blades, ignoring spec

**Cause**: The original `_cq_impeller` template was written before `n_blades` was added to `spec_extractor`. It only read `params.get("n_fins", 6)` — which was also not reliably populated for impellers. Any goal specifying "8 blades" or "12 blades" would silently produce 6-bladed geometry.

**Fix**: Template updated to use `params.get("n_blades", params.get("n_fins", 6))`. The spec extractor was also updated to extract `n_blades` with 5 regex patterns including "6-bladed", "6 backward-curved blades", "with 6 blades".

**Root cause**: `n_blades` param was added to spec_extractor AFTER the impeller template was written. Any template written before 2026-04-09 may have similar gaps — check templates for missing `n_blades` fallback if impeller blade count is wrong.

## 2026-04-09 — Code precheck false positive: dimension inside params.get() default

**Context** (documented for clarity — NOT a bug, correctly passes): `_precheck_code_spec` scans generated code for numeric literals near spec dimensions. A check for `od_mm=200` will match on the literal `200` anywhere in the code, including inside `params.get("od_mm", 200)`.

This is intentional and correct behavior: if the default value in `params.get("od_mm", 200)` matches the spec, the code WILL produce geometry of the correct size (the fallback default is used when the key is absent from params). The precheck correctly passes.

The false positive concern was: if the default is stale (e.g., `params.get("od_mm", 150)` but spec says `od_mm=200`), precheck would find `150` in code and flag a mismatch. This is a true positive — the code would produce 150mm geometry when spec requires 200mm if params dict is empty. Fix in that case: update the default or ensure params is populated before the template runs.

## Keyword routing: "ring" matches "bearing ring" as a standalone word
**Date**: 2026-04-09
**Symptom**: "bearing ring 6205" → `_cq_spacer` instead of `_cq_ball_bearing`
**Root cause**: `_kw_matches("ring", "bearing ring 6205")` — "ring" appears as a standalone word (bounded by space/end), so the word-boundary regex correctly matches it. The word-boundary fix does NOT protect against cases where the ambiguous keyword appears standalone in the goal.
**Fix**: Move bearing entry BEFORE the "ring" entry in `_KEYWORD_TO_TEMPLATE`. Order is critical; more-specific entries must precede their overlapping generic ones.
**File**: `aria_os/generators/cadquery_generator.py`

## Keyword routing: specific subtypes after generic entries never reached
**Date**: 2026-04-09
**Symptom**: "timing pulley gt2" → `_cq_pulley`, "pcb enclosure" → `_cq_housing`, "spur gear" → `_cq_gear`
**Root cause**: Generic entries `["pulley", "sheave"]`, `["housing", "enclosure"]`, `["gear", "sprocket", "cog"]` were placed BEFORE the specialized entries `timing_pulley`, `pcb_enclosure`, `involute_gear`. First match wins, so specialized entries were never reached.
**Fix**: Move all specialized entries BEFORE their generic counterparts. Pattern: "specific before generic" is the invariant.
**File**: `aria_os/generators/cadquery_generator.py`

## Scan-to-CAD: housing classified as "freeform" due to 40% coverage threshold
**Date**: 2026-04-09
**Symptom**: `aria_housing.stl` (110x140x120mm, not watertight) → topology="freeform" even with 8 detected planes
**Root cause**: `_classify_topology()` returned "freeform" if total primitive coverage < 40%. Housing had ~35% coverage (non-watertight mesh, many curved internal features reduce RANSAC hit rate)
**Fix**: Lowered threshold to 25%; added plane-count heuristic (>=5 planes → prismatic regardless of coverage); excluded sphere area from plane/cylinder fractions to prevent sphere false positives from diluting plane_frac
**File**: `aria_os/agents/feature_extraction_agent.py`

## Scan-to-CAD: sphere false positives on prismatic housing
**Date**: 2026-04-09
**Symptom**: 2 spurious spheres (r~54mm) detected in housing scan. Surface points scattered at ~54mm radius from housing center happen to satisfy the RANSAC distance threshold
**Root cause**: `_MIN_INLIER_RATIO = 0.02` (2%) is too lenient for spheres. Flat housing surface has enough scattered points at any given radial distance to accumulate 2% inlier rate
**Fix**: Added `_MIN_INLIER_RATIO_SPHERE = 0.08` (8%) — spheres require 4x higher inlier fraction than planes/cylinders. Real spheres (ball knobs, domes) have dense coverage; false positives fail the 8% threshold
**File**: `aria_os/agents/feature_extraction_agent.py`

## Gear face-width dimension mapped to wrong axis in eval
**Date**: 2026-04-09
**Symptom**: "12mm wide" on a spur gear sets `width_mm=12` in spec. EvalAgent checks `width_mm` against X bounding box (44mm OD) → 267% error, fails every iteration even when geometry is correct. For a gear, "wide" = face width = Z axis, not X.
**Fix needed**: In `spec_extractor.py` or `eval_agent.py`, when `part_type` is `gear`/`involute_gear`, remap `width_mm` to `height_mm` (axial dimension) so the check targets Z, not X.
**File**: `aria_os/agents/eval_agent.py` + `aria_os/spec_extractor.py`

## _cq_involute_gear template falls back to disc on BRep_API polyline error
**Date**: 2026-04-09
**Symptom**: On iter 1-2, template hits "Polyline failed: BRep_API: command not done" and silently falls back to a plain cylinder (9-10KB STEP, no teeth). On LLM iter 3, teeth are generated but disconnected from gear body (non-watertight). Root cause: the involute tooth polygon construction fails at certain tooth counts/modules in CadQuery.
**Fix needed**: Add a try/except around the polyline/polygon tooth loop in `_cq_involute_gear`; fall back to a simpler approximated tooth profile using arcs instead of exact involute polyline.
**File**: `aria_os/generators/cadquery_generator.py` (`_cq_involute_gear` function)

## 2026-04-09 — _cq_involute_gear: self-intersecting tooth polygon causes BRep_API failure

**Root cause**: `_one_tooth()` included root-circle endpoints at ±half_ta. When teeth are rotated by i*P_STEP, the root-right point of tooth i is exactly coincident with root-left of tooth i+1 — OCCT BRep_API cannot build a valid face from a polygon with coincident boundary points. Additionally, the tip arc formula `half_ta + tip_half` has a ~8° angular gap from the actual involute endpoint, producing self-intersecting geometry on small-module gears.

**Fix**: Profile now runs base_r → tip_r only (no root points). Tip arc computed from `atan2(rf[-1])` → `atan2(lf[0])` (actual involute endpoint angles). Blank uses `max(root_r, base_r)` as outer radius. Result: 44×44×12mm, watertight=True, 1144 faces on a 20T m=2 gear.

**File**: `aria_os/generators/cadquery_generator.py` → `_cq_involute_gear()`, lines ~3086-3134

## [2026-04-10] llm_nozzle_bell_small_rocket_engine.stl renders blank
- STL renders as 3265-byte empty PNG at ALL camera angles/distances/colors
- Not fixed — use nozzle_template_test.stl instead
- Scale=717 (bounding sphere), bounds ±250mm in X/Z, ±60mm in Y
