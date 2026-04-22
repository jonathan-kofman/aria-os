"""Onshape REST client — direct access via API keys (no OAuth needed).

Uses the access/secret key pair from `.env` (ONSHAPE_ACCESS_KEY +
ONSHAPE_SECRET_KEY) to POST feature operations into Onshape Part
Studios from ARIA's backend.
"""
from .client import OnshapeClient, get_client
from .executor import OnshapeExecutor

__all__ = ["OnshapeClient", "get_client", "OnshapeExecutor"]
