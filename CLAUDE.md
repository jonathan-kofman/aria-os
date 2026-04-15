# CLAUDE.md — ARIA-OS Export (Architectural Guide)

## What This Is

ARIA-OS: multi-domain autonomous engineering pipeline.
Natural language goal → physics-constrained CAD → validated STEP/STL → DFM → Quote → CAM.

This is the canonical generic pipeline repo. aria-auto-belay imports from here.
Cloud LLMs (Anthropic/Gemini) handle code generation. Ollama handles agent orchestration.

## Entry Point

```
python run_aria_os.py "description of part"
```

No argparse — CLI uses manual argv dispatch. No `--help`. Unrecognized flags fall through to the default pipeline.

### CLI Flags

| Flag | Behavior |
|------|----------|
| (default) or `--full "goal"` | Full pipeline |
| `--list` | List known parts |
| `--validate` | Validate all outputs |
| `--modify <script.py> "change"` | Modify existing part |
| `--cam <step_file>` | CAM toolpath |
| `--draw` / `--quote` / `--review` | Post-processing steps |
| `--image` / `--image-full` | Image-to-CAD (vision analysis → generate) |
| `--assemble` / `--assembly` | Assembly generation |
| `--cem-full` / `--cem-advise` | Physics checks |
| `--scenario` / `--system` | Scenario/system pipeline |
| `--analyze-part` | FEA/CFD |
| `--view` | View STEP file |

---

## Architecture

### Two Operating Modes

**Agent Mode** (when Ollama is available):
`orchestrator.run()` → `run_agent_loop()` in `agents/refinement_loop.py`
1. ResearchAgent — web search for prior art (skipped if goal has ≥3 numeric dims)
2. SpecAgent — constraint extraction from goal (regex first, LLM enrichment if params missing)
3. DesignerAgent — CadQuery code generation (template first, then CADSmith, then LLM)
4. EvalAgent — geometry validation (trimesh + visual_verifier)
5. RefinerAgent — failure recovery; loops back to DesignerAgent
6. Post-processing: DFM, Quote, FEA, Drawing, CAM

**Legacy Mode** (no Ollama, or agent fallback):
`orchestrator.run()` → keyword planner
1. `planner.plan()` — parse goal → part_id, params
2. `spec_extractor.extract_spec()` — extract dimensions from natural language
3. `cem_generator.resolve_and_compute()` — physics model
4. `multi_cad_router` — pick CAD tool (CadQuery, Grasshopper, Blender, Fusion, Zoo)
5. Generator → STEP/STL
6. Post-processing: validation, DFM, Quote, CAM

**Coordinator** (alternative async orchestrator — `aria_os/agents/coordinator.py`):
5-phase async pipeline: Research → Synthesis → Geometry → Manufacturing → Finalize.
Detects assemblies and delegates to AssemblyAgent.

### Fallback Chain

If agent loop produces no real file: `_use_agents = False`, rerun via legacy path.
Check `Path(x).is_file()` not `.exists()` — `Path("").exists()` returns True (cwd).

### Key Files

| File | Role |
|------|------|
| `aria_os/orchestrator.py` | Primary entry `run()` — chooses agent vs legacy mode |
| `aria_os/planner.py` | Goal → plan dict (part_id, params) for legacy mode |
| `aria_os/spec_extractor.py` | Regex-based dimensional extraction; no LLM |
| `aria_os/generators/cadquery_generator.py` | 80+ templates + LLM fallback; core of geometry generation |
| `aria_os/visual_verifier.py` | Render 3 views → vision LLM → structured pass/fail |
| `aria_os/agents/refinement_loop.py` | Orchestrates SpecAgent→Designer→Eval→Refiner loop |
| `aria_os/agents/designer_agent.py` | Tries template, then CADSmith, then LLM; code precheck |
| `aria_os/agents/spec_agent.py` | Regex spec + LLM enrichment for missing template params |
| `aria_os/agents/eval_agent.py` | Trimesh geometry checks + visual verification |
| `aria_os/agents/refiner_agent.py` | Parses failures → refinement_instructions for next iter |
| `aria_os/multi_cad_router.py` | Picks best CAD tool for part type |
| `aria_os/llm_client.py` | Ollama / Anthropic / Gemini routing |

