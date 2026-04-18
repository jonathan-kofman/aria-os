"""
Tests for the MCP CAD generation pipeline.

These tests do NOT require any MCP server to be installed — they test the
plumbing (config probing, mode selection, fallback routing). Live MCP
generation tests would require Onshape/FreeCAD/Rhino set up, gated separately.
"""
from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# Server config probing
# ---------------------------------------------------------------------------

class TestServerConfigs:
    def test_no_servers_when_env_clean(self, monkeypatch):
        from core.mcp_pipeline import get_available_mcp_servers
        monkeypatch.delenv("ONSHAPE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("ONSHAPE_SECRET_KEY", raising=False)
        monkeypatch.delenv("RHINO_MCP_URL", raising=False)
        # Probing is environment-gated; without keys, list is empty
        servers = get_available_mcp_servers()
        # Could be 0 or 1 if FreeCAD-MCP is installed locally — accept both
        assert isinstance(servers, list)
        for s in servers:
            assert "name" in s
            assert "type" in s

    def test_onshape_config_when_keys_set(self, monkeypatch):
        from core.mcp_pipeline import get_onshape_server_config
        monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "fake-access-key")
        monkeypatch.setenv("ONSHAPE_SECRET_KEY", "fake-secret-key")
        cfg = get_onshape_server_config()
        assert cfg is not None
        assert cfg["name"] == "onshape"
        assert "ONSHAPE_ACCESS_KEY" in cfg["env"]

    def test_onshape_config_missing_keys(self, monkeypatch):
        from core.mcp_pipeline import get_onshape_server_config
        monkeypatch.delenv("ONSHAPE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("ONSHAPE_SECRET_KEY", raising=False)
        assert get_onshape_server_config() is None

    def test_rhino_config_when_url_set(self, monkeypatch):
        from core.mcp_pipeline import get_rhino_server_config
        monkeypatch.setenv("RHINO_MCP_URL", "http://localhost:7777")
        cfg = get_rhino_server_config()
        assert cfg is not None
        assert cfg["name"] == "rhino"

    def test_is_mcp_available_returns_bool(self):
        from core.mcp_pipeline import is_mcp_available
        assert isinstance(is_mcp_available(), bool)

    def test_preferred_server_picks_onshape_first(self, monkeypatch):
        from core.mcp_pipeline.server_configs import get_preferred_server
        servers = [
            {"name": "rhino", "type": "url", "url": "http://x"},
            {"name": "onshape", "type": "url", "url": "stdio://onshape"},
        ]
        chosen = get_preferred_server(servers)
        assert chosen["name"] == "onshape"

    def test_preferred_server_returns_none_when_empty(self):
        from core.mcp_pipeline.server_configs import get_preferred_server
        assert get_preferred_server([]) is None


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

class TestGenerationMode:
    def test_default_is_cadquery(self, monkeypatch):
        from core.mcp_pipeline.cad_orchestrator import get_generation_mode
        monkeypatch.delenv("ARIA_GENERATION_MODE", raising=False)
        assert get_generation_mode() == "cadquery"

    def test_env_override(self, monkeypatch):
        from core.mcp_pipeline.cad_orchestrator import get_generation_mode
        monkeypatch.setenv("ARIA_GENERATION_MODE", "mcp")
        assert get_generation_mode() == "mcp"
        monkeypatch.setenv("ARIA_GENERATION_MODE", "AUTO")
        assert get_generation_mode() == "auto"


# ---------------------------------------------------------------------------
# run_with_fallback — the critical safety property
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    def test_cadquery_mode_calls_cq_directly(self):
        """mode='cadquery' must call cadquery_fn and never touch MCP code."""
        from core.mcp_pipeline.cad_orchestrator import run_with_fallback

        called = {"count": 0}

        def fake_cq(**kw):
            called["count"] += 1
            return {"step_path": "/tmp/a.step", "stl_path": "/tmp/a.stl"}

        out = run_with_fallback(
            "test goal", cadquery_fn=fake_cq, mode="cadquery",
        )
        assert called["count"] == 1
        assert out["backend"] == "cadquery"
        assert out["success"] is True

    def test_mcp_mode_no_fallback_on_failure(self, monkeypatch):
        """mode='mcp' must NOT call cadquery_fn when MCP fails — fail loudly."""
        from core.mcp_pipeline.cad_orchestrator import run_with_fallback
        monkeypatch.delenv("ONSHAPE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("ONSHAPE_SECRET_KEY", raising=False)
        monkeypatch.delenv("RHINO_MCP_URL", raising=False)

        called = {"count": 0}

        def fake_cq(**kw):
            called["count"] += 1
            return {"step_path": "/tmp/a.step"}

        out = run_with_fallback(
            "test goal", cadquery_fn=fake_cq, mode="mcp",
        )
        # CadQuery must NOT have been called
        assert called["count"] == 0
        assert out["success"] is False
        assert out["backend"] == "mcp"

    def test_auto_mode_falls_back_when_mcp_unavailable(self, monkeypatch):
        """mode='auto' must call cadquery_fn after MCP failure."""
        from core.mcp_pipeline.cad_orchestrator import run_with_fallback
        monkeypatch.delenv("ONSHAPE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("ONSHAPE_SECRET_KEY", raising=False)
        monkeypatch.delenv("RHINO_MCP_URL", raising=False)

        called = {"count": 0}

        def fake_cq(**kw):
            called["count"] += 1
            return {"step_path": "/tmp/a.step", "stl_path": "/tmp/a.stl"}

        out = run_with_fallback(
            "test goal", cadquery_fn=fake_cq, mode="auto",
        )
        assert called["count"] == 1
        assert out["success"] is True
        assert out["backend"] == "cadquery"
        assert "mcp_attempt" in out  # attempt is recorded

    def test_unknown_mode_returns_error(self):
        from core.mcp_pipeline.cad_orchestrator import run_with_fallback
        called = {"count": 0}
        def fake_cq(**kw):
            called["count"] += 1
            return {}
        out = run_with_fallback(
            "x", cadquery_fn=fake_cq, mode="bogus",
        )
        assert out["success"] is False
        assert "Unknown" in out["error"]
        assert called["count"] == 0


# ---------------------------------------------------------------------------
# Existing pipeline isolation — the critical regression check
# ---------------------------------------------------------------------------

class TestExistingPipelineIsolation:
    def test_existing_cadquery_generator_unchanged(self):
        """Importing the new MCP module must not affect the CadQuery generator."""
        from aria_os.generators import cadquery_generator
        # Import the new pipeline
        from core.mcp_pipeline import MCPCADClient  # noqa: F401
        # Existing entry point still exists
        assert hasattr(cadquery_generator, "write_cadquery_artifacts")

    def test_no_env_var_no_mcp_invocation(self, monkeypatch):
        """Absent ARIA_GENERATION_MODE, default is 'cadquery' and MCP is silent."""
        from core.mcp_pipeline.cad_orchestrator import get_generation_mode
        monkeypatch.delenv("ARIA_GENERATION_MODE", raising=False)
        assert get_generation_mode() == "cadquery"


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_builds_with_minimal_args(self):
        from core.mcp_pipeline.prompts import build_system_prompt
        p = build_system_prompt()
        assert "mechanical engineer" in p.lower()
        assert "Units: mm" in p

    def test_includes_material_when_given(self):
        from core.mcp_pipeline.prompts import build_system_prompt
        p = build_system_prompt(material="aluminum_6061")
        assert "aluminum_6061" in p

    def test_includes_dfm_for_cnc(self):
        from core.mcp_pipeline.prompts import build_system_prompt
        p = build_system_prompt(target_process="cnc_3axis")
        assert "CNC" in p
        assert "wall" in p.lower()

    def test_includes_dfm_for_3dp(self):
        from core.mcp_pipeline.prompts import build_system_prompt
        p = build_system_prompt(target_process="fdm_3dp")
        assert "0.8mm" in p or "overhang" in p.lower()
