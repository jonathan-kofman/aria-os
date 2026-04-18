"""
DEPRECATION SHIM
================
BaseAgent moved to `manufacturing_core.agents.base_agent`. This file:
1. Re-exports BaseAgent + helpers so existing aria_os imports keep working
2. Provides ariaOS-specific defaults: AGENT_MODELS lookup, ariaOS llm_client
   as the cloud fallback, ariaOS context limits

Anywhere in ariaOS that imports `from .base_agent import BaseAgent` keeps
working unchanged. New projects should import from manufacturing_core directly.
"""
from __future__ import annotations

from typing import Any, Callable

from manufacturing_core.agents.base_agent import (
    BaseAgent as _CoreBaseAgent,
    AgentConfig,
    call_ollama,
    is_ollama_available as _core_is_ollama_available,
    _call_ollama,
)

from .ollama_config import OLLAMA_HOST, OLLAMA_TIMEOUT, OLLAMA_WARMUP_TIMEOUT, CONTEXT_LIMITS


# ariaOS-specific config — points the manufacturing-core BaseAgent at
# the same Ollama instance + timeouts ariaOS has always used.
_ARIA_CONFIG = AgentConfig(
    ollama_host=OLLAMA_HOST,
    ollama_timeout=OLLAMA_TIMEOUT,
    warmup_timeout=OLLAMA_WARMUP_TIMEOUT,
    default_model="qwen2.5-coder:7b",
)


def _aria_cloud_fallback(prompt: str, system: str) -> str | None:
    """Cloud LLM fallback specific to ariaOS — uses aria_os.llm_client."""
    try:
        from ..llm_client import call_llm
        return call_llm(prompt, system=system)
    except Exception:
        return None


class BaseAgent(_CoreBaseAgent):
    """ariaOS BaseAgent — manufacturing-core BaseAgent pre-wired with ariaOS defaults."""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: str = "llama3.1:8b",
        tools: dict[str, Callable] | None = None,
        max_context_tokens: int = 4000,
        fallback_to_cloud: bool = False,
    ):
        super().__init__(
            name=name,
            system_prompt=system_prompt,
            model=model,
            tools=tools,
            max_context_tokens=max_context_tokens,
            fallback_to_cloud=fallback_to_cloud,
            config=_ARIA_CONFIG,
            cloud_fallback_fn=_aria_cloud_fallback,
        )


def is_ollama_available(model: str | None = None) -> bool:
    """ariaOS-specific wrapper that uses ariaOS config."""
    return _core_is_ollama_available(
        model=model,
        config=_ARIA_CONFIG,
        cloud_key_env="ANTHROPIC_API_KEY",
    )


__all__ = ["BaseAgent", "is_ollama_available", "_call_ollama", "AgentConfig", "call_ollama"]
