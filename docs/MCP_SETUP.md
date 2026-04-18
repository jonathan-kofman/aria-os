# MCP-Based CAD Generation — Setup Guide

ARIA-OS supports an alternative pipeline that uses MCP (Model Context Protocol)
to drive a real CAD application instead of generating CadQuery code in one shot.

This is **opt-in** and **additive**. The default pipeline (CadQuery code
generation) is unchanged. Existing flows continue to work without any
configuration.

## Quick switch

```bash
# Use existing CadQuery pipeline (default — no change to behavior)
unset ARIA_GENERATION_MODE

# Use MCP exclusively (fails if MCP unavailable)
export ARIA_GENERATION_MODE=mcp

# Try MCP first, automatically fall back to CadQuery if MCP fails
export ARIA_GENERATION_MODE=auto
```

## Install dependencies

```bash
pip install anthropic   # MCP SDK ships with anthropic package
```

## Configure at least one MCP server

### Option 1: Onshape (recommended — cloud-based, no local install)

```bash
# 1. Get API keys from https://dev-portal.onshape.com/
export ONSHAPE_ACCESS_KEY="..."
export ONSHAPE_SECRET_KEY="..."

# 2. Install the Onshape MCP server (separate process)
git clone https://github.com/hedless/onshape-mcp ~/onshape-mcp
cd ~/onshape-mcp
pip install -e .

# 3. ARIA-OS auto-detects via env vars
```

### Option 2: FreeCAD (local — fully open-source)

Requires FreeCAD installed and a community FreeCAD-MCP server on PATH.
Probe will look for `freecad-mcp` binary. If not found, this server isn't
listed as available.

### Option 3: Rhino (local — paid CAD package)

```bash
# Run a Rhino-MCP server on its default port, then:
export RHINO_MCP_URL="http://localhost:7777"
```

## Verify

```bash
python -c "from core.mcp_pipeline import is_mcp_available, get_available_mcp_servers; \
           print('available:', is_mcp_available()); \
           print('servers:', [s['name'] for s in get_available_mcp_servers()])"
```

## How it differs from the CadQuery pipeline

| | CadQuery (default) | MCP (opt-in) |
|---|---|---|
| **Method** | LLM emits one Python script | LLM has live conversation with CAD tool |
| **Visibility** | Sees code, never the geometry | Sees each operation's result |
| **Recovery** | If code fails, regenerate from scratch | Diagnoses + retries individual operations |
| **Speed** | Fast (single LLM call) | Slower (10-50 tool calls per part) |
| **Accuracy** | Good for simple parts, brittle for complex | Better for parts with many features |
| **Cost** | One LLM call per attempt | Many calls per part (~10-20x cost) |
| **Requires** | Just CadQuery + Python | Real CAD app + MCP server running |

## Compatibility guarantee

The MCP code lives entirely in `core/mcp_pipeline/`. It does NOT modify:

- `aria_os/generators/cadquery_generator.py` (or any of the 80+ templates)
- `aria_os/visual_verifier.py`
- `aria_os/agents/*` (SpecAgent, DesignerAgent, etc.)
- `aria_os/orchestrator.py` (no conditional branch added unless explicitly wired)

To wire MCP into the pipeline, modify orchestrator.py with one new conditional:

```python
from core.mcp_pipeline import run_with_fallback, get_generation_mode

mode = get_generation_mode()  # "cadquery" | "mcp" | "auto"
if mode != "cadquery":
    result = run_with_fallback(
        goal=goal, cadquery_fn=existing_cadquery_call, mode=mode,
    )
    if result["success"]:
        return result
# Fall through to existing CadQuery code
```

This branch is currently NOT wired into orchestrator.py — the MCP module is
infrastructure-only until you decide to enable it. Run the tests in
`tests/test_mcp_pipeline.py` to verify it works in isolation first.

## Rollback

If anything breaks, the rollback is trivial:

```bash
unset ARIA_GENERATION_MODE
```

Or remove the conditional branch you added to orchestrator.py. The MCP
package can stay installed; without the branch + env var it's dormant.
