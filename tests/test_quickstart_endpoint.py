"""tests/test_quickstart_endpoint.py

Smoke + contract tests for the YC-application Quickstart endpoint
(POST /api/v1/quickstart/generate) added in dashboard/aria_server.py.

Goals (per the brief):
  - The endpoint exists, accepts a JSON {goal, mode} body
  - Validates input (empty goal -> 422)
  - Tags the run as surface=quickstart in the SSE event stream
  - Wraps the same _run_pipeline path /api/generate uses (no orchestrator
    duplication) — verified by patching _run_pipeline and asserting it's
    invoked with the goal passed in
  - The shared SSE stream (/api/log/stream) opens and begins emitting
    immediately after a quickstart submission

These tests stub the heavy pipeline call so they don't actually generate
geometry — we only care about the contract here. End-to-end pipeline
correctness is covered by the existing test_e2e_pipeline.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable so we can `from dashboard.aria_server import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fastapi_available = True
try:
    from fastapi.testclient import TestClient  # noqa: F401
except Exception:
    fastapi_available = False

pytestmark = pytest.mark.skipif(
    not fastapi_available, reason="fastapi/httpx not installed"
)


def _client():
    """Late-import the dashboard app to avoid loading it at module level
    (matches the pattern used in tests/test_w9_vr_endpoints.py)."""
    from fastapi.testclient import TestClient
    from dashboard.aria_server import app
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------

class TestQuickstartContract:
    def test_endpoint_exists_and_returns_200_for_valid_goal(self, monkeypatch):
        """Smoke: POST /api/v1/quickstart/generate accepts a non-empty
        goal and returns 200 with status=started. The actual pipeline
        is patched to a no-op so the test runs in milliseconds."""
        import dashboard.aria_server as srv

        called = {}

        def _fake_pipeline(goal, max_attempts, mode, quality_tier, host_context):
            called["goal"] = goal
            called["mode"] = mode
            called["quality_tier"] = quality_tier

        monkeypatch.setattr(srv, "_run_pipeline", _fake_pipeline)

        body = {
            "goal": "100mm flange with 4 M6 bolts",
            "mode": "text",
            "quality_tier": "balanced",
        }
        resp = _client().post("/api/v1/quickstart/generate", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "started"
        assert data["surface"] == "quickstart"
        assert data["input_mode"] == "text"
        assert data["goal"] == body["goal"]
        # Routed mode comes from _auto_detect_mode — for this goal it
        # should not be electrical/assembly. The exact value isn't load
        # bearing for this test, just that we got SOMETHING back.
        assert data["mode"] in {"native", "mechanical", "sheetmetal",
                                  "kicad", "dwg", "asm"}

    def test_empty_goal_returns_422(self):
        """Validation: empty / whitespace-only goal must be rejected
        with HTTP 422 so the frontend surfaces an error instead of
        kicking off a pipeline with no input."""
        resp = _client().post(
            "/api/v1/quickstart/generate",
            json={"goal": "   ", "mode": "text"},
        )
        assert resp.status_code == 422, resp.text

    def test_missing_goal_returns_422(self):
        """Pydantic validation: goal is required."""
        resp = _client().post("/api/v1/quickstart/generate", json={"mode": "text"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Pipeline-reuse guarantee
# ---------------------------------------------------------------------------

class TestQuickstartReusesPipeline:
    def test_calls_underlying_run_pipeline(self, monkeypatch):
        """The whole point of /api/v1/quickstart/generate is that it does
        NOT duplicate orchestrator logic — it must funnel into the same
        _run_pipeline helper that /api/generate uses."""
        import dashboard.aria_server as srv

        invocations = []

        def _spy(goal, max_attempts, mode, quality_tier, host_context):
            invocations.append({
                "goal": goal,
                "max_attempts": max_attempts,
                "mode": mode,
                "quality_tier": quality_tier,
                "host_context": host_context,
            })

        monkeypatch.setattr(srv, "_run_pipeline", _spy)

        resp = _client().post(
            "/api/v1/quickstart/generate",
            json={"goal": "L-bracket 80x60x40mm with 4 M6 holes",
                  "mode": "text"},
        )
        assert resp.status_code == 200

        # Background executor — give it a tick to run.
        import time
        for _ in range(20):
            if invocations:
                break
            time.sleep(0.05)

        assert len(invocations) >= 1
        inv = invocations[0]
        assert inv["goal"] == "L-bracket 80x60x40mm with 4 M6 holes"
        # quickstart never passes host_context — that's a hosted-panel
        # concern. The endpoint must explicitly pass None.
        assert inv["host_context"] is None


# ---------------------------------------------------------------------------
# SSE stream begins after submission (the acceptance criterion)
# ---------------------------------------------------------------------------

class TestQuickstartSSEStreamBegins:
    def test_event_stream_emits_quickstart_tag_after_submit(self, monkeypatch):
        """End-to-end smoke: hit /api/v1/quickstart/generate, then read
        /api/log/recent and confirm a 'surface=quickstart' tagged event
        appears. /api/log/recent is the synchronous mirror of the SSE
        history — using it avoids the test having to deal with chunked
        transfer encoding under TestClient.

        This is the core acceptance criterion: 'Pipeline progress streams.'
        """
        import dashboard.aria_server as srv

        # Stub the pipeline so we don't spin up CadQuery
        monkeypatch.setattr(srv, "_run_pipeline",
                            lambda *a, **kw: None)

        client = _client()
        resp = client.post(
            "/api/v1/quickstart/generate",
            json={"goal": "small mounting bracket", "mode": "text"},
        )
        assert resp.status_code == 200

        # The endpoint emits two `step` events synchronously before
        # dispatching the pipeline executor — the quickstart-tagged
        # `surface=quickstart` event and a `Received goal:` event.
        # Both should be visible in the SSE history immediately.
        recent = client.get("/api/log/recent?n=20").json()
        events = recent.get("events", [])
        assert events, "no events in /api/log/recent after submit"

        # Look for our surface tag — this is what makes quickstart traffic
        # distinguishable from chat-panel / dashboard traffic later.
        tagged = [e for e in events
                   if "surface=quickstart" in (e.get("message") or "")
                      or (e.get("data") or {}).get("surface") == "quickstart"]
        assert tagged, (
            "no surface=quickstart tagged event in stream — the pipeline "
            "must emit a quickstart-tagged step so analytics can identify "
            f"the run later. events seen: {[e.get('message') for e in events[-5:]]}"
        )

    def test_sse_endpoint_is_routed(self):
        """/api/log/stream is the channel the quickstart frontend
        subscribes to via EventSource after the POST returns. We can't
        easily consume an infinite SSE stream under TestClient without
        a hang, so this test asserts the route is REGISTERED on the
        FastAPI app — which is the actual contract we care about.
        """
        from dashboard.aria_server import app
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/log/stream" in paths, (
            f"/api/log/stream is missing from app.routes — frontend "
            f"EventSource will fail. Registered: {sorted(p for p in paths if p)}"
        )

    def test_quickstart_route_is_registered(self):
        """The new endpoint must be wired up on the same FastAPI app
        that already serves /api/generate."""
        from dashboard.aria_server import app
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/v1/quickstart/generate" in paths, (
            f"/api/v1/quickstart/generate not registered. "
            f"Registered: {sorted(p for p in paths if p)[:30]}"
        )