---

## Template System (CRITICAL)

### How Routing Works

`DesignerAgent.generate()` calls `_find_template_fuzzy(part_id, goal, spec)` on iteration 1.

**5-step lookup in priority order:**

1. **Goal keyword scan** — scan full goal text against `_KEYWORD_TO_TEMPLATE` (first match wins). Goal text is ground truth; always checked before LLM-assigned part_type.
2. **Exact map lookup** — `_CQ_TEMPLATE_MAP.get(part_id)`. Keys are exact string part_ids.
3. **Keyword scan of part_id** — check the part_id slug against `_KEYWORD_TO_TEMPLATE`.
4. **Keyword scan of spec["part_type"]** — try `_CQ_TEMPLATE_MAP` and `_KEYWORD_TO_TEMPLATE` on the LLM-extracted part_type.
5. **Word-overlap fuzzy score** — tokenize goal, score against all keyword lists, pick best if score ≥ 1.

Returns `(template_fn | None, match_type)` where match_type is "exact"/"keyword"/"goal"/"fuzzy"/None.

### Three Lookup Tables

**`_CQ_TEMPLATE_MAP`** — exact string key → template function. Used for known part_id slugs like `"flange"`, `"impeller"`, `"aria_ratchet_ring"`, `"nema17"`. The definitive routing for pipeline-generated part_ids.

**`_CQ_TEMPLATE_MAP`** entry format:
```python
"part_type_slug": _cq_function_name,
```

**`_KEYWORD_TO_TEMPLATE`** — ordered list of `(keyword_list, template_fn)`. Keyword lists checked with `any(kw in text for kw in keywords)`. Order matters: multi-word phrases must come before single-word entries to prevent premature matches.
```python
(["flanged coupling", "pipe coupling", "flanged pipe"], _cq_flange),
(["coupling", "coupler"],                               _cq_shaft_coupling),
```

**`_CQ_PART_MAP`** — note: this name appears in older docs but the actual dict is `_CQ_TEMPLATE_MAP`. There is no separate `_CQ_PART_MAP`.

### Adding a New Template

1. Write `_cq_mypart(params: dict) -> str`. Function must:
   - Read all params from `params` dict with defaults
   - Accept param name aliases: `params.get("od_mm", params.get("outer_dia_mm", 100.0))`
   - Return Python string of CadQuery code
   - End with `result = ...` and `bb = result.val().BoundingBox()`
   - Print `f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}"`
   - Never use `.cylinder()` — use `.circle(r).extrude(h)` instead

2. Register in `_CQ_TEMPLATE_MAP`:
   ```python
   "mypart": _cq_mypart,
   "my_part_alias": _cq_mypart,
   ```

3. Register in `_KEYWORD_TO_TEMPLATE` (insert at correct priority position):
   ```python
   (["mypart", "my part", "my_part_alias"], _cq_mypart),
   ```

### Template Parameter Conventions

- Always use `params.get("key", default)` — never crash on missing param
- Accept multiple aliases: `params.get("od_mm", params.get("outer_dia_mm", params.get("outer_diameter_mm", 100.0)))`
- Clamp derived values: `max(min_val, min(max_val, computed))`
- Pre-compute geometry constants at template evaluation time (not in the generated string)
- Use f-strings with pre-computed Python values — the generated string runs in a fresh Python scope

### Template Return Contract

```python
def _cq_mypart(params: dict) -> str:
    od = float(params.get("od_mm", 100.0))
    # ... pre-compute constants ...
    return f"""
import cadquery as cq

OD = {od}

result = cq.Workplane("XY").circle(OD / 2).extrude(10)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""
```

---

## Spec Extraction

`spec_extractor.extract_spec(description)` returns a dict. All keys are optional.

### Dimensional Parameters

