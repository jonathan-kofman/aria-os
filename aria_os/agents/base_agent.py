"""Base agent class with Ollama calling and tool dispatch."""
from __future__ import annotations

import json
import re
import traceback
from typing import Any, Callable

from .design_state import DesignState
from .ollama_config import OLLAMA_HOST, OLLAMA_TIMEOUT, CONTEXT_LIMITS


class BaseAgent:
    """
    Base class for all ARIA pipeline agents.

    Wraps Ollama LLM calls with:
    - Focused system prompts per agent role
    - Structured tool dispatch (TOOL_CALL: pattern parsing)
    - Context truncation for small models
    - Fallback to cloud LLM if Ollama unavailable
    """

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: str = "llama3.1:8b",
        tools: dict[str, Callable] | None = None,
        max_context_tokens: int = 4000,
        fallback_to_cloud: bool = False,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model
        self.tools = tools or {}
        self.max_context_tokens = max_context_tokens
        self.fallback_to_cloud = fallback_to_cloud

    def run(self, user_prompt: str, state: DesignState) -> str:
        """
        Call Ollama with system + user prompt.
        Parse TOOL_CALL: patterns from response, execute tools, re-inject results.
        Returns final text response after up to 3 tool-call rounds.
        """
        from .. import event_bus

        truncated_prompt = self._truncate_context(user_prompt)
        messages = [truncated_prompt]
        full_response = ""

        for tool_round in range(4):  # max 3 tool rounds + 1 final
            current_prompt = "\n".join(messages)
            event_bus.emit("agent", f"[{self.name}] round {tool_round + 1}", {
                "agent": self.name, "model": self.model, "round": tool_round + 1,
            })

            response = self._call_llm(current_prompt)
            if response is None:
                return f"[{self.name}] LLM call failed — no response"

            full_response = response

            # Parse tool calls
            tool_calls = self._parse_tool_calls(response)
            if not tool_calls:
                break  # no more tool calls — done

            # Execute tool calls and inject results
            tool_results = []
            for fn_name, args in tool_calls:
                result = self._execute_tool(fn_name, args, state)
                tool_results.append(f"TOOL_RESULT ({fn_name}): {result}")

            messages.append(response)
            messages.append("\n".join(tool_results))
            messages.append("Continue based on the tool results above.")

        return full_response

    def _call_llm(self, prompt: str) -> str | None:
        """Call Ollama, falling back to cloud if configured."""
        # Try Ollama first
        response = _call_ollama(prompt, self.system_prompt, self.model)
        if response:
            return response

        # Fallback to cloud if enabled
        if self.fallback_to_cloud:
            try:
                from ..llm_client import call_llm
                return call_llm(prompt, system=self.system_prompt)
            except Exception:
                pass

        return None

    def _parse_tool_calls(self, response: str) -> list[tuple[str, list[str]]]:
        """
        Extract TOOL_CALL: func_name(arg1, arg2) patterns from LLM response.
        Returns list of (function_name, [args]).
        """
        calls = []
        pattern = r"TOOL_CALL:\s*(\w+)\(([^)]*)\)"
        for match in re.finditer(pattern, response):
            fn_name = match.group(1)
            raw_args = match.group(2).strip()
            if raw_args:
                # Split by comma, strip quotes and whitespace
                args = [a.strip().strip("'\"") for a in raw_args.split(",")]
            else:
                args = []
            calls.append((fn_name, args))
        return calls

    def _execute_tool(self, fn_name: str, args: list[str], state: DesignState) -> str:
        """Execute a tool by name, return result as string."""
        if fn_name not in self.tools:
            return f"ERROR: Unknown tool '{fn_name}'. Available: {list(self.tools.keys())}"

        fn = self.tools[fn_name]
        try:
            # Try calling with parsed args
            if args:
                result = fn(*args)
            else:
                result = fn()

            # Truncate large results
            result_str = json.dumps(result, default=str) if not isinstance(result, str) else result
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "...(truncated)"
            return result_str

        except Exception as exc:
            return f"ERROR executing {fn_name}: {exc}"

    def _truncate_context(self, text: str) -> str:
        """Rough truncation to fit within context window."""
        # Approximate: 1 token ~ 0.75 words ~ 4 chars
        max_chars = self.max_context_tokens * 4
        if len(text) <= max_chars:
            return text
        # Keep first and last portions
        half = max_chars // 2
        return text[:half] + "\n...(context trimmed)...\n" + text[-half:]


def _call_ollama(prompt: str, system: str, model: str, json_mode: bool = False) -> str | None:
    """Direct Ollama HTTP API call. Returns response text or None."""
    import urllib.request
    import urllib.error

    url = f"{OLLAMA_HOST}/api/generate"
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        **({"format": "json"} if json_mode else {}),
        "options": {
            "temperature": 0.2,
            "num_predict": 4096,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
            return data.get("response", "")
    except urllib.error.URLError as exc:
        print(f"  [{model}] Ollama unavailable: {exc}")
        return None
    except Exception as exc:
        print(f"  [{model}] Ollama error: {exc}")
        return None


def is_ollama_available() -> bool:
    """Quick health check — is Ollama running?"""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False
