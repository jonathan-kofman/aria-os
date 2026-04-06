# CLAUDE.md — ARIA-OS Export

## What This Is

ARIA-OS: multi-domain autonomous engineering pipeline.
Natural language goal → physics-constrained CAD → validated STEP/STL → DFM → Quote → CAM.

Local Ollama LLMs handle orchestration and agent loops. Cloud LLMs (Anthropic/Gemini) handle complex code generation.
This is the **canonical generic pipeline repo**. aria-auto-belay imports from here.

## Entry Point

```
python run_aria_os.py "description of part"
```

No argparse — CLI uses manual argv dispatch. No `--help`.

### CLI Commands

| Flag | Usage |
|------|-------|
| (default) | `python run_aria_os.py "goal text"` — full pipeline |
| `--full` | `--full "goal"` — full pipeline (explicit) |
| `--list` | List known parts |
| `--validate` | Validate all outputs |
| `--modify` | `--modify <script.py> "change desc"` — modify existing part |
| `--cam` | `--cam <step_file> [--material X] [--machine Y]` — CAM toolpath |
| `--setup` | `--setup <step> <cam_script> [--material X]` — setup sheet |
| `--cam-validate` | Validate CAM output |
| `--quote` | Generate manufacturing quote |
| `--draw` | Generate engineering drawing |
| `--ecad` | ECAD generation |
| `--autocad` | AutoCAD DXF/DWG generation |
| `--review` | STEP review with LLM |
| `--review-view` | View existing review |
| `--ecad-variants` | Run ECAD variant sweep |
| `--constrain` | Constraint solver |
| `--assemble` / `--assembly` | Assembly generation |
| `--optimize` | Parametric optimization |
| `--cem-full` | Full CEM physics check |
| `--cem-advise` | CEM advisor |
| `--material-study` | Material study for single part |
| `--material-study-all` | Material study across all parts |
| `--lattice` / `--lattice-test` | Lattice generation |
| `--generate-and-assemble` | Generate parts + assemble |
| `--scenario` / `--scenario-dry-run` | Scenario interpreter |
| `--system` / `--system-dry-run` | Full system pipeline |
| `--analyze-part` | Physics analysis (FEA/CFD) |
| `--view` | View STEP file |
| `--image` | Render STL to PNG |

## Pipeline Flow

### Agent Mode (when Ollama available)
`orchestrator.run()` → `run_agent_loop()`:
1. **ResearchAgent** — web search for prior art
2. **SpecAgent** — constraint extraction from goal
3. **DesignerAgent** — CadQuery code generation
4. **EvalAgent** — geometry validation
5. **RefinerAgent** — failure recovery loop
6. Post-processing: DFM, Quote, FEA, Drawing, CAM

### Legacy Mode (no Ollama)
`orchestrator.run()` → keyword planner:
1. `planner.plan()` — parse goal → part_id, params
2. `spec_extractor` — extract dimensions
3. `cem_generator` — resolve physics model
4. `multi_cad_router` — route to CAD tool (CadQuery, Grasshopper, Blender, Fusion, Zoo)
5. Generator → STEP/STL
6. Post-processing: validation, DFM, Quote, CAM

### Coordinator (alternative orchestrator)
`aria_os/agents/coordinator.py` — 5-phase async pipeline:
1. Research (parallel web search)
2. Synthesis (LLM spec)
3. Geometry (CAD / ECAD / Civil)
4. Manufacturing (FEA, DFM, Drawing, CAM, Quote — parallel)
5. Finalize (memory, MillForge bridge)

Detects assemblies → delegates to `AssemblyAgent`.

## Package Structure