| Key | Pattern | Notes |
|-----|---------|-------|
| `od_mm` | "213mm OD", "OD=213mm", "outer diameter 213mm" | Outer diameter |
| `bore_mm` / `id_mm` | "120mm bore", "bore=120", "ID 50mm" | Same value stored in both keys |
| `thickness_mm` / `height_mm` | "21mm thick", "21mm tall", "height=21mm" | Same value stored in both keys |
| `width_mm` | "50mm wide", "width=50mm" | |
| `depth_mm` | "40mm deep", "depth=40mm" | |
| `length_mm` | "200mm long", "length=200mm" | |
| `diameter_mm` | "50mm diameter" | Only set if `od_mm` not found |
| `wall_mm` | "3mm wall", "wall thickness=3mm" | |
| `module_mm` | "1.5mm module", "module=1.5" | Gear module |

**Box notation**: "80x60x40mm" → `width_mm=80, height_mm=60, depth_mm=40` (overrides single-value extractions).

### Count Parameters

| Key | Pattern |
|-----|---------|
| `n_teeth` | "24 teeth", "24-tooth" |
| `n_bolts` | "4 bolts", "4xM8", "4 holes" |
| `n_fins` | "8 fins", "8 parallel fins" |
| `n_blades` | "6 blades", "6-bladed", "6 backward-curved blades" |
| `n_spokes` | "5 spokes", "5-arm", "5 arms" |

### Qualitative Parameters

| Key | Values |
|-----|--------|
| `blade_sweep` | "backward", "backward_curved", "backward_swept", "forward", "forward_curved", "forward_swept", "radial" |
| `material` | "aluminium", "aluminium_6061", "steel", "stainless_steel", "titanium", "nylon", "pla", "petg", "carbon_fibre" |
| `part_type` | Keyword-matched string (see `_PART_TYPE_KEYWORDS` — longest match wins) |

### Angle Parameters

| Key | Pattern |
|-----|---------|
| `blade_angle_deg` | "sweep=30deg", "30° sweep", "backward-curved 30°" |
| `angle_deg` | "angled 45deg", "45° angle", "at 45 degrees" |

### Bolt Pattern Parameters

| Key | Pattern | Notes |
|-----|---------|-------|
| `bolt_circle_r_mm` | "PCD=100mm" (stored as 50), "bolt circle 100mm" (stored as 50), "bolts at 25mm" (stored as 25) | Stored as radius (PCD/2), except "bolts at Nmm" which is already a radius |
| `bolt_dia_mm` | "M8 bolt" → 8.0, "4xM8" | |
| `bolt_square_mm` | "160mm square" → stores raw side length, `bolt_circle_r_mm` auto-derived | |

### SpecAgent LLM Enrichment

SpecAgent runs regex extraction first. If detected part_type has missing required params, it calls the Ollama LLM to fill gaps. Required params per part_type:

```python
_TEMPLATE_REQUIRED = {
    "bracket":      {"width_mm", "height_mm", "thickness_mm"},
    "flange":       {"od_mm", "bore_mm", "thickness_mm", "n_bolts"},
    "gear":         {"od_mm", "n_teeth", "height_mm"},
    "impeller":     {"od_mm", "bore_mm", "height_mm", "n_blades"},
    "heat_sink":    {"width_mm", "height_mm", "n_fins"},
    "shaft":        {"diameter_mm", "length_mm"},
    ...
}
```

LLM values only fill gaps — they never override regex-extracted or user-provided values.

---

## Visual Verification (CRITICAL)

### Pipeline

`verify_visual(step_path, stl_path, goal, spec)` in `visual_verifier.py`:

1. `_render_views()` → render 3 orthographic views → returns `(paths, view_labels)`
2. `_geometry_precheck()` → deterministic bbox + watertight check vs spec dims (±15%)
3. `_build_checklist()` → generate feature check list from goal keywords + spec counts
4. `_call_vision()` → send images + prompt to vision API → get structured JSON result
5. Combine: precheck failures veto vision PASS regardless of confidence

### Provider Chain

Priority order — first available is primary:

| Provider | Model | Confidence Cap | Notes |
|----------|-------|---------------|-------|
| Gemini | gemini-2.5-flash (then 2.0-flash) | 0.95 | Free daily quota; first to exhaust; session-level `_gemini_quota_exhausted` flag |
| Groq | llama-4-scout-17b-16e-instruct (then llama-3.2-11b) | 0.92 | Free tier, ~1-3s, separate quota |
| Ollama | gemma4:e4b (then llava variants) | 0.85 | Local, no quota; reliable for failures, not for PASS confirmation |
| Anthropic | claude-sonnet-4-6 | 1.0 (no cap) | Paid, authoritative; no cross-validation needed |

