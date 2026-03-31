"""
tests/test_api_server.py — FastAPI server: 422 validation, health, runs log.

Covers:
  - GET /api/health: returns 200, backends dict with expected keys
  - GET /api/runs: returns runs list, respects limit param
  - POST /api/generate: 422 on empty/short description, 200/500 on valid input
  - _append_run: in-memory log + disk log behaviour
  - Run log growth cap (500 entries)
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip all tests if FastAPI is not installed
fastapi_available = True
try:
    from fastapi.testclient import TestClient
    from aria_os.api_server import app, _RUN_LOG, _append_run
except ImportError:
    fastapi_available = False

pytestmark = pytest.mark.skipif(
    not fastapi_available, reason="fastapi or httpx not installed"
)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from aria_os.api_server import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clear_run_log():
    """Clear in-memory log before each test."""
    if fastapi_available:
        from aria_os import api_server
        api_server._RUN_LOG.clear()
    yield
    if fastapi_available:
        from aria_os import api_server
        api_server._RUN_LOG.clear()


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_has_status_ok(self, client):
        data = client.get("/api/health").json()
        assert data.get("status") == "ok"

    def test_has_backends(self, client):
        data = client.get("/api/health").json()
        assert "backends" in data

    def test_backends_has_four_keys(self, client):
        data = client.get("/api/health").json()
        backends = data["backends"]
        for key in ("cadquery", "grasshopper", "blender", "fusion360"):
            assert key in backends

    def test_each_backend_has_available_key(self, client):
        data = client.get("/api/health").json()
        for name, info in data["backends"].items():
            assert "available" in info, f"Backend {name} missing 'available' key"

    def test_has_timestamp(self, client):
        data = client.get("/api/health").json()
        assert "timestamp" in data


# ---------------------------------------------------------------------------
# /api/runs
# ---------------------------------------------------------------------------

class TestRunsEndpoint:
    def test_empty_log_returns_empty_list(self, client):
        data = client.get("/api/runs").json()
        assert data["runs"] == []
        assert data["total"] == 0

    def test_limit_param_respected(self, client):
        from aria_os import api_server
        for i in range(10):
            api_server._RUN_LOG.append({"idx": i})
        data = client.get("/api/runs?limit=3").json()
        assert len(data["runs"]) == 3

    def test_returns_last_n(self, client):
        from aria_os import api_server
        for i in range(5):
            api_server._RUN_LOG.append({"idx": i})
        data = client.get("/api/runs?limit=2").json()
        assert data["runs"][-1]["idx"] == 4

    def test_total_reflects_full_log(self, client):
        from aria_os import api_server
        for i in range(7):
            api_server._RUN_LOG.append({"idx": i})
        data = client.get("/api/runs?limit=3").json()
        assert data["total"] == 7


# ---------------------------------------------------------------------------
# /api/generate — validation (422)
# ---------------------------------------------------------------------------

class TestGenerateValidation:
    def test_empty_description_422(self, client):
        resp = client.post("/api/generate", json={"description": ""})
        assert resp.status_code == 422

    def test_whitespace_only_422(self, client):
        resp = client.post("/api/generate", json={"description": "   "})
        assert resp.status_code == 422

    def test_too_short_422(self, client):
        resp = client.post("/api/generate", json={"description": "ab"})
        assert resp.status_code == 422

    def test_missing_description_422(self, client):
        resp = client.post("/api/generate", json={})
        assert resp.status_code == 422

    def test_valid_description_does_not_422(self, client):
        # May succeed (200) or fail (500) depending on deps, but never 422
        resp = client.post("/api/generate", json={"description": "simple bracket 50mm"})
        assert resp.status_code != 422


# ---------------------------------------------------------------------------
# _append_run
# ---------------------------------------------------------------------------

class TestAppendRun:
    def test_appends_to_in_memory_log(self):
        from aria_os import api_server
        api_server._RUN_LOG.clear()
        api_server._append_run({"test": "entry"})
        assert len(api_server._RUN_LOG) == 1
        assert api_server._RUN_LOG[0]["test"] == "entry"

    def test_caps_at_500(self):
        from aria_os import api_server
        api_server._RUN_LOG.clear()
        for i in range(510):
            api_server._append_run({"idx": i})
        assert len(api_server._RUN_LOG) <= 500

    def test_disk_log_written(self, tmp_path):
        from aria_os import api_server
        original_path = api_server._LOG_PATH
        api_server._LOG_PATH = tmp_path / "test_log.json"
        try:
            api_server._RUN_LOG.clear()
            api_server._append_run({"disk": "test"})
            assert (tmp_path / "test_log.json").exists()
        finally:
            api_server._LOG_PATH = original_path
            api_server._RUN_LOG.clear()
