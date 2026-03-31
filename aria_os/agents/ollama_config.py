"""Per-agent Ollama model configuration."""
from __future__ import annotations

import os

# Models per agent role. Override via env vars:
#   ARIA_AGENT_DESIGNER_MODEL=qwen2.5-coder:32b
#   ARIA_AGENT_SPEC_MODEL=llama3.1:8b

# Single model for all agents to avoid VRAM context-switching crashes.
# On 12-16GB VRAM, loading multiple models causes HTTP 500 errors.
# qwen2.5-coder:14b is the best single model for both code gen AND reasoning.
# Override per-agent via env vars if you have 24GB+ VRAM.
_DEFAULT_MODEL = os.environ.get("ARIA_AGENT_MODEL", "qwen2.5-coder:7b")

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

# Context window limits per agent (tokens, estimated as words * 1.3)
CONTEXT_LIMITS: dict[str, int] = {
    "spec":     4000,
    "designer": 8000,
    "eval":     3000,
    "refiner":  4000,
}

# Ollama connection
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))  # 10 min for 14b model