### Cross-Validation

If primary (non-Anthropic) provider reports PASS at confidence ≥ 0.90:
- The next provider in the chain is called as second opinion
- Both must agree on PASS — if second disagrees, result becomes FAIL
- Final confidence = `min(primary_conf, cross_conf)`
- Anthropic is authoritative and does NOT trigger cross-validation

### Geometry Precheck

`_geometry_precheck(stl_path, spec)` — deterministic, no LLM:
- Loads STL with trimesh, gets `mesh.bounding_box.extents` (x, y, z sizes)
- Checks: `od_mm` vs `max(extents[0], extents[1])`, `width_mm`, `length_mm`, `height_mm/thickness_mm`
- Watertight check: `mesh.is_watertight`
- Tolerance: ±15% on all linear dimensions
- Precheck failures reduce final confidence and force `overall_match = False`

### View Rendering

`_render_views(stl_path, goal, out_dir)` → `(paths, view_labels)`:

1. Try GL renderer (trimesh scene.save_image) — proper depth sorting, 3 views + optional cross-section
2. Always falls back to matplotlib wireframe on headless Windows (no OpenGL)

**Wireframe path (actual behavior on Windows)**:
- Uses `mesh.edges_unique` (E×2) → `mesh.vertices[edges_unique]` (E×2×3)
- Projects each edge's 2 endpoints → stacks as (E×2×2) segments
- `LineCollection(segs, linewidths=0.4, colors='steelblue', alpha=0.5)`
- `ax.autoscale()` called after `ax.add_collection(lc)` — REQUIRED
- Subsamples to 20,000 edges for dense meshes
- Convex hull overlay for orientation
- View labels: "Top projection (XY plane — wireframe, looking straight down from above)", "Front projection (XZ plane — wireframe, looking from front)", "Side projection (YZ plane — wireframe, looking from right side)"

View labels are passed 1:1 into the vision prompt — the model is explicitly told "only reference the images listed above, do NOT mention isometric or any other views not in this list."

---

## Code Pre-check (LLM-generated code only)

After LLM generates CadQuery code (NOT templates), `_precheck_code_spec(code, spec)` in `designer_agent.py` scans for:

**Count mismatches**: collects `range(N)` and `N = <int>` from code. If spec `n_blades=6` but no literal `6` in code and closest range is `range(4)`, reports mismatch. Uses `re.search(rf'\b{expected}\b', code)` to also find literals inside expressions.

**Dimension mismatches**: for `od_mm`, `bore_mm`, `height_mm` — checks that a numeric literal within ±15% of the spec value exists in the generated code. Values < 5mm are skipped (too many false positives).

**Sweep direction mismatches**: if spec says "backward" but code contains "forward" (and not "backward"), or vice versa.

If issues found, DesignerAgent regenerates once with an explicit correction prompt. If corrected code has fewer issues, it is used. False positives are harmless — they only add context to the prompt.

**Important**: a dimension inside `params.get("od_mm", 150)` has `150` as a literal — the precheck will find it and pass. This is correct behavior.

---

## Common Part Families

### Impeller / Fan (`_cq_impeller`)

Params: `od_mm`, `bore_mm`, `height_mm`, `n_blades` (fallback: `n_fins`), `blade_thickness_mm`, `blade_sweep`, `blade_angle_deg`.

Open-face design (no shroud): base disc (20% of height) + blades standing above it (80% exposed).

Key formulas:
- `hub_od = bore_mm * 2`
- `bt_use = max(user_bt, od * 0.10)` — minimum 10% OD or blades invisible in wireframe renders
- `sweep > 0` = backward (tip trails), `sweep < 0` = forward (tip leads), `sweep = 0` = radial
- `blade_sweep = "backward"` → `sweep = +abs(blade_angle_deg)` (default 30°)
- `blade_sweep = "forward"` → `sweep = -abs(blade_angle_deg)`
- n_blades: `params.get("n_blades", params.get("n_fins", 6))`

