"""W9 ARIA-VR endpoint tests.

Pin the contract for /ws/model_updates broadcast,
/api/voice_plan upload+transcribe+plan flow, and
/api/measurements/save persistence — all without spinning up a
real headset.

These tests use FastAPI's TestClient so we hit the real handlers
(not mocked stubs) but stub the LLM / STT entry points so the
tests don't burn API credits or require a Whisper backend.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


def _client():
    """Late-import to avoid loading the whole dashboard at module import."""
    from fastapi.testclient import TestClient
    from dashboard.aria_server import app
    return TestClient(app)


# --- W9.2 WebSocket model-update broadcast -----------------------------

class TestModelUpdateWebSocket:
    def test_hello_on_connect(self):
        with _client().websocket_connect("/ws/model_updates") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["event"] == "hello"
            assert msg["subscribers"] >= 1

    def test_ping_pong(self):
        with _client().websocket_connect("/ws/model_updates") as ws:
            ws.receive_text()  # hello
            ws.send_text(json.dumps({"event": "ping"}))
            reply = json.loads(ws.receive_text())
            assert reply["event"] == "pong"

    def test_broadcast_reaches_subscriber(self):
        """Open a WS connection, programmatically call the broadcast
        helper, verify the new_model event lands."""
        import asyncio
        from dashboard.aria_server import broadcast_model_update

        with _client().websocket_connect("/ws/model_updates") as ws:
            ws.receive_text()  # hello
            # Run the async broadcast from a sync test by spinning a
            # fresh event loop just for this call. The TestClient
            # uses anyio under the hood so the WS itself is happy.
            loop = asyncio.new_event_loop()
            try:
                n = loop.run_until_complete(
                    broadcast_model_update(
                        "/outputs/runs/abc/part.glb",
                        run_id="abc"))
            finally:
                loop.close()
            assert n >= 1
            msg = json.loads(ws.receive_text())
            assert msg["event"] == "new_model"
            assert msg["url"].endswith("part.glb")
            assert msg["run_id"] == "abc"


# --- W9.3 /api/voice_plan endpoint --------------------------------------

class TestVoicePlanEndpoint:
    """The endpoint orchestrates: STT → resolve target → classify
    intent → plan → broadcast. We stub STT + planner so we exercise
    the orchestration without external deps."""

    def test_resolves_demonstrative_with_selection(self, monkeypatch):
        """'make this hole 2mm bigger' + selection in host_context →
        feature alias substituted into the resolved goal."""
        # Stub transcribe — return a fixed utterance
        import aria_os.speech_to_text as stt
        monkeypatch.setattr(
            stt, "transcribe",
            lambda wav_path: "make this hole 2mm bigger")
        # Stub the planner — return a tiny valid plan we can inspect
        import aria_os.native_planner.dispatcher as disp
        monkeypatch.setattr(
            disp, "make_plan",
            lambda goal, spec, **kw: [{"kind": "addParameter",
                                         "params": {"name": "captured_goal",
                                                      "value": goal}}])

        c = _client()
        ctx = {"selection": [
            {"type": "edge", "id": "e1",
             "feature": "bolt_hole_3"}]}
        files = {"audio": ("voice.webm",
                            io.BytesIO(b"fakewebmbytes"),
                            "audio/webm")}
        r = c.post("/api/voice_plan",
                    files=files,
                    data={"host_context_json": json.dumps(ctx),
                           "quality": "balanced"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transcription"] == "make this hole 2mm bigger"
        assert body["intent"] == "modify"
        assert body["mode"] == "modify"
        assert body["resolved_target"]["feature_alias"] == "bolt_hole_3"
        # The substituted goal must contain the alias and not "this"
        assert "bolt_hole_3" in body["goal"]
        # The captured plan param confirms goal flowed through
        plan = body["plan"]
        assert plan and plan[0]["kind"] == "addParameter"
        assert "bolt_hole_3" in plan[0]["params"]["value"]

    def test_no_audio_returns_422(self):
        c = _client()
        r = c.post("/api/voice_plan", data={})
        # FastAPI returns 422 for missing required field
        assert r.status_code == 422

    def test_malformed_host_context_returns_422(self, monkeypatch):
        import aria_os.speech_to_text as stt
        monkeypatch.setattr(stt, "transcribe",
                              lambda wav_path: "make a flange")
        c = _client()
        files = {"audio": ("v.webm", io.BytesIO(b"x"), "audio/webm")}
        r = c.post("/api/voice_plan", files=files,
                    data={"host_context_json": "{not json}"})
        assert r.status_code == 422
        assert "host_context_json" in r.json()["detail"]

    def test_transcription_failure_returns_502(self, monkeypatch):
        import aria_os.speech_to_text as stt
        monkeypatch.setattr(stt, "transcribe", lambda wav_path: "")
        c = _client()
        files = {"audio": ("v.webm", io.BytesIO(b"x"), "audio/webm")}
        r = c.post("/api/voice_plan", files=files, data={})
        assert r.status_code == 502


# --- W9.4 /api/measurements/save persistence ---------------------------

class TestMeasurementsSave:
    def test_persists_to_outputs_vr_run_id(self, tmp_path, monkeypatch):
        """Persistence is keyed by run_id; the file ends up under
        outputs/vr/<run_id>/measurements.json relative to REPO_ROOT."""
        import dashboard.aria_server as srv
        # Redirect REPO_ROOT to a tmp path so the test doesn't pollute
        # the real outputs/ directory.
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)

        c = _client()
        payload = {
            "run_id": "20260425T120000_abc",
            "model_url": "/outputs/runs/20260425T120000_abc/part.glb",
            "measurements": [
                {"kind": "distance", "points": [[0,0,0], [10,0,0]],
                 "value": 10.0, "label": "OD"},
            ],
        }
        r = c.post("/api/measurements/save", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        # The file actually exists in the redirected outputs/
        saved = tmp_path / "outputs" / "vr" / payload["run_id"] / \
            "measurements.json"
        assert saved.is_file()
        roundtrip = json.loads(saved.read_text())
        assert roundtrip["run_id"] == payload["run_id"]
        assert len(roundtrip["measurements"]) == 1

    def test_missing_run_id_falls_back_to_untracked(
            self, tmp_path, monkeypatch):
        import dashboard.aria_server as srv
        monkeypatch.setattr(srv, "REPO_ROOT", tmp_path)
        c = _client()
        r = c.post("/api/measurements/save",
                    json={"measurements": []})
        assert r.status_code == 200
        # Untracked dir created
        assert (tmp_path / "outputs" / "vr" / "untracked"
                / "measurements.json").is_file()
