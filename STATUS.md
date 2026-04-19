# ARIA-OS — Status & Pro-Quality Roadmap

_Last updated: 2026-04-19. Pass this doc to a fresh Claude session along with `CLAUDE.md` to get accurate context._

---

## TL;DR

**What ARIA-OS is:** a headless natural-language → multi-domain engineering pipeline. Prompt in, fab-ready CAD + ECAD + drawings + CAM + assembly docs + cost out. Target verticals: drones, brackets/mounts, enclosures, lattice-structural parts, impellers, consumer-grade electronics.

**Closest commercial analog:** Zoo.dev (KCL + Text-to-CAD) + nTop (implicit/lattice) stitched together. Unlike either, ARIA-OS spans MCAD + ECAD + CAM in one pipeline.

**Current pro-quality level (honest):**

| Domain | Level | Gap to pro |
|---|---|---|
| MCAD (B-rep via CadQuery/OCCT) | 75% | Missing: constraint solver (DCM), feature tree, assembly mates. Pad pitch issues in minimal footprints. |
| MCAD (implicit/SDF) | **85%** | Full TPMS family, strut lattices, FGM, FEA-stress-driven density all landed. Need: STEP export via NURBS fit (out of scope for OSS), more lattice primitives. |
| ECAD (KiCad-writer pipeline) | 60% | **DRC-aware but 205 violations/board** — minimal footprints cause 120 mask-bridges + 14 IC shorts. Footprint lib lookup landed but not wired. Schematic writer v1 works (generic pins); v2 w/ real symbols needs fixing. No ERC yet. |
| CAM | 70% | Fusion-script path works but needs Fusion app. FreeCAD Path headless module ships; NC sim via CAMotics ships. Both skip gracefully until installed. |
| FEA | 65% | Static-linear via gmsh+CalculiX wired. Topology-opt loop landed (iterative FGM). Missing: nonlinear, modal, thermal coupling. |
| Drawings | 40% | Orthographic SVG projections via CadQuery today. TechDraw MBD module ships but not wired. Missing real GD&T. |
| Validation | 70% | Vision LLM (cross-validated Gemini+Groq+Ollama+Anthropic, cache+escalation ladder) + geometry precheck. Missing: stress checks, tolerance stacks, metrology comparisons. |
| Testing | 75% | 522 tests collected, 516/522 pass on local run. 23% branch coverage overall. Zero-coverage on build_pipeline.py + coordinator.py (big gap). |
| **Weighted average** | **~70%** | Realistic "matches AdamCAD/Zoo.dev on target verticals" with tier-2 polish missing. |

**Gap to 100% pro quality:** credible estimate is 6–12 months of focused work ~$150–500k or 1–2 engineers. The path is owning the orchestration + templates + fine-tuned LLM, wrapping open-source kernels (OCCT, KiCad, CalculiX, Freerouting, FreeCAD). See `CLAUDE.md` → "ARIA-OS Ship Status" section and `scripts/PRO_HEADLESS_SETUP.md`.

---

## Repo structure (load-bearing modules only)

