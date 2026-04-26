"""Per-agent Ollama model configuration.

Local GPU: RTX 1000 Ada (6GB VRAM). Gemma 4 26B MoE activates only ~3.8B
params per token; with 16–32GB system RAM, Ollama pages experts between
VRAM and RAM and the model runs acceptably on-device. No Lightning AI or
other remote GPU required.

The Gemma tag is auto-selected from host RAM via
llm_client.recommended_gemma_model() — different machines (PC vs laptop)
get different defaults without code changes. GEMMA_MODEL env var still
overrides if set explicitly.

For fully cloud-hosted options, still supported via:
  OLLAMA_HOST=http://<cloud-ip>:11434
"""
from __future__ import annotations

import os

from ..llm_client import recommended_gemma_model

# Models per agent role. Override via env vars:
#   ARIA_AGENT_DESIGNER_MODEL=qwen2.5-coder:32b
#   ARIA_AGENT_SPEC_MODEL=llama3.1:8b
#   ARIA_AGENT_MODEL=gemma4:26b   (use Gemma 4 MoE for all agents)

# Default small coder model — used by agents that don't need Gemma.
_DEFAULT_MODEL = os.environ.get("ARIA_AGENT_MODEL", "qwen2.5-coder:7b")

# Gemma 4 (Apache 2.0) — strong local model for code gen + reasoning.
# Auto-selected from host RAM:
#   >= 32 GB → gemma4:31b (dense, full quality)
#   >= 16 GB → gemma4:26b (MoE, ~3.8B active params per token, RAM-light)
#   >=  8 GB → gemma4:4b  (dense 4B)
#   >=  4 GB → gemma4:1b  (tiny, fits 4GB-class machines)
#    <  4 GB → falls back to qwen2.5-coder:7b (Gemma skipped at runtime)
# Multimodal (text + image), supports configurable thinking mode +
# function calling / structured JSON output. Override with GEMMA_MODEL.
_GEMMA_MODEL = (
    os.environ.get("GEMMA_MODEL")
    or recommended_gemma_model()
    or _DEFAULT_MODEL
)

AGENT_MODELS: dict[str, str] = {
    "spec":     os.environ.get("ARIA_AGENT_SPEC_MODEL",     _DEFAULT_MODEL),
    "designer": os.environ.get("ARIA_AGENT_DESIGNER_MODEL", _DEFAULT_MODEL),
    "eval":     os.environ.get("ARIA_AGENT_EVAL_MODEL",     _DEFAULT_MODEL),
    "refiner":  os.environ.get("ARIA_AGENT_REFINER_MODEL",  _DEFAULT_MODEL),
}

# Per-domain designer model overrides (all use same model to avoid swapping)
DESIGNER_MODELS: dict[str, str] = {
    "cad":      os.environ.get("ARIA_AGENT_DESIGNER_MODEL", _DEFAULT_MODEL),
    "cam":      os.environ.get("ARIA_AGENT_DESIGNER_MODEL", _DEFAULT_MODEL),
    "ecad":     os.environ.get("ARIA_AGENT_DESIGNER_MODEL", _DEFAULT_MODEL),
    "civil":    _DEFAULT_MODEL,
    "drawing":  _DEFAULT_MODEL,
    "assembly": _DEFAULT_MODEL,
    "dfm":      os.environ.get("ARIA_AGENT_DFM_MODEL",      _DEFAULT_MODEL),
}

# Gemma 4 model configurations per agent role.
# When Gemma 4 is available in Ollama, it is preferred over the default
# qwen2.5-coder:7b for most tasks: the MoE design gives a large-model quality
# ceiling at 7B-like inference cost. The designer agent uses Gemma 4 as a
# fallback between cloud LLMs and template generation (see
# designer_agent.py _call_llm).
GEMMA_MODELS: dict[str, str] = {
    "spec":     _GEMMA_MODEL,   # spec extraction, structured output
    "designer": _GEMMA_MODEL,   # CadQuery code generation
    "eval":     _GEMMA_MODEL,   # geometry validation reasoning
    "refiner":  _GEMMA_MODEL,   # code fix suggestions
}

# Context window limits per agent (tokens, estimated as words * 1.3)
# Gemma 4 supports 128k context — designer limit can be raised when using it.
CONTEXT_LIMITS: dict[str, int] = {
    "spec":     4000,
    "designer": 8000,
    "eval":     3000,
    "refiner":  4000,
}

# Ollama connection
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "90"))   # 90s — enough for 7B cold load
OLLAMA_WARMUP_TIMEOUT = int(os.environ.get("OLLAMA_WARMUP_TIMEOUT", "10"))  # pre-flight check
