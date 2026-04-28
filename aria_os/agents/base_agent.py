"""
DEPRECATION SHIM (with self-healing fallback)
=============================================
BaseAgent moved to `manufacturing_core.agents.base_agent`. This file:
1. Re-exports BaseAgent + helpers so existing aria_os imports keep working
2. Provides ariaOS-specific defaults: AGENT_MODELS lookup, ariaOS llm_client
   as the cloud fallback, ariaOS context limits
3. Falls back to a minimal in-tree BaseAgent if manufacturing_core isn't
   installed on this machine — keeps SpecAgent / DesignerAgent / etc.
   functional (Ollama + ariaOS llm_client) instead of erroring out the
   whole pipeline at import time.

Anywhere in ariaOS that imports `from .base_agent import BaseAgent` keeps
working unchanged. New projects should import from manufacturing_core directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .ollama_config import (
    OLLAMA_HOST, OLLAMA_TIMEOUT, OLLAMA_WARMUP_TIMEOUT, CONTEXT_LIMITS)


# Try the canonical import first. Anything below the try only runs when
# manufacturing_core is missing on the user's box (the common case for
# laptop installs that haven't installed the shared core lib).
_USING_FALLBACK_BASE = False
try:
    from manufacturing_core.agents.base_agent import (  # type: ignore
        BaseAgent as _CoreBaseAgent,
        AgentConfig,
        call_ollama,
        is_ollama_available as _core_is_ollama_available,
        _call_ollama,
    )
except Exception as _exc:
    _USING_FALLBACK_BASE = True
    print(f"[base_agent] manufacturing_core unavailable ({_exc}); "
          f"using in-tree fallback so the rest of the pipeline still runs",
          flush=True)

    @dataclass
    class AgentConfig:                                  # type: ignore
        ollama_host: str = OLLAMA_HOST
        ollama_timeout: float = OLLAMA_TIMEOUT
        warmup_timeout: float = OLLAMA_WARMUP_TIMEOUT
        default_model: str = "qwen2.5-coder:7b"

    def _call_ollama(prompt: str, model: str, system: str = "",   # type: ignore
                     config: AgentConfig | None = None) -> str:
        """Minimal Ollama client mirroring the manufacturing-core API.
        Returns response text or empty string on failure (never raises)."""
        try:
            import json as _json
            import urllib.request
            cfg = config or AgentConfig()
            body = _json.dumps({
                "model":  model,
                "prompt": prompt,
                "system": system,
                "stream": False,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{cfg.ollama_host.rstrip('/')}/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=cfg.ollama_timeout) as r:
                data = _json.loads(r.read().decode("utf-8"))
                return str(data.get("response", "") or "")
        except Exception:
            return ""

    def call_ollama(*args, **kwargs):                    # type: ignore
        return _call_ollama(*args, **kwargs)

    def _core_is_ollama_available(*, model=None, config=None,
                                    cloud_key_env="ANTHROPIC_API_KEY"
                                    ) -> bool:
        """Check Ollama health, then any cloud key as a tie-breaker so
        the agent loop's fallback chain can still escalate."""
        try:
            import os
            import urllib.request
            cfg = config or AgentConfig()
            with urllib.request.urlopen(
                    f"{cfg.ollama_host.rstrip('/')}/api/tags",
                    timeout=2.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        import os as _os
        return bool(_os.environ.get(cloud_key_env))

    class _CoreBaseAgent:                                 # type: ignore
        """Stub BaseAgent — Ollama-call -> cloud-fallback chain. Compatible
        signature with manufacturing_core's BaseAgent so subclasses don't
        notice the difference."""

        def __init__(
            self,
            name: str,
            system_prompt: str,
            model: str = "llama3.1:8b",
            tools: dict[str, Callable] | None = None,
            max_context_tokens: int = 4000,
            fallback_to_cloud: bool = False,
            config: AgentConfig | None = None,
            cloud_fallback_fn: Callable[[str, str], str | None] | None = None,
        ):
            self.name = name
            self.system_prompt = system_prompt
            self.model = model
            self.tools = tools or {}
            self.max_context_tokens = max_context_tokens
            self.fallback_to_cloud = fallback_to_cloud
            self.config = config or AgentConfig()
            self._cloud_fallback_fn = cloud_fallback_fn

        def run(self, prompt: str, state: Any | None = None,
                  *, max_iterations: int = 1) -> str:
            """Single-pass Ollama call → cloud fallback. No tool loop."""
            text = _call_ollama(prompt, self.model,
                                  system=self.system_prompt,
                                  config=self.config) or ""
            if not text and self._cloud_fallback_fn is not None:
                try:
                    cloud = self._cloud_fallback_fn(prompt, self.system_prompt)
                    if cloud:
                        text = cloud
                except Exception:
                    pass
            return text

        # The teaching/explanation surface from manufacturing_core's
        # BaseAgent — agents call self.explain(...) / explain_beginner(...)
        # to attach human-readable rationale to their output. In the
        # fallback we no-op (the calling code already swallows
        # AttributeError) but providing real methods keeps log spam down.
        def explain(self, *args, **kwargs) -> None:
            return None

        def explain_beginner(self, *args, **kwargs) -> None:
            return None

        def explain_advanced(self, *args, **kwargs) -> None:
            return None

        def teach(self, *args, **kwargs) -> None:
            return None


# ariaOS-specific config — points the manufacturing-core (or fallback)
# BaseAgent at the same Ollama instance + timeouts ariaOS has always used.
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
    """ariaOS BaseAgent — pre-wired with ariaOS defaults."""

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


__all__ = ["BaseAgent", "is_ollama_available", "_call_ollama",
            "AgentConfig", "call_ollama"]