```
aria-os-export/
├── aria_os/
│   ├── orchestrator.py           # Entry: run() — agent/legacy mode dispatch (0% test cov)
│   ├── build_pipeline.py         # 17-stage preset pipeline (0% test cov)
│   ├── planner.py                # Legacy: goal → plan dict
│   ├── spec_extractor.py         # Regex dim extraction (no LLM)
│   ├── tool_router.py            # Picks CAD kernel: cadquery / sdf / grasshopper / ...
│   │
│   ├── agents/
│   │   ├── coordinator.py        # Alt async 5-phase pipeline (0% cov)
│   │   ├── refinement_loop.py    # SpecAgent → Designer → Eval → Refiner
│   │   ├── designer_agent.py     # Template → CADSmith → LLM fallback
│   │   └── eval_agent.py         # trimesh + visual verify
│   │
│   ├── generators/
│   │   ├── cadquery_generator.py # 80+ parametric CAD templates
│   │   ├── sdf_generator.py      # Legacy SDF — still used; template-first path wired
│   │   ├── blender_generator.py
│   │   └── llm_generator.py
│   │
│   ├── sdf/                      # ★ NEW pro-grade implicit kernel (2026-04-19)
│   │   ├── primitives.py         # ellipsoid, rounded_box, prism, pyramid, rotations...
│   │   ├── lattices.py           # Full TPMS + BCC/FCC/octet/Kagome/honeycomb/stochastic
│   │   ├── operators.py          # displace, morph, engrave_text
│   │   ├── fgm.py                # Functionally graded material fields
│   │   ├── analysis.py           # volume / mass / CoG / printability
│   │   ├── export.py             # OBJ / 3MF / PLY
│   │   └── templates.py          # Deterministic template library — NL → SDF, no LLM
│   │
│   ├── ecad/
│   │   ├── ecad_generator.py     # Parses NL → components + nets (LLM-assisted)
│   │   ├── kicad_pcb_writer.py   # Direct .kicad_pcb emitter (no KiCad install needed)
│   │   ├── kicad_sch_writer.py   # ★ NEW direct .kicad_sch emitter (v1, generic symbols)
│   │   ├── kicad_symbol_lib.py   # ★ NEW index of 22,713 KiCad symbols w/ pin electrical types
│   │   ├── kicad_footprint_lib.py # ★ NEW index of 15,428 KiCad footprints (not wired yet)
│   │   ├── drc_check.py          # ★ NEW `kicad-cli pcb drc` wrapper + `sch erc`
│   │   ├── autoroute.py          # ★ NEW Freerouting JAR wrapper
│   │   ├── diy_fab.py            # 3D-print + CNC + copper-tape PCB fab (novel)
│   │   ├── ecad_validator.py     # Custom validators (edge keepout etc.)
│   │   └── pcb_3d.py             # Populate 3D PCB STEP
│   │
│   ├── cam/
│   │   ├── freecad_cam.py        # ★ NEW headless CAM via freecadcmd
│   │   └── nc_sim.py             # ★ NEW CAMotics G-code collision check
│   ├── cam_generator.py          # Legacy Fusion-script CAM path
│   │
│   ├── fea/                      # ★ NEW static-linear FEA
│   │   └── calculix_stage.py     # gmsh + CalculiX, 12-material library
│   │
│   ├── topo_opt/                 # ★ NEW FEA-driven topology optimization loop
│   │   └── opt_loop.py           # Iterative envelope → graded lattice → FEA → new density
│   │
│   ├── drawings/                 # ★ NEW MBD drawings
│   │   └── mbd_drawings.py       # FreeCAD TechDraw — 3-view PDF/SVG
│   │
│   ├── visual_verifier.py        # Vision-LLM ladder: cache > Ollama-FAIL > cloud-PASS > Anthropic
│   ├── caching.py                # Content-addressed STL/STEP cache
│   ├── llm_client.py             # Anthropic / Gemini / Groq / Ollama fallback chain
│   ├── cost_estimate.py          # Print + CNC + PCB + electronics + fasteners
│   ├── fasteners_bom.py          # Hardware SKU rollup (McMaster/BoltDepot)
│   ├── assembly_instructions.py  # Per-preset MD + PDF guide
│   ├── mass_calc.py              # STEP volume × density → mass_g
│   ├── drone_quad.py             # 5" / 7" FPV drone preset
│   ├── drone_quad_military.py    # 7" ruggedized recon variant
│   ├── lattice/                  # CadQuery lattice template shim
│   ├── cem/                      # Physics checks
│   └── autocad/                  # DXF civil elements
│
├── dashboard/
│   └── dashboard_server.py       # FastAPI — /api/preset/*, /api/cost/*, /api/bundle, /api/drc
│
├── frontend/
│   └── src/
│       ├── App.jsx               # React entry (Vite)
│       ├── tabs/
│       │   ├── GenerateTab.jsx   # QuickBuilds presets + NL Generate panels
│       │   └── FilesTab.jsx      # Output browser w/ type pills + grouping
│       └── aria/                 # STLViewer, theme, primitives
│
├── scripts/
│   ├── PRO_HEADLESS_SETUP.md     # ★ Install KiCad / FreeCAD / CalculiX / Freerouting / Java
│   ├── README_FINETUNING.md      # Qwen2.5-Coder fine-tune plan
│   ├── build_synthetic_dataset.py # Template-sweep → training triples
│   └── augment_goals.py          # Gemini paraphrase augmentation
│
├── tests/                        # 522 collected, 516 pass locally
│   ├── test_diy_fab.py           # 9 tests ✓
│   ├── test_visual_verifier.py   # 37 tests ✓
│   ├── test_cad_router.py        # 50 tests ✓
│   ├── test_topo_opt.py          # 9 tests ✓
│   ├── test_kicad_footprint_lib.py
│   └── ... (17 more)
│
├── CLAUDE.md                     # Architectural guide (READ FIRST)
├── STATUS.md                     # ← this file
└── session-logs/                 # Daily session logs — YYYY-MM-DD.md
```

