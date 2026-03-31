# ARIA-OS

**Autonomous engineering pipeline — from natural language to manufactured parts.**

ARIA-OS is a multi-agent AI system that generates CAD geometry, validates it against engineering constraints, estimates manufacturing costs, and produces CNC toolpaths — all from a single natural language description.

## What It Does

```
"skateboard truck baseplate, 80x120mm, 6mm thick, 4x M5 holes, aluminium 7075"
    ↓
[Research]     → Web search for real-world specs and dimensions
[Spec Extract] → 8 structured parameters extracted
[CAD Generate] → CadQuery geometry with 14 faces, M5 bolt holes
[Validate]     → Single solid ✓, watertight ✓, bbox matches ✓
[DFM Analysis] → Score 96/100, CNC 3-axis recommended
[Quote]        → $112.88, 4-6 business days, aluminium 6061
[CAM]          → Fusion 360 toolpath script, 38 min cycle time
[GD&T Drawing] → A3 engineering drawing SVG
```

## Quick Start

```bash
pip install -r requirements_aria_os.txt

# Basic part generation
python run_aria_os.py "simple bracket, 100mm wide, 60mm tall, 8mm thick"

# Full pipeline (CAD + FEA + DFM + Quote + CAM + Drawing)
python run_aria_os.py --full "ARIA cam collar, 80mm OD, 60mm bore, 45mm thick"

# Coordinator mode (parallel research + manufacturing pipeline)
python run_aria_os.py --coordinator "climbing hold bracket, M8 holes, 6061-T6"

# Agent mode with local Ollama models
python run_aria_os.py --agent-mode "iPhone 13 Pro Max protective case"
```

## Multi-Agent Architecture

22 specialized agents orchestrated by a Coordinator:

| Agent | Model | Role |
|-------|-------|------|
| **Coordinator** | — | Decomposes tasks, manages 5-phase pipeline |
| **ResearchAgent** | — | Web search (DDG/Brave/SearXNG/Google) |
| **SpecAgent** | llama3.1:8b | Extracts constraints from natural language |
| **DesignerAgent** | Cloud/Ollama | Generates CadQuery geometry code |
| **EvalAgent** | qwen2.5-coder:7b | Validates geometry, checks solid count |
| **RefinerAgent** | qwen2.5-coder:7b | Code-aware failure analysis + fixes |
| **DFM Agent** | llama3.1:8b | Manufacturability scoring (0-100) |
| **Quote Agent** | llama3.1:8b | Instant cost estimation (CNC/3D print/sheet metal) |
| **CAM Agent** | — | Fusion 360 toolpath generation |

### Pipeline Phases

```
Phase 1 (parallel):  Research materials + standards + similar parts
Phase 2 (serial):    Coordinator synthesizes build recipe from research
Phase 3 (serial):    Generate geometry → Validate → Refine (up to 15 iterations)
Phase 4 (parallel):  CAM toolpaths + FEA simulation
Phase 5 (serial):    DFM + Quote + Final assembly
```

## Ollama Setup (Local AI)

```bash
ollama pull qwen2.5-coder:7b
ollama pull llama3.1:8b

# Optional: better code generation
ollama pull qwen2.5-coder:14b
```

## 85 Materials

Full Prototyping.io coverage plus aerospace/composites/ceramics:

Aluminium (2024, 6061, 6063, 7075, MIC-6) · Steel (1018, 1045, 4130, 4140, 4340, A36) · Stainless (303, 304, 316, 416, 17-4 PH) · Titanium (Grade 2, Grade 5) · Inconel (718, 625) · Copper (C101, C110) · Brass 360 · Bronze 932 · PEEK · Delrin · Nylon · Polycarbonate · TPU · Carbon Fiber · Kevlar · and 50+ more

## Feature Flags

```bash
export ARIA_PROFILE=dev          # dev | demo | production
export ARIA_FEATURE_WEB_SEARCH=1 # Enable/disable individual features
```

## Project Structure

```
aria_os/
├── agents/           # 22-file multi-agent system
│   ├── coordinator.py    # 5-phase parallel pipeline
│   ├── search_chain.py   # Multi-source web search
│   ├── memory.py         # Knowledge consolidation
│   ├── features.py       # Feature flags + build profiles
│   ├── designer_agent.py # CAD code generation
│   ├── eval_agent.py     # Geometry validation
│   ├── dfm_agent.py      # Manufacturability analysis
│   ├── quote_agent.py    # Cost estimation
│   └── cam_agent.py      # CNC toolpath generation
├── generators/       # CadQuery templates (16+ part types)
├── autocad/          # Civil engineering DXF generation
└── physics_analyzer.py  # FEA/CFD (beam, cylinder, plate, drop impact)
cem/                  # Physics models (structural, thermal, fluid)
```

## License

MIT — see [LICENSE](LICENSE)
