# ARIA-OS — Honest Status

_Last updated: 2026-04-19. No percentages, no averages, no self-scoring. Every claim backed by a runnable command or marked **UNVERIFIED**._

---

## What this is

A headless natural-language → engineering pipeline for CAD + ECAD + CAM + FEA + drawings. 16,000 lines. In active rebuild toward professional-grade output using the same tooling professionals use (KiCad's own `pcbnew` API, FreeCAD Python API, CalculiX, Freerouting HTTP mode, pythonOCC direct access).

---

## Per-feature honest state

### CAD (mechanical)

| Feature | State | Evidence |
|---|---|---|
| CadQuery templates (80+) | **WORKS** for common families (bracket, flange, impeller, gear, shaft, housing, heat_sink). Tested. | `python run_aria_os.py "100mm flange 4 M6 bolts"` produces valid STEP+STL |
| SDF kernel primitives + lattices | **WORKS** (math verified, unit tests pass) | `tests/test_sdf_*` |
| SDF → STL via marching cubes | **WORKS** but lossy | Octet-truss sphere test: voxel-volume matches mesh-volume within 0.8% |
| Constraint-based sketching | **NOT BUILT** — everything is absolute coords | — |
| Feature tree / editable parametrics | **NOT BUILT** — CadQuery scripts are flat, non-editable | — |
| Assembly mates (concentric/coincident/distance) | **NOT BUILT** — assemblies hard-code positions | — |
| NURBS surfacing (G2, trimmed) | **NOT BUILT** — CadQuery's high-level loft only | — |
| Tolerance stacks (1D/3D) | **NOT BUILT** | — |
| FEA static-linear (CalculiX wrapper code) | **CODED — UNVERIFIED** (CalculiX not installed on dev machine) | `aria_os/fea/calculix_stage.py` exists; never run end-to-end |
| FEA modal (CalculiX wrapper code) | **CODED — UNVERIFIED** (same) | `run_modal_fea` untested against real CCX `.dat` output |
| Topology optimization loop | **CODED — UNVERIFIED** (built by background agent; agent flagged STL→STEP as fragile) | `aria_os/topo_opt/opt_loop.py`; never converged on a real part |
| Mass / volume / CoG (voxel) | **WORKS** | `aria_os.sdf.analysis.compute_mass` |
| Printability checks (thin-wall, overhang) | **WORKS but heuristic** — voxel-based approximations | Correctly flags thin-walled gyroid as unprintable |

### ECAD

| Feature | State | Evidence |
|---|---|---|
| Direct `.kicad_pcb` s-expression writer | **WORKS** (loads in KiCad 10). But generates **DRC-broken PCBs** — 193-318 violations per board. | `outputs/drone_quad/drone_recon_military_7inch/ecad/*/fc_pcb.kicad_pcb` |
| Placeholder-pad footprint generation | **WORKS but produces bad geometry** — mask bridges, wrong clearances. **This is the root of the 193 DRC violations.** | `kicad_pcb_writer._build_footprint_sexpr` |
| Real footprint library lookup | **WORKS** (15,428 footprints indexed). **Breaks more than it fixes** when enabled because placer isn't dense-board-aware. | `ARIA_USE_REAL_FOOTPRINTS=1` → fc_pcb goes from 193 → 318 violations |
| Real symbol library lookup | **WORKS** (22,713 symbols indexed, 5/13 hit rate on drone board). Normalizer imperfect — misses BMP280, QMC5883, AMS1117. | `aria_os.ecad.kicad_symbol_lib.lookup_symbol` |
| Schematic writer v2 (real symbols) | **WORKS** (loads in KiCad 10 after colon-prefix fix) | `aria_os/ecad/kicad_sch_writer.py` |
| Schematic → KiCad ERC | **WORKS** (finds 184 real violations: 137 pin_not_connected, 17 label_dangling, 13 lib_symbol_issues, 12 endpoint_off_grid, 4 footprint_link_issues) | `outputs/_erc_final/erc_report.json` |
| Net map coverage | **INCOMPLETE** — 64-pin STM32 declares ~12 nets; 52 pins unconnected. **This drives all 137 pin_not_connected errors.** | `ecad_generator._assign_component_nets` |
| DRC integration | **WORKS** — reports are parseable, fail reasons clear | 193 violations baseline, worst=error |
| Autoroute (Freerouting) | **CODED — UNVERIFIED** (Java + Freerouting JAR not installed) | `aria_os/ecad/autoroute.py` |
| DIY fab (3D-print substrate + copper tape) | **WORKS** | `tests/test_diy_fab.py` 9/9 pass |
| 3D PCB STEP export | **WORKS** | `aria_os/ecad/pcb_3d.py` |
| Gerber export | **WORKS** via `kicad-cli pcb export gerbers` when pipeline has a real `.kicad_pcb` | — |
| Multi-layer stackup | **NOT BUILT** — single-layer only. This is why routing fails on dense boards. | — |
| Controlled-impedance traces | **NOT BUILT** | — |
| Per-net-class DRC rules (power vs signal) | **NOT BUILT** | — |
| Proper sym-lib-table + fp-lib-table | **NOT BUILT** — we hack around it with `ARIA_` prefix renames | — |

### Drawings

| Feature | State | Evidence |
|---|---|---|
| CadQuery SVG orthographic projections | **WORKS** | `_render_views` |
| FreeCAD TechDraw MBD | **CODED — UNVERIFIED** (FreeCAD not installed on dev machine) | `aria_os/drawings/mbd_drawings.py` never actually run |
| GD&T annotations (datums, feature control frames, tolerance zones) | **NOT BUILT** | — |
| Title block + revision + material | **NOT BUILT** (stub code in TechDraw module; untested) | — |
| Auto-dimensioning from 3D model | **NOT BUILT** | — |
| Section / detail views | **NOT BUILT** | — |
| Multi-sheet PDF export | **NOT BUILT** | — |

### CAM

| Feature | State | Evidence |
|---|---|---|
| Fusion 360 Python script emission | **WORKS but requires Fusion app** — not truly headless | `aria_os/cam_generator.py` |
| FreeCAD Path headless CAM | **CODED — UNVERIFIED** (FreeCAD not installed) | `aria_os/cam/freecad_cam.py` |
| CAMotics NC simulation | **CODED — UNVERIFIED** (CAMotics not installed) | `aria_os/cam/nc_sim.py` |
| CAM physics (spindle RPM, feed, material removal) | **WORKS** | `aria_os.cam_physics` |

### Self-extending agent (hackathon pitch surface)

| Feature | State | Evidence |
|---|---|---|
| Orchestrator + 5 sub-agent scaffolding | **WORKS** (dry-run, no actual LLM calls) | `tests/test_self_extend_*` 14 tests pass |
| Guardrail 1 sandbox (git-worktree isolation) | **WORKS** | 7 tests pass |
| Guardrail 2 contract tests (5 fixtures) | **WORKS on the 5 part types hardcoded** | Correctly rejects stub candidate that fails flange fixture |
| Guardrail 3 physics judge (FEA + DRC dispatch) | **WORKS as code — UNVERIFIED end-to-end** (CalculiX not installed) | `aria_os/self_extend/physics_judge.py` |
| Guardrail 4 trust tier (quarantined → trusted) | **WORKS as JSON state** — but `check_before_use` is **never called** from `build_pipeline`, so quarantined modules are never actually gated | `aria_os/self_extend/trust.py` |
| Live Claude Code sub-agent in Hypothesis stage | **NOT BUILT** — current implementation returns a hardcoded stub bracket. The hackathon pitch promises "novel structure discovery" which this scaffold does not yet do. | `hypothesis.propose_candidates` returns `_STUB_CADQUERY_CANDIDATE` |
| Webhook receiver (GitHub issue → agent) | **NOT BUILT** | — |
| Frontend Agent tab with SSE streaming | **BUILT — UNVERIFIED in production** (committed but never manually loaded on the Vercel deploy) | `frontend/src/tabs/AgentTab.jsx` |

### Testing

| Feature | State | Evidence |
|---|---|---|
| Total tests collected | ~560 | `pytest --collect-only` |
| Tests passing locally | 516 (+/−3 flaky) | Last full run |
| Branch coverage | **23%** overall | `coverage run --branch` |
| `build_pipeline.py` coverage | **0%** (386 statements) | — |
| `orchestrator.py` coverage | **0%** (2076 statements) | — |
| `agents/coordinator.py` coverage | **0%** (796 statements) | — |
| Mutation testing | **NOT RUN** — mutmut doesn't work on Windows, cosmic-ray config committed but never executed | `cosmic-ray-diy-fab.toml` |

---

## What's planned (rebuild using professional tooling)

Per-task tracking in the workspace task list (tasks #72-#108). High-level phases:

- **Phase 0:** honest docs + tool installs
- **Phase 1:** deep research on KiCad pcbnew API, KiCad schematic format, Freerouting HTTP, TechDraw, ASME Y14.5, pythonOCC, SolveSpace
- **Phase 2:** rewrite ECAD on real `pcbnew` Python API + proper sym-lib-table + 4-layer stackup + per-net-class DRC rules + Freerouting HTTP client + net_map growth
- **Phase 3:** rewrite drawings on FreeCAD TechDraw with real GD&T (datums, feature control frames, tolerance zones, title block, BOM)
- **Phase 4:** add pythonOCC advanced BRep operations, SolveSpace constraint solver, feature tree, Assembly4 mates, NURBS surfacing, tolerance stacks
- **Phase 5:** end-to-end verification — submit gerbers to OSHPark, print a part and CMM-check dimensions vs drawing

Estimated timeline: 4-6 weeks of focused work. Not 1 week. Not "mostly done." This is a real rebuild.

---

## What NOT to believe

- Any previous "~70% pro quality" framing. Closer to: CAD templates work, ECAD output is fab-broken, drawings don't exist as real deliverables, half the pipeline stages never run.
- Any agent-written summary claiming completion without evidence attached. If there's no command to run or artifact to inspect, treat it as UNVERIFIED.
- Test counts as proxies for quality. 516 passing tests and 23% branch coverage with 0% on the hottest files is not "well tested."

---

## Commands that actually work today

```bash
# Generate a drone preset end-to-end (bundle includes broken PCBs)
python run_aria_os.py  # via dashboard POST /api/preset/{id}

# Generate a lattice part (zero LLM spend, works cleanly)
python -m aria_os.sdf.templates  # octet/gyroid/iwp/honeycomb templates

# Run the self-extension agent (dry-run, walks the 9 stages)
python -m aria_os.self_extend.orchestrator "bracket 50x30x4mm" --dry-run

# Write a schematic + run ERC (real output, real violations)
python -c "from aria_os.ecad.kicad_sch_writer import write_kicad_sch; write_kicad_sch('path/to/bom.json')"
```

## Commands that should work but haven't been verified end-to-end

```bash
# FEA modal analysis (needs CalculiX installed)
python -c "from aria_os.fea.calculix_stage import run_modal_fea; ..."

# Freerouting autoroute (needs Java + JAR installed)
python -c "from aria_os.ecad.autoroute import run_autoroute; ..."

# FreeCAD TechDraw drawing (needs FreeCAD installed)
python -c "from aria_os.drawings.mbd_drawings import generate_drawing; ..."

# CAMotics G-code collision check (needs CAMotics installed)
python -c "from aria_os.cam.nc_sim import simulate_gcode; ..."
```