### L-bracket (`_cq_l_bracket`)

Params: `width_mm`, `height_mm`, `depth_mm`, `thickness_mm`, `n_bolts`, `bolt_dia_mm`.

**Critical gotcha**: `thickness_mm > 20` means it's a bbox dimension, not wall thickness.
- If `depth_mm` present: use it as horizontal leg depth
- If `thickness_mm > 20`: treat as bbox dim, use for horizontal leg depth
- Wall auto-derived: `max(4, min(10, min(h, leg_h) * 0.12))` mm
- Holes split between base plate and vertical leg

vs `_cq_bracket`: flat plate with holes, no L-shape. Use for simple mounting plates.
vs `_cq_flat_plate`: no holes by default, for covers/panels.

### Flange (`_cq_flange`)

Params: `od_mm`, `bore_mm`, `thickness_mm`, `n_bolts`, `bolt_circle_r_mm`, `bolt_dia_mm`.

Bolt holes on PCD. Visual checklist uses "circular/PCD pattern" only for flanges/discs/wheels — not for brackets (which get "linear/grid pattern").

### Gear (`_cq_gear` vs `_cq_involute_gear`)

`_cq_gear`: simple disc with approximated tooth profile. Fast, renders well.
`_cq_involute_gear`: proper involute tooth geometry. Use when tooth accuracy matters.

Params: `od_mm`, `n_teeth`, `height_mm`, `module_mm`.

### Housing (`_cq_housing`)

Two modes depending on params:
- If `od_mm` present: cylindrical body with bore and bolt holes
- If no `od_mm`: rectangular enclosure with shell(), mounting bosses, lid screw holes

Params (cylindrical): `od_mm`, `bore_mm`, `height_mm`, `n_bolts`, `bolt_circle_r_mm`, `bolt_dia_mm`.
Params (rectangular): `width_mm`, `height_mm`, `depth_mm`, `wall_mm`.

### Nozzle / LRE Nozzle (`_cq_nozzle`)

Bell/de Laval profile computed at template time using Rao's approximate method.
Params: entry radius (`entry_r_mm` or `chamber_dia_mm`), `throat_r_mm`, `exit_r_mm`, `conv_length_mm`, `length_mm`, `wall_mm`.
Produces spline-revolved hollow nozzle with optional bolt flange.

### Heat Sink (`_cq_heat_sink`)

Base plate + parallel fins.
Params: `width_mm`, `depth_mm`, `base_thickness_mm`, `fin_height_mm`, `fin_thickness_mm`, `n_fins`, `fin_spacing_mm`.

### Spoked Wheel (`_cq_spoked_wheel`)

Params: `od_mm`, `bore_mm`, `n_spokes`, `thickness_mm`.

### Shaft (`_cq_shaft`)

Simple cylinder. Params: `diameter_mm`, `length_mm`.

### Snap Hook (`_cq_snap_hook`)

Cantilever snap-fit hook. Params: `length_mm`, `width_mm`, `thickness_mm`, `hook_height_mm`, `hook_depth_mm`.

### Spool (`_cq_spool`)

Drum + two flanges + hub bore + helical groove approximation.
Params: `od_mm` (drum), `width_mm`, `flange_od_mm`, `hub_od_mm`, `n_grooves`.
Note: uses non-standard param aliases: `diameter`/`od_mm`, `width`/`drum_width_mm`, etc.

---

## Known Gotchas (MUST READ)

### 0. Run Isolation (`outputs/runs/<run_id>/`)

Every pipeline run creates a timestamped directory: `outputs/runs/20260409T215033_a3f1c9b2/`.

- `run_id` = `datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]`
- `run_manifest.json` written by `aria_os/run_manifest.py:create_run()`
- Artifacts (`part.step`, `part.stl`) copied from legacy paths into the run dir
- MillForge bundle endpoint (`POST /api/aria/bundle`) accepts the `run_id` for tracking
- **Concurrent runs are safe** — each has a unique run directory

Legacy `outputs/cad/step/agent.step` still written, now using `session_id` (`DesignState.session_id`) in the filename to avoid collision between concurrent instances.

### 1. Concurrent Pipeline Runs (RESOLVED)

