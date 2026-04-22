"""Onshape REST client using API access + secret keys.

Implements the HMAC-SHA256 request signing scheme Onshape requires:
  https://onshape-public.github.io/docs/api-intro/#api-authentication

Every request signs:
  method, timestamp, nonce, path, query, content-type
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

import requests


ONSHAPE_BASE_URL = "https://cad.onshape.com"


def _load_keys(repo_root: Path | None = None) -> tuple[str, str]:
    """Read ONSHAPE_ACCESS_KEY + ONSHAPE_SECRET_KEY from env first,
    then .env at the repo root."""
    access = os.environ.get("ONSHAPE_ACCESS_KEY", "").strip()
    secret = os.environ.get("ONSHAPE_SECRET_KEY", "").strip()
    if access and secret:
        return access, secret
    if repo_root is None:
        # Walk up from this file to find the project root (has .env)
        here = Path(__file__).resolve()
        for p in [here.parent, *here.parents]:
            if (p / ".env").is_file():
                repo_root = p
                break
    if repo_root and (repo_root / ".env").is_file():
        for line in (repo_root / ".env").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k.strip() == "ONSHAPE_ACCESS_KEY" and not access: access = v
            if k.strip() == "ONSHAPE_SECRET_KEY" and not secret: secret = v
    return access, secret


class OnshapeClient:
    """Minimal Onshape REST client with HMAC request signing."""

    def __init__(self, access_key: str | None = None,
                  secret_key: str | None = None,
                  base_url: str = ONSHAPE_BASE_URL,
                  repo_root: Path | None = None):
        if access_key is None or secret_key is None:
            access_key, secret_key = _load_keys(repo_root)
        if not access_key or not secret_key:
            raise RuntimeError(
                "ONSHAPE_ACCESS_KEY + ONSHAPE_SECRET_KEY not set in env "
                "or .env. Get them at https://dev-portal.onshape.com.")
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")

    # --- Authentication --------------------------------------------------

    def _sign(self, method: str, path: str,
               query: str = "", content_type: str = "") -> dict[str, str]:
        """Compute the auth headers for a request."""
        now = datetime.datetime.now(datetime.UTC).strftime(
            "%a, %d %b %Y %H:%M:%S GMT")
        nonce = secrets.token_urlsafe(18)[:25]
        # Onshape lowercases the ENTIRE concatenated message for signing
        # (per https://github.com/onshape-public/apikey-python). Individually
        # lowercasing pieces is not the same — the nonce + date must also
        # be lowered because the signature is computed on the whole string.
        msg_str = (method + "\n"
                   + nonce + "\n"
                   + now + "\n"
                   + content_type + "\n"
                   + path + "\n"
                   + query + "\n").lower()
        msg = msg_str.encode("utf-8")
        sig = hmac.new(self.secret_key.encode("utf-8"),
                        msg, hashlib.sha256).digest()
        sig_b64 = base64.b64encode(sig).decode()
        return {
            "On-Nonce": nonce,
            "Date":     now,
            "Authorization": f"On {self.access_key}:HmacSHA256:{sig_b64}",
        }

    # --- Core HTTP -------------------------------------------------------

    def request(self, method: str, path: str,
                 *, params: dict | None = None,
                 json_body: dict | None = None,
                 timeout: float = 30) -> dict:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        content_type = "application/json" if json_body is not None else ""
        headers = {
            "Accept": "application/json;charset=UTF-8;qs=0.09",
            **self._sign(method, path, query, content_type),
        }
        if content_type:
            headers["Content-Type"] = content_type
        url = self.base_url + path + (f"?{query}" if query else "")
        resp = requests.request(
            method, url,
            headers=headers,
            data=json.dumps(json_body) if json_body is not None else None,
            timeout=timeout)
        if not resp.ok:
            raise RuntimeError(
                f"Onshape {method} {path} failed: {resp.status_code} "
                f"{resp.text[:400]}")
        if resp.content and resp.headers.get("Content-Type", "").startswith(
                "application/json"):
            return resp.json()
        return {}

    # --- Convenience wrappers -------------------------------------------

    def list_documents(self, limit: int = 20) -> list[dict]:
        """Return the user's recent documents."""
        r = self.request("GET", "/api/documents",
                          params={"limit": limit})
        return r.get("items", [])

    def list_part_studios(self, did: str, wid: str) -> list[dict]:
        """List Part Studios in a workspace."""
        r = self.request("GET",
                          f"/api/documents/d/{did}/w/{wid}/elements",
                          params={"elementType": "PARTSTUDIO"})
        if isinstance(r, list): return r
        return r.get("items", []) if isinstance(r, dict) else []

    def add_feature(self, did: str, wid: str, eid: str,
                     feature: dict) -> dict:
        """POST a new feature to a Part Studio's feature list."""
        return self.request(
            "POST",
            f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features",
            json_body={"feature": feature})

    def get_features(self, did: str, wid: str, eid: str) -> dict:
        return self.request(
            "GET", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features")


def get_client(repo_root: Path | None = None) -> OnshapeClient:
    """Cached accessor — reads env once, returns a reusable client."""
    return OnshapeClient(repo_root=repo_root)
