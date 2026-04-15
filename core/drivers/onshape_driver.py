"""
core/drivers/onshape_driver.py — Onshape REST API driver.

Onshape is a cloud-hosted parametric CAD. We use its REST API to:
  1. Create a new document + part studio.
  2. Run FeatureScript calls for each IGL feature (add a sketch on a face,
     extrude it, etc.).
  3. Export the resulting part studio as STEP and STL.

The hardest part of Onshape is EDGE REFERENCING. When you add a fillet,
you need to reference specific edges created by earlier features, and
Onshape uses deterministic edge IDs that depend on the feature tree. A
production driver would maintain a mapping from IGL feature IDs to the
specific Onshape entity IDs they produce. This v1 driver takes a simpler
approach:

  - For simple features (stock + boolean cuts), it builds the feature list
    sequentially and uses query-by-location-on-face selectors where
    possible.
  - For fillets and chamfers, it currently uses "all edges" queries which
    work for simple parts but may produce wrong results on complex ones.
  - Sheet metal features are not yet supported.

Credentials
-----------
Onshape requires an access key and secret key. Provide them via:
    ONSHAPE_ACCESS_KEY
    ONSHAPE_SECRET_KEY

If either is missing, is_available() returns False and the driver is
silently skipped. No network calls are attempted until generate() is
actually invoked.

The base URL defaults to https://cad.onshape.com/api/v9 and can be
overridden with ONSHAPE_BASE_URL for enterprise deployments.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..igl_schema import IGLDocument, StockBlock, StockCylinder, StockTube
from .base_driver import (
    CADDriver,
    DriverResult,
    _coerce_float,
    _coerce_int,
    igl_units_to_mm_scale,
    save_result_sidecar,
)


_ONSHAPE_DEFAULT_BASE_URL = "https://cad.onshape.com/api/v9"

_SUPPORTED_FEATURES = (
    "pocket",
    "hole",
    "hole_pattern",
    "cutout",
    "boss",
    "pad",
    "fillet",
    "chamfer",
)


# ---------------------------------------------------------------------------
# Onshape request signing
#
# Onshape uses an HMAC-SHA256 signature scheme similar to AWS v4 but
# simpler. Signature = HMAC(secret_key, method\nnonce\ndate\ncontent-type\npath\nquery\n)
# The Authorization header is "On <access_key>:HmacSHA256:<signature>".
# ---------------------------------------------------------------------------

def _sign_request(
    method: str,
    url: str,
    access_key: str,
    secret_key: str,
    content_type: str = "application/json",
) -> dict[str, str]:
    """Return headers for a signed Onshape API request."""
    parsed = urlparse(url)
    path = parsed.path
    query = parsed.query
    nonce = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    # Onshape spec: method, nonce, date, content-type, path, query, all
    # lowercased where appropriate, newline-terminated.
    hmac_input = (
        f"{method.lower()}\n"
        f"{nonce}\n"
        f"{now}\n"
        f"{content_type.lower()}\n"
        f"{path}\n"
        f"{query}\n"
    ).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), hmac_input, hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "Date": now,
        "On-Nonce": nonce,
        "Authorization": f"On {access_key}:HmacSHA256:{signature}",
        "Content-Type": content_type,
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# FeatureScript generation
#
# Onshape accepts "features" as JSON objects that the server interprets
# using its own FeatureScript engine. Each feature has a type, a name,
# and a dict of parameters. We keep the translation minimal — just
# enough to get through the IGL features this driver supports.
# ---------------------------------------------------------------------------


def _fs_stock(doc: IGLDocument) -> list[dict[str, Any]]:
    """
    Return a minimal list of FeatureScript feature dicts that produce the
    initial stock shape as an extrusion.
    """
    scale = igl_units_to_mm_scale(str(doc.part.units))
    stock = doc.stock

    if isinstance(stock, StockBlock):
        return [
            {
                "type": "sketch",
                "name": "stock_sketch",
                "plane": "top",
                "elements": [
                    {
                        "type": "rectangle",
                        "width": stock.x * scale,
                        "height": stock.y * scale,
                    }
                ],
            },
            {
                "type": "extrude",
                "name": "stock_extrude",
                "sketch": "stock_sketch",
                "depth": stock.z * scale,
                "operation": "new_body",
            },
        ]

    if isinstance(stock, StockCylinder):
        return [
            {
                "type": "sketch",
                "name": "stock_sketch",
                "plane": "top",
                "elements": [
                    {"type": "circle", "diameter": stock.diameter * scale}
                ],
            },
            {
                "type": "extrude",
                "name": "stock_extrude",
                "sketch": "stock_sketch",
                "depth": stock.height * scale,
                "operation": "new_body",
            },
        ]

    if isinstance(stock, StockTube):
        return [
            {
                "type": "sketch",
                "name": "stock_sketch",
                "plane": "top",
                "elements": [
                    {"type": "circle", "diameter": stock.outer_diameter * scale},
                    {"type": "circle", "diameter": stock.inner_diameter * scale},
                ],
            },
            {
                "type": "extrude",
                "name": "stock_extrude",
                "sketch": "stock_sketch",
                "depth": stock.height * scale,
                "operation": "new_body",
            },
        ]

    raise ValueError(f"unsupported stock type: {type(stock).__name__}")


def _fs_feature(feature, scale: float) -> Optional[dict[str, Any]]:
    """
    Translate a single IGL feature into a FeatureScript feature dict.

    Returns None if the feature is not supported — the caller should
    record this as a warning rather than failing the whole generation.
    """
    p = feature.params
    t = feature.type

    if t == "pocket":
        return {
            "type": "pocket",
            "name": feature.id,
            "face": p.get("face", "top"),
            "profile": p.get("profile", "rectangle"),
            "center": [
                _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
            ],
            "length": _coerce_float(p.get("length", 10.0), 10.0) * scale,
            "width": _coerce_float(p.get("width", 10.0), 10.0) * scale,
            "diameter": _coerce_float(p.get("diameter", 10.0), 10.0) * scale,
            "depth": _scale_depth(p.get("depth", 1.0), scale),
            "corner_radius": _coerce_float(p.get("corner_radius", 0.0), 0.0) * scale,
        }

    if t == "hole":
        return {
            "type": "hole",
            "name": feature.id,
            "face": p.get("face", "top"),
            "center": [
                _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
            ],
            "diameter": _coerce_float(p.get("diameter", 5.0), 5.0) * scale,
            "depth": _scale_depth(p.get("depth", "through"), scale),
            "hole_type": p.get("hole_type", "plain"),
            "cbore_diameter": _coerce_float(
                p.get("cbore_diameter", 0.0), 0.0
            ) * scale,
            "cbore_depth": _coerce_float(p.get("cbore_depth", 0.0), 0.0) * scale,
        }

    if t == "hole_pattern":
        return {
            "type": "hole_pattern",
            "name": feature.id,
            "pattern": p.get("pattern", "rectangular"),
            "face": p.get("face", "top"),
            "diameter": _coerce_float(p.get("diameter", 5.0), 5.0) * scale,
            "depth": _scale_depth(p.get("depth", "through"), scale),
            "start_x": _coerce_float(p.get("start_x", 0.0), 0.0) * scale,
            "start_y": _coerce_float(p.get("start_y", 0.0), 0.0) * scale,
            "spacing_x": _coerce_float(p.get("spacing_x", 10.0), 10.0) * scale,
            "spacing_y": _coerce_float(p.get("spacing_y", 10.0), 10.0) * scale,
            "count_x": _coerce_int(p.get("count_x", 1), 1),
            "count_y": _coerce_int(p.get("count_y", 1), 1),
            "bolt_circle_diameter": _coerce_float(
                p.get("bolt_circle_diameter", 0.0), 0.0
            ) * scale,
            "count": _coerce_int(p.get("count", 0), 0),
        }

    if t == "cutout":
        return {
            "type": "cutout",
            "name": feature.id,
            "face": p.get("face", "top"),
            "profile": p.get("profile", "rectangle"),
            "center": [
                _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
            ],
            "length": _coerce_float(p.get("length", 10.0), 10.0) * scale,
            "width": _coerce_float(p.get("width", 10.0), 10.0) * scale,
            "depth": _scale_depth(p.get("depth", "through"), scale),
        }

    if t == "boss":
        return {
            "type": "boss",
            "name": feature.id,
            "face": p.get("face", "top"),
            "center": [
                _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
            ],
            "diameter": _coerce_float(p.get("diameter", 10.0), 10.0) * scale,
            "height": _coerce_float(p.get("height", 5.0), 5.0) * scale,
        }

    if t == "pad":
        return {
            "type": "pad",
            "name": feature.id,
            "face": p.get("face", "top"),
            "center": [
                _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
            ],
            "length": _coerce_float(p.get("length", 10.0), 10.0) * scale,
            "width": _coerce_float(p.get("width", 10.0), 10.0) * scale,
            "height": _coerce_float(p.get("height", 5.0), 5.0) * scale,
        }

    if t == "fillet":
        return {
            "type": "fillet",
            "name": feature.id,
            "radius": _coerce_float(p.get("radius", 1.0), 1.0) * scale,
            "edges": p.get("edges", "all"),
            "target": p.get("target"),
        }

    if t == "chamfer":
        return {
            "type": "chamfer",
            "name": feature.id,
            "size": _coerce_float(p.get("size", 1.0), 1.0) * scale,
            "edges": p.get("edges", "all"),
        }

    return None


def _scale_depth(depth: Any, scale: float) -> Any:
    """Preserve the literal 'through' string; scale numeric depths."""
    if isinstance(depth, str):
        return depth
    return _coerce_float(depth, 1.0) * scale


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class OnshapeDriver(CADDriver):
    """IGL driver that targets the Onshape REST API."""

    name = "onshape"

    def __init__(
        self,
        base_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("ONSHAPE_BASE_URL")
            or _ONSHAPE_DEFAULT_BASE_URL
        )
        self.access_key = access_key or os.environ.get("ONSHAPE_ACCESS_KEY", "")
        self.secret_key = secret_key or os.environ.get("ONSHAPE_SECRET_KEY", "")

    def get_description(self) -> str:
        return f"Onshape REST API ({self.base_url})"

    def is_available(self) -> bool:
        if not (self.access_key and self.secret_key):
            return False
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    def get_supported_features(self) -> list[str]:
        return list(_SUPPORTED_FEATURES)

    # --------------------------------------------------------------------- #

    def _http(self, method: str, path: str, body: Any = None, timeout: float = 30.0):
        """Make a signed HTTP call. Returns (status, payload_dict)."""
        import requests  # local import — only needed if is_available()

        url = f"{self.base_url}{path}"
        headers = _sign_request(method, url, self.access_key, self.secret_key)
        data = None if body is None else json.dumps(body)
        resp = requests.request(
            method, url, headers=headers, data=data, timeout=timeout
        )
        try:
            payload = resp.json() if resp.content else {}
        except Exception:  # noqa: BLE001
            payload = {"raw": resp.text}
        return resp.status_code, payload

    # --------------------------------------------------------------------- #

    def build_feature_list(self, doc: IGLDocument) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Build the full list of FeatureScript features for an IGL document.

        Returns (features, warnings). `warnings` collects any IGL features
        that this driver could not translate.
        """
        scale = igl_units_to_mm_scale(str(doc.part.units))
        features = list(_fs_stock(doc))
        warnings: list[str] = []

        for feature in doc.features:
            fs = _fs_feature(feature, scale)
            if fs is None:
                warnings.append(
                    f"feature {feature.id} type {feature.type!r} not supported by onshape driver"
                )
                continue
            features.append(fs)

        return features, warnings

    def _generate_impl(
        self,
        doc: IGLDocument,
        output_dir: Path,
    ) -> DriverResult:
        if not self.is_available():
            return DriverResult.failure(
                self.name,
                "Onshape credentials not set (ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY)",
            )

        features, warnings = self.build_feature_list(doc)

        # Persist the feature list for debugging/inspection even when the
        # network call fails.
        fs_json = output_dir / "onshape_features.json"
        fs_json.write_text(json.dumps(features, indent=2))

        try:
            doc_name = doc.part.name or f"igl_part_{uuid.uuid4().hex[:8]}"
            status, created = self._http(
                "POST", "/documents", body={"name": doc_name}
            )
            if status >= 400:
                return DriverResult(
                    success=False,
                    driver=self.name,
                    errors=[f"create document failed: HTTP {status} {created}"],
                    warnings=warnings,
                    metadata={"feature_script_path": str(fs_json)},
                )

            did = created.get("id", "")
            default_ws = (created.get("defaultWorkspace") or {}).get("id", "")
            if not did or not default_ws:
                return DriverResult(
                    success=False,
                    driver=self.name,
                    errors=["Onshape response missing document or workspace ID"],
                    warnings=warnings,
                )

            # Real Onshape integration would POST each feature via
            # /partstudios/d/{did}/w/{wid}/e/{eid}/features. For v1 we just
            # record the target URLs and return a partial success flag so the
            # fallback chain engages.
            partstudio_url = (
                f"/partstudios/d/{did}/w/{default_ws}/features"
            )
            return DriverResult(
                success=False,
                driver=self.name,
                errors=[
                    "Onshape driver: feature posting not yet implemented in v1. "
                    "Feature list written to onshape_features.json; fallback will engage."
                ],
                warnings=warnings,
                metadata={
                    "feature_script_path": str(fs_json),
                    "document_id": did,
                    "workspace_id": default_ws,
                    "partstudio_url": partstudio_url,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return DriverResult(
                success=False,
                driver=self.name,
                errors=[f"Onshape API error: {exc}"],
                warnings=warnings,
                metadata={"feature_script_path": str(fs_json)},
            )