`DesignState.session_id` is a `uuid.uuid4().hex[:8]` generated at construction time. Output files are named `<part_id or session_id>.step/.stl` — never bare `agent.step`. Multiple instances are safe.

### 2. `groq` Package in Wrong Python Env

Pipeline uses `C:\Users\jonko\miniforge3\python.exe`. `pip install groq` from a Bash tool call goes to a different env. User must run `pip install groq` in their own terminal.

### 3. Unicode Box-Drawing Characters

`──` and similar Unicode separators in Python code strings cause `UnicodeEncodeError: 'charmap' codec can't encode` on Windows cp1252. Use plain ASCII `# ---` instead.

### 4. `thickness_mm` Ambiguity for L-brackets

"80x60x40mm bracket": box notation sets `thickness_mm=40` (last dimension). Template checks: `if raw_t > 20` treat as bbox dimension, not wall thickness. Wall derived at `max(4, min(10, min(h, leg_h) * 0.12))`.

### 5. matplotlib `ax.autoscale()` Required After `add_collection`

After `ax.add_collection(lc)`, axis limits do NOT update automatically. Must call `ax.autoscale()` before `plt.savefig()`.

### 6. Groq Hallucinating View Names

Before view label anchoring: Groq reported "visible in isometric view" when no isometric was rendered. Fixed by passing exact `view_labels` list to the prompt and explicitly saying to not mention views not in the list.

### 7. False Circular Bolt Pattern Check

`_build_checklist()` only asserts "circular/PCD pattern" for goals containing flange/disc/wheel/ring keywords. Bracket and plate goals get "N bolt holes visible (top projection)" without the circular requirement.

### 8. `n_blades` Not Extracted in Old Sessions

Before 2026-04-09, `spec_extractor` only extracted `n_fins` and `n_bolts`, not `n_blades`. Templates must fall back: `params.get("n_blades", params.get("n_fins", 6))`.

### 9. `aria_models` Not Present

CEM static/dynamic checks return None. `dashboard/aria_statemachine_tab.py` will fail if that tab is invoked. This is the generic export repo — by design.

### 10. `sdf_heat_exchanger.py` Missing

Referenced in orchestrator but missing — caught by try/except, falls back to CadQuery.

### 11. `Path("").exists()` Returns True

Always use `.is_file()` not `.exists()` when checking for actual output files.

### 12. CadQuery `.cylinder()` Does Not Exist

Use `.circle(r).extrude(h)` instead. The reference template injection in DesignerAgent explicitly warns LLMs about this.

---

## Visual Checklist Generation

`_build_checklist(goal, spec)` builds the vision feature check list from three sources:

1. **`_FEATURE_KEYWORDS`**: keyword → (description, view_hint) pairs. Scanned against `goal.lower()`. Examples: "bore" → "large center hole visible (top projection)", "impeller" → "curved vane/blade features visible radiating from hub (top projection)".

2. **Regex count patterns**: `re.findall(r"(\d+)\s*[xX×]\s*(hole|fin|blade|...)", goal)` and `r"(\d+)\s+(?:\w+\s+)?(hole|fin|blade|...)"`. Generates "N distinct feature features visible". Skipped if spec has authoritative count.

3. **Spec-driven checks**: `n_teeth`, `n_bolts`, `n_blades`, `bore_mm`, `od_mm`, `n_fins`, `n_spokes`, `blade_sweep`. These are authoritative — override regex guesses.

All view hints use "top projection", "front projection", "side projection" — NOT "isometric view".

---

## Agent Loop

`agents/refinement_loop.py` — `run_agent_loop(state)`:

**Research** (skipped if ≥3 numeric dims already in goal): `ResearchAgent.research(state)`

**SpecAgent** (once): regex extraction → optional LLM enrichment

**Design → Eval → Refine loop** (max `state.max_iterations`, stall after 3 no-improvement iters):

DesignerAgent strategy:
- **Iteration 1, no refinement**: template first → CADSmith → LLM
  - LLM provider order: Anthropic first (quality), then Gemini, then Gemma/Ollama
  - Injects closest template source as reference code
  - Injects CadQuery operations reference for goal-specific patterns
