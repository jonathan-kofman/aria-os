"""
aria_os/compute_client.py — Rhino Compute REST client

Talks to a running compute.geometry server (default: http://localhost:8081).
Provides health checks, Grasshopper definition solving, and RhinoCommon
geometry operations via the Resthopper protocol.

Usage:
    from aria_os.compute_client import ComputeClient

    client = ComputeClient()
    if client.is_available():
        result = client.solve_grasshopper(gh_script_code, inputs={...})
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional


RHINO_COMPUTE_URL = os.environ.get("RHINO_COMPUTE_URL", "http://localhost:8081")


class ComputeClient:
    """Stateless REST client for Rhino Compute."""

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = (url or RHINO_COMPUTE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("RHINO_COMPUTE_KEY")
        self._version_cache: dict | None = None

    # -------------------------------------------------------------------
    # Health / status
    # -------------------------------------------------------------------

    def is_available(self, timeout: float = 5.0) -> bool:
        """Return True if Compute is reachable and healthy."""
        try:
            resp = self._get("/healthcheck", timeout=timeout)
            return resp == "Healthy"
        except Exception:
            return False

    def version(self, timeout: float = 5.0) -> dict:
        """Return Compute version info."""
        if self._version_cache is None:
            raw = self._get("/version", timeout=timeout)
            self._version_cache = json.loads(raw)
        return self._version_cache

    # -------------------------------------------------------------------
    # Grasshopper endpoint (Resthopper protocol)
    # -------------------------------------------------------------------

    def solve_grasshopper(
        self,
        definition: str | Path | None = None,
        *,
        algo_base64: str | None = None,
        pointer: str | None = None,
        values: list[dict] | None = None,
        absolute_tolerance: float = 0.001,
        angle_tolerance: float = 1.0,
        model_units: str = "Millimeters",
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """
        Solve a Grasshopper definition via Rhino Compute.

        Provide ONE of:
          - definition: path to a .gh/.ghx file (will be base64-encoded)
          - algo_base64: already-encoded definition string
          - pointer: URL to a .gh file accessible by Compute

        values: list of input param dicts, each with:
            {"ParamName": "radius", "InnerTree": {"0": [{"type": "System.Double", "data": "25.0"}]}}

        Returns the raw Resthopper response dict (outputs in "values" key).
        """
        payload: dict[str, Any] = {
            "absolutetolerance": absolute_tolerance,
            "angletolerance": angle_tolerance,
            "modelunits": model_units,
            "recursionlevel": 0,
            "values": values or [],
        }

        if definition is not None:
            p = Path(definition)
            if p.exists():
                raw = p.read_bytes()
                payload["algo"] = base64.b64encode(raw).decode("ascii")
            else:
                raise FileNotFoundError(f"GH definition not found: {definition}")
        elif algo_base64 is not None:
            payload["algo"] = algo_base64
        elif pointer is not None:
            payload["pointer"] = pointer
        else:
            raise ValueError("Provide definition, algo_base64, or pointer")

        body = json.dumps(payload).encode("utf-8")
        resp = self._post("/grasshopper", body, timeout=timeout)
        return json.loads(resp)

    def get_io(
        self,
        definition: str | Path | None = None,
        *,
        algo_base64: str | None = None,
        pointer: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """
        Get input/output names and types for a Grasshopper definition.
        """
        payload: dict[str, Any] = {}

        if definition is not None:
            p = Path(definition)
            if p.exists():
                raw = p.read_bytes()
                payload["algo"] = base64.b64encode(raw).decode("ascii")
            else:
                raise FileNotFoundError(f"GH definition not found: {definition}")
        elif algo_base64 is not None:
            payload["algo"] = algo_base64
        elif pointer is not None:
            payload["pointer"] = pointer

        body = json.dumps(payload).encode("utf-8")
        resp = self._post("/io", body, timeout=timeout)
        return json.loads(resp)

    # -------------------------------------------------------------------
    # RhinoCommon geometry endpoints
    # -------------------------------------------------------------------

    def geometry_post(
        self,
        endpoint: str,
        args: list[Any],
        timeout: float = 30.0,
    ) -> Any:
        """
        Call a RhinoCommon geometry endpoint.

        Example:
            client.geometry_post("rhino/geometry/brep/createbooleandifference", [brep_a, brep_b, 0.001])
        """
        body = json.dumps(args).encode("utf-8")
        resp = self._post(f"/{endpoint.strip('/')}", body, timeout=timeout)
        return json.loads(resp)

    # -------------------------------------------------------------------
    # Input helpers — build Resthopper input values
    # -------------------------------------------------------------------

    @staticmethod
    def make_input(name: str, value: Any, tree_path: str = "0") -> dict:
        """
        Build a single Resthopper input parameter.

        Handles common Python types → Resthopper type mapping.
        """
        if isinstance(value, (int, float)):
            rh_type = "System.Double"
            data = str(float(value))
        elif isinstance(value, str):
            rh_type = "System.String"
            data = value
        elif isinstance(value, bool):
            rh_type = "System.Boolean"
            data = str(value).lower()
        else:
            rh_type = "System.String"
            data = json.dumps(value)

        return {
            "ParamName": name,
            "InnerTree": {
                tree_path: [{"type": rh_type, "data": data}]
            },
        }

    @staticmethod
    def make_input_list(params: dict[str, Any]) -> list[dict]:
        """Convert a flat dict of params to Resthopper input values."""
        return [
            ComputeClient.make_input(k, v)
            for k, v in params.items()
            if isinstance(v, (int, float, str, bool))
        ]

    # -------------------------------------------------------------------
    # HTTP helpers
    # -------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["RhinoComputeKey"] = self.api_key
        return h

    def _get(self, path: str, timeout: float = 10.0) -> str:
        req = urllib.request.Request(
            f"{self.url}{path}",
            headers=self._headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")

    def _post(self, path: str, body: bytes, timeout: float = 30.0) -> str:
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