```
aria_os/                    # Main package
  orchestrator.py           # Primary entry: run()
  planner.py                # Goal → plan (part_id, params)
  context_loader.py         # Load mechanical constants, CEM context
  llm_client.py             # Ollama / Anthropic / Gemini routing
  multi_cad_router.py       # Pick best CAD tool for the part
  cad_router.py             # CadQuery iteration + routing
  cad_prompt_builder.py     # Build LLM prompts for CAD generation
  spec_extractor.py         # Extract constraints from natural language
  zoo_bridge.py             # Zoo.dev KittyCAD API bridge
  visual_verifier.py        # Screenshot-based geometry verification
  cad_operations_reference.py # CadQuery API reference for LLM context

  generators/               # CAD code generators
    cadquery_generator.py   # 80+ CQ templates + LLM fallback
    grasshopper_generator.py
    blender_generator.py
    fusion_generator.py
    llm_generator.py
    autocad_generator.py

  agents/                   # Autonomous agent system
    base_agent.py           # Agent base class (Ollama-backed)
    coordinator.py          # Multi-phase async coordinator
    design_state.py         # Shared state for agent loop
    refinement_loop.py      # Main agent loop orchestrator
    designer_agent.py       # CadQuery code writer
    eval_agent.py           # Geometry evaluator
    refiner_agent.py        # Failure recovery
    spec_agent.py           # Constraint extractor
    research_agent.py       # Web search agent
    dfm_agent.py / dfm_tools.py
    quote_agent.py / quote_tools.py
    cam_agent.py / cam_tools.py
    assembly_agent.py       # Multi-part assembly decomposition
    onshape_bridge.py       # Onshape API bridge
    reflector_agent.py      # Self-reflection
    chain_summarizer.py     # Summarize agent chains
    memory.py               # Agent memory persistence
    domains.py              # Domain detection (CAD/ECAD/Civil/etc.)
    features.py             # Feature extraction
    ollama_config.py        # Ollama model config

  cem/                      # Computational Engineering Model (physics)
    cem_checks.py           # Static/dynamic physics checks
    cem_context.py          # CEM context loading
    cem_advisor.py          # Physics-based design advice
    cem_generator.py        # CEM-to-geometry bridge

  cam/                      # CAM toolpath generation
    cam_generator.py
    cam_physics.py
    cam_setup.py
    cam_validator.py

  ecad/                     # ECAD (PCB) generation
  autocad/                  # AutoCAD DXF/DWG
  gh_integration/           # Grasshopper/Rhino integration
  lattice/                  # Lattice structure generation

cem/                        # Root-level CEM modules (domain-specific physics)
  cem_registry.py           # Route goal → CEM module
  cem_aria.py               # ARIA auto-belay physics
  cem_lre.py                # Liquid rocket engine physics
  cem_clock.py              # Clock/escapement physics
  cem_civil.py              # Civil/structural physics
  cem_core.py               # Shared CEM base

contracts/                  # JSON schemas for inter-module contracts
dashboard/                  # Streamlit UI (NOT part of terminal pipeline)
tests/                      # pytest suite (158 tests)
outputs/                    # Generated artifacts (STEP, STL, CAM, etc.)
```

## Testing

```
python -m pytest tests/ -q
```

158 tests, all passing. No test timeout plugin — some e2e tests take ~2 min.

## Key Dependencies

- **Required**: cadquery, trimesh, numpy, ezdxf, OCP (via cadquery)
- **Optional**: ollama (agent mode), anthropic/google-generativeai (cloud LLM), trimesh (validation), plotly (visualization)
- **NOT in this repo**: `aria_models` (auto-belay physics models) — imported optionally in `cem/cem_checks.py` and `dashboard/`. Physics checks degrade gracefully without it.

## Known Gaps

- `aria_models` not present — CEM static/dynamic checks return None (by design, this is the generic repo)
- `sdf_heat_exchanger.py` referenced in orchestrator but missing — caught by try/except, falls back to CadQuery
- `dashboard/aria_statemachine_tab.py` has runtime `aria_models` dependency — will fail if that tab is invoked
- CLI has no `--help` — unrecognized flags fall through to the default pipeline path
