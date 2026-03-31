"""
aria_api_tab.py — Streamlit tab for the ARIA-OS FastAPI server.

Shows server status, recent run log, and a generate form.
Start the server separately with: uvicorn aria_os.api_server:app --port 8000
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import streamlit as st

_API_URL_DEFAULT = "http://localhost:8000"
_SERVER_PROC: list = []  # holds subprocess.Popen instance


def _api_url() -> str:
    return st.session_state.get("api_url", _API_URL_DEFAULT)


def _get(path: str, params: dict | None = None) -> tuple[int, dict]:
    try:
        import requests
        resp = requests.get(f"{_api_url()}{path}", params=params, timeout=4)
        return resp.status_code, resp.json()
    except Exception as exc:
        return 0, {"error": str(exc)}


def _post(path: str, body: dict) -> tuple[int, dict]:
    try:
        import requests
        resp = requests.post(f"{_api_url()}{path}", json=body, timeout=120)
        return resp.status_code, resp.json()
    except Exception as exc:
        return 0, {"error": str(exc)}


def _start_server_background() -> None:
    """Launch uvicorn in a background subprocess (fire-and-forget)."""
    if _SERVER_PROC:
        return  # already started
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aria_os.api_server:app",
         "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _SERVER_PROC.append(proc)


def render_api_tab() -> None:
    st.header("ARIA-OS API Server")
    st.caption("FastAPI server — POST /api/generate · GET /api/health · GET /api/runs")

    # --- Connection settings ---
    with st.expander("Server settings", expanded=False):
        url = st.text_input("API base URL", value=_API_URL_DEFAULT, key="api_url_input")
        st.session_state["api_url"] = url
        col_start, col_stop = st.columns(2)
        with col_start:
            if st.button("Start server (background)", use_container_width=True):
                _start_server_background()
                st.success("Server launch requested on port 8000. Refresh health below.")
        with col_stop:
            st.caption("Or start manually:")
            st.code("uvicorn aria_os.api_server:app --reload", language="bash")

    # --- Health check ---
    st.subheader("Backend health")
    if st.button("Refresh health", use_container_width=False):
        code, data = _get("/api/health")
        if code == 200:
            st.success(f"Server OK — {data.get('timestamp', '')}")
            backends = data.get("backends", {})
            rows = []
            for name, info in backends.items():
                rows.append({
                    "Backend": name,
                    "Available": "✓" if info.get("available") else "✗",
                    "Info": info.get("version") or info.get("reason") or info.get("note") or "",
                })
            st.table(rows)
        else:
            st.error(f"Server unreachable (HTTP {code}): {data.get('error', '')}")

    st.divider()

    # --- Generate form ---
    st.subheader("Generate a part")
    description = st.text_area(
        "Part description",
        placeholder='e.g. "ARIA ratchet ring, 213mm OD, 24 teeth, 21mm thick"',
        height=80,
    )
    dry_run = st.checkbox("Dry run (plan + route only, no generation)", value=False)
    if st.button("Generate", type="primary", use_container_width=True):
        if not description.strip():
            st.warning("Enter a description first.")
        else:
            with st.spinner("Generating…"):
                code, data = _post("/api/generate", {"description": description, "dry_run": dry_run})
            if code == 200:
                st.success(
                    f"**{data.get('status', 'done')}** — "
                    f"backend: `{data.get('backend')}`, "
                    f"attempts: {data.get('attempts')}, "
                    f"elapsed: {data.get('elapsed_s')}s"
                )
                if data.get("step_path"):
                    st.code(f"STEP: {data['step_path']}\nSTL:  {data['stl_path']}")
                if data.get("warnings"):
                    for w in data["warnings"]:
                        st.warning(w)
            elif code == 422:
                st.error(f"Validation error: {json.dumps(data.get('detail', data), indent=2)}")
            else:
                st.error(f"Error (HTTP {code}): {data.get('detail') or data.get('error', '')}")

    st.divider()

    # --- Run log ---
    st.subheader("Recent runs")
    limit = st.number_input("Show last N runs", min_value=1, max_value=100, value=10)
    if st.button("Load runs", use_container_width=False):
        code, data = _get("/api/runs", {"limit": int(limit)})
        if code == 200:
            runs = data.get("runs", [])
            if not runs:
                st.info("No runs recorded yet.")
            else:
                st.caption(f"Total logged: {data.get('total', 0)}")
                for r in reversed(runs):
                    with st.expander(
                        f"{r.get('timestamp', '')[:19]}  ·  {r.get('description', '')[:60]}"
                    ):
                        st.json(r)
        else:
            st.error(f"Could not load runs (HTTP {code})")

    # --- Disk log ---
    st.divider()
    st.subheader("Persistent log (disk)")
    log_path = Path(__file__).resolve().parent.parent / "outputs" / "api_run_log.json"
    if log_path.exists():
        try:
            entries = json.loads(log_path.read_text(encoding="utf-8"))
            st.caption(f"{len(entries)} entries in {log_path}")
            if st.button("Show last 5 disk entries"):
                for e in entries[-5:]:
                    st.json(e)
        except Exception as exc:
            st.warning(f"Could not read log: {exc}")
    else:
        st.info("No disk log yet — runs appear here after the first successful generate.")
