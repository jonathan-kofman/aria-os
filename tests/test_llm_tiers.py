"""Tests for aria_os/llm_client.py quality-tier routing.

We can't hit real API keys in CI, so we monkeypatch the `_try_*` backends
to record which providers get called in which order. This is where credit
bleeds live — we verify the chain is exactly what the tier promises."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os import llm_client  # noqa: E402


class TestQualityTiers:
    def setup_method(self):
        self.calls: list[str] = []

    def _patch_all_none(self, monkeypatch):
        """Patch every backend to return None so call_llm walks the full chain.
        Groq was added to the chain post-eval-fix; it must be patched here
        too or the real Groq backend answers before the mocks."""
        def make(name):
            def _fn(*a, **k):
                self.calls.append(name)
                return None
            return _fn
        monkeypatch.setattr(llm_client, "_try_groq", make("groq"))
        monkeypatch.setattr(llm_client, "_try_anthropic", make("anthropic"))
        monkeypatch.setattr(llm_client, "_try_gemini", make("gemini"))
        monkeypatch.setattr(llm_client, "_try_gemma", make("gemma"))
        monkeypatch.setattr(llm_client, "_try_ollama", make("ollama"))

    def test_balanced_default_order(self, monkeypatch):
        """Default tier: Groq → Gemini → Gemma → Sonnet → Ollama.
        Groq is the cost-conscious primary (free + sub-second + tool_use);
        Anthropic stays in the chain but never first."""
        self._patch_all_none(monkeypatch)
        result = llm_client.call_llm("test", "sys")
        assert result is None
        assert self.calls == ["groq", "gemini", "gemma", "anthropic", "ollama"]
        # crucial: anthropic must NOT be first
        assert self.calls[0] != "anthropic"

    def test_fast_tier_order(self, monkeypatch):
        self._patch_all_none(monkeypatch)
        llm_client.call_llm("test", quality="fast")
        assert self.calls == ["groq", "gemini", "gemma", "ollama", "anthropic"]
        # fast tier — anthropic is LAST resort
        assert self.calls[-1] == "anthropic"

    def test_premium_tier_order(self, monkeypatch):
        self._patch_all_none(monkeypatch)
        llm_client.call_llm("test", quality="premium")
        # Premium: quality first → Anthropic Sonnet, then Gemini, then
        # Groq as a free fallback, then Gemma/Ollama.
        assert self.calls == ["anthropic", "gemini", "groq", "gemma", "ollama"]

    def test_unknown_quality_falls_back_to_balanced(self, monkeypatch):
        self._patch_all_none(monkeypatch)
        llm_client.call_llm("test", quality="nonsense")
        # Balanced now starts with Groq
        assert self.calls[0] == "groq"

    def test_chain_short_circuits_on_success(self, monkeypatch):
        """Once a backend returns a string, the rest are not called."""
        def groq_ok(*a, **k):
            self.calls.append("groq")
            return "cadquery code here"
        def others(name):
            def _fn(*a, **k):
                self.calls.append(name)
                return None
            return _fn
        monkeypatch.setattr(llm_client, "_try_groq", groq_ok)
        monkeypatch.setattr(llm_client, "_try_gemini", others("gemini"))
        monkeypatch.setattr(llm_client, "_try_anthropic", others("anthropic"))
        monkeypatch.setattr(llm_client, "_try_gemma", others("gemma"))
        monkeypatch.setattr(llm_client, "_try_ollama", others("ollama"))
        result = llm_client.call_llm("test")
        assert result == "cadquery code here"
        # Balanced chain starts at groq; once it returns, no further calls.
        assert self.calls == ["groq"]

    def test_exception_in_backend_doesnt_halt_chain(self, monkeypatch):
        """A throwing backend should NOT halt the chain — we try the next one."""
        def groq_raises(*a, **k):
            self.calls.append("groq")
            raise ConnectionError("groq dns")
        def gemini_raises(*a, **k):
            self.calls.append("gemini")
            raise ConnectionError("dns")
        def gemma_ok(*a, **k):
            self.calls.append("gemma")
            return "result from gemma"
        def others(name):
            def _fn(*a, **k):
                self.calls.append(name)
                return None
            return _fn
        monkeypatch.setattr(llm_client, "_try_groq", groq_raises)
        monkeypatch.setattr(llm_client, "_try_gemini", gemini_raises)
        monkeypatch.setattr(llm_client, "_try_gemma", gemma_ok)
        monkeypatch.setattr(llm_client, "_try_anthropic", others("anthropic"))
        monkeypatch.setattr(llm_client, "_try_ollama", others("ollama"))
        r = llm_client.call_llm("test", quality="balanced")
        assert r == "result from gemma"
        # all 3 prior backends were attempted despite raising
        assert "groq" in self.calls
        assert "gemini" in self.calls
        assert "gemma" in self.calls

    def test_local_first_still_works(self, monkeypatch):
        """call_llm_local_first should still prefer Gemma then Gemini then
        Anthropic then Ollama (the non-code path)."""
        self._patch_all_none(monkeypatch)
        llm_client.call_llm_local_first("test")
        # Must try local models before billing anything
        assert self.calls[0] == "gemma"
        # Anthropic should be 3rd or later
        assert self.calls.index("anthropic") >= 2


class TestRecordCounts:
    def test_counts_reset_clean(self):
        llm_client.reset_llm_call_counts()
        assert llm_client.llm_call_counts() == {}

    def test_record_increments(self):
        llm_client.reset_llm_call_counts()
        llm_client._record_llm_call("anthropic")
        llm_client._record_llm_call("anthropic")
        llm_client._record_llm_call("gemini")
        counts = llm_client.llm_call_counts()
        assert counts["anthropic"] == 2
        assert counts["gemini"] == 1


class TestAnthropicModelTier:
    def test_fast_tier_uses_haiku_when_patched(self, monkeypatch):
        """Verify _try_anthropic(model_tier='fast') targets Haiku, not Sonnet."""
        called_models = []
        class FakeClient:
            class messages:
                @staticmethod
                def create(model, **kw):
                    called_models.append(model)
                    class _M:
                        content = [type("B", (), {"text": "ok"})()]
                    return _M()
        class FakeAnthropic:
            def __init__(self, api_key): pass
            messages = FakeClient.messages
        fake_module = type("M", (), {"Anthropic": FakeAnthropic})()
        monkeypatch.setitem(sys.modules, "anthropic", fake_module)
        monkeypatch.setattr(llm_client, "get_anthropic_key", lambda *a, **k: "fake-key")
        r = llm_client._try_anthropic("p", "", model_tier="fast")
        assert r == "ok"
        assert any("haiku" in m.lower() for m in called_models), \
            f"Expected haiku model, got {called_models}"

    def test_premium_tier_uses_sonnet(self, monkeypatch):
        called_models = []
        class FakeMessages:
            @staticmethod
            def create(model, **kw):
                called_models.append(model)
                class _M:
                    content = [type("B", (), {"text": "ok"})()]
                return _M()
        class FakeAnthropic:
            def __init__(self, api_key): pass
            messages = FakeMessages
        fake_module = type("M", (), {"Anthropic": FakeAnthropic})()
        monkeypatch.setitem(sys.modules, "anthropic", fake_module)
        monkeypatch.setattr(llm_client, "get_anthropic_key", lambda *a, **k: "fake-key")
        r = llm_client._try_anthropic("p", "", model_tier="premium")
        assert r == "ok"
        assert any("sonnet" in m.lower() for m in called_models)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