---

## Pipeline stages (`build_pipeline.run_full_build`)

17 stages, all `graceful-degrade` — missing tool = skip, never abort:

```
structsight → mechanical → ecad → drawings → diy_fab
  → drc → autoroute → fea
  → mass → instructions → fasteners → cost
  → print → cam → nc_sim
  → cam_headless → mbd_drawings
  → sim → circuit_sim → millforge
```

`on_stage(name, status, elapsed_s, **extra)` fires at start/end/fail/skip of each; dashboard shows them as pills.

**Live on every preset build:** structsight, mechanical, ecad, drawings, mass, instructions, fasteners, cost, print, cam, sim, circuit_sim, millforge, diy_fab.

**Skip until OSS tool installed:**
- `drc` ← `kicad-cli` (KiCad 8+ — installed on Jonathan's box, works)
- `autoroute` ← Freerouting JAR + Java 17+
- `fea` ← CalculiX (ccx) + gmsh (gmsh ✓ already pip-installed)
- `nc_sim` ← CAMotics
- `cam_headless` ← FreeCAD Path
- `mbd_drawings` ← FreeCAD TechDraw

Install doc: `scripts/PRO_HEADLESS_SETUP.md`.

---

## What works end-to-end right now

3 drone presets → all 17 stages run (some skip, none fail):

| Preset | Total time | Cost | Mass | Bundle |
|---|---|---|---|---|
| `5inch_fpv` | ~22s | $304 | 503g | STEP + STL + KiCad PCB + gerbers + diy_fab + drawings + CAM + instructions + fasteners |
| `7inch_long_range` | ~25s | $322 | 675g | same |
| `military_recon` | ~26s | $548 | 1,190g | same + vision pod, fiber spool, payload rail, GPS |

Plus:
- **DRC catches 205 real violations** on `fc_pcb.kicad_pcb` (working as intended — tells you which boards are fab-ready)
- **Visual verify** with 4-provider cascade + cache (saves Anthropic $$)
- **NL→SDF templates** resolve octet-truss / gyroid / IWP / honeycomb / FGM blocks without LLM calls

---

## Known gaps (honest, ranked by ROI to close)

### Immediate (next-session candidates)

1. **Wire `kicad_footprint_lib` into `kicad_pcb_writer`** — lookup real footprints for STM32, MPU6000, passives. Drops DRC from 205 → near-zero. 1-2 hours.
2. **Schematic writer v2** — use real symbols from `kicad_symbol_lib` (inheritance embedder broke — see `git log` for v1 revert). Unlocks real ERC. 2-3 hours.
3. **`USB_C_Receptacle` normalizer** — footprint lookup misses this common part. 15 min.

### Medium

4. **Fine-tune Qwen2.5-Coder-7B on synthetic dataset** — `scripts/build_synthetic_dataset.py` + `augment_goals.py` ship. Run the training on a Runpod 4090 ($30, 2hr). Permanent Anthropic replacement.
5. **Real FEA integration in build_pipeline** — CalculiX call exists in `aria_os/fea/calculix_stage.py` but user needs to install `ccx`. Current skip is OK.
6. **Wire topology-opt loop as a preset stage** — `aria_os/topo_opt/opt_loop.py` works standalone; could be a "generative bracket" preset.
7. **Test coverage on `build_pipeline.py`** (0% currently, 386 statements).

### Longer-term (weeks)

8. **Constraint solver (DCM port)** — unlocks editable parametrics (SolveSpace solver wrap).
9. **Feature tree / parametric history** — DAG of ops, re-evaluate on edit.
10. **Schematic capture with hierarchical sheets** — for multi-board designs.
11. **FreeCAD integration verified** — install FreeCAD, run one real CAM headless + TechDraw job end-to-end.

### Architectural

12. **OpenMDAO-style DAG refactor** — stages as Components + Drivers so "no Anthropic credits" swaps Drivers not the pipeline.
13. **Signal integrity + thermal sim** — deep OpenEMS wrap. Low ROI for drone verticals.
14. **Parasolid-grade boolean robustness** — OCCT has known ceilings; not closable without switching kernels.

---

## Honest test suite notes

- **522 collected / 516 pass on last full run.** 4 failures + 2 errors were recently cleaned up (3 main()-as-test files moved to `scripts/driver_*.py`; `test_rhino_compute_e2e.py` moved to scripts; `test_cad_router.py::test_default_is_cadquery` had real regression — fixed to monkeypatch Compute state).
- **Branch coverage: ~23% total.** Much higher on focused modules (kicad_symbol_lib 100%, diy_fab 90%, visual_verifier 12%).
- **Big coverage holes:** `build_pipeline.py` (0%, 386 stmts), `orchestrator.py` (~0%, 2076 stmts), `agents/coordinator.py` (0%, 796 stmts). These are the hot paths.
- **Mutation testing:** config lives at `cosmic-ray-diy-fab.toml`. Run in WSL (mutmut doesn't support Windows natively). Not yet executed.

---

## Cross-project integration (manufacturing-core)

ARIA-OS is 1 of 3 repos sharing `manufacturing-core`:
- `millforge-ai/` — lights-out manufacturing backend (CAM handoff, AS9100, work orders)
- `structsight/` — engineering judgment service

**Known boundary bugs (recently fixed):**
- `tolerance_class` drift: ARIA sent `{tight, ultra, medium, standard}` but shared enum is `{fine, medium, coarse}`. Remapped.
- Silent material fallback: unknown materials used to become `"steel"` silently. Now warns via `logger.warning`.
- `MILLFORGE_BUNDLE_URL` used to be hardcoded; now env-overridable.

**Still open:**
- `coordinator.py:1225-1357` hand-builds MillForge job dict instead of using `ARIAToMillForgeJob` from `millforge_aria_common` — 130 lines of duplicated schema.
- `structsight_context` is `Optional[dict]` in `aria_bridge.py:95` — should be `Optional[StructSightResult]`.

See full cross-project audit in session logs.

---

## Sibling docs

- **`CLAUDE.md`** — architectural guide. Template system, pipeline stages, LLM provider chain, known gotchas. Load first.
- **`scripts/PRO_HEADLESS_SETUP.md`** — OSS tool install commands.
- **`scripts/README_FINETUNING.md`** — dataset + training plan for owned-LLM path.
- **`session-logs/`** — daily work logs; fresh Claude should skim the most recent 2-3 to pick up in-flight threads.
- **`.learnings/ERRORS.md`** / **`.learnings/LEARNINGS.md`** — gotchas + patterns.