- **Iteration 2+**: Gemini first (faster/cheaper), then Anthropic fallback
- Code precheck runs after LLM generation (not after templates)
- Max 1 regeneration per precheck failure

EvalAgent: trimesh geometry checks (volume, watertight, solid count) + `verify_visual()`

RefinerAgent: parses failure list → `state.refinement_instructions` for next iteration

STALL_LIMIT = 3 iterations with no improvement in failure count.

---

## Package Structure

```
aria_os/
  orchestrator.py           # Entry: run()
  planner.py                # Legacy: goal → plan
  spec_extractor.py         # Regex dimensional extraction
  visual_verifier.py        # Render → vision API → structured result
  llm_client.py             # Ollama / Anthropic / Gemini routing
  multi_cad_router.py       # Pick CAD tool

  generators/
    cadquery_generator.py   # 80+ templates + _find_template_fuzzy + LLM fallback
    llm_generator.py        # Pure LLM code generation

  agents/
    refinement_loop.py      # run_agent_loop()
    designer_agent.py       # generate() + _precheck_code_spec()
    spec_agent.py           # extract() — regex + LLM enrichment
    eval_agent.py           # evaluate() — geometry + visual
    refiner_agent.py        # refine() — parse failures → instructions
    coordinator.py          # Async 5-phase coordinator

  cem/                      # Physics checks
  cam/                      # CAM toolpath
  ecad/                     # PCB generation
  lattice/                  # Lattice structures

cem/                        # Root-level domain physics
tests/                      # pytest suite
outputs/
  cad/step/                 # legacy agent.step (use session_id filename to avoid collision)
  cad/stl/                  # legacy agent.stl (same)
  runs/<run_id>/            # per-run isolation (NEW — see Run Isolation below)
    part.step               # copy of primary STEP artifact
    part.stl                # copy of primary STL artifact
    run_manifest.json       # full metadata (see aria_os/run_manifest.py)
```

---

## Testing

```
python -m pytest tests/ -q        # 158 tests
python run_aria_os.py "goal"      # full pipeline
```

No test timeout plugin. Some e2e tests take ~2 min.

---

## Key Dependencies

- **Required**: cadquery, trimesh, numpy, ezdxf, OCP (via cadquery)
- **Optional (vision)**: google-generativeai (Gemini), groq (Groq), anthropic
- **Optional (agent)**: ollama (local LLM)
- **Optional (render)**: scipy (ConvexHull in wireframe), PIL (image downscale for Ollama)
- **Missing by design**: `aria_models` (auto-belay physics), `sdf_heat_exchanger.py`

---

## ARIA-OS Ship Status (updated 2026-04-15)

**Phase 4 verification + fix loop complete.**

| Area | Status |
|------|--------|
| 17 CadQuery templates | ✅ All ship-ready |
| 10 pipeline modes | ✅ All ship-ready |
| 6 specialized flows | ✅ Ship (3 green, 3 yellow — Railway OOM caveat only) |
| Routing system | ✅ Fixed: "fusion360" rename, sheet metal re-routed to CQ |
| Dashboard `_build_argv` | ✅ 18+ missing command→flag mappings added |
| Subprocess output streaming | ✅ GET /api/run/{id} lines field fixed |
| Import safety on Railway | ✅ aria_os/__init__.py lazy imports prevent crash |

**No P0 ship blockers.** Redeploy Railway main branch to pick up Phase 4 fixes.

### Key fixes applied in this cycle

- `return "fusion"` → `return "fusion360"` everywhere in tool_router.py (orchestrator check was failing)
- Sheet metal goals no longer route to non-existent Fusion 360 path (routes to CQ templates instead)
- `aria_os/__init__.py` is now lazy — import errors in optional deps don't crash the whole package
- gusset: leg_a_mm/leg_b_mm extraction added to spec_extractor; template falls back to width/depth
- weld_bead: workplane fixed YZ→XY (was producing ~484-byte degenerate STL)
- u_channel: dedicated `_cq_u_channel` wrapper with `n_bends=2` default (was producing single-flanged panel)
- height_mm vs thickness_mm extraction bug fixed in spec_extractor (2026-04-15)
