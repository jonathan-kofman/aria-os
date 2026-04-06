"""
aria_os/zoo_bridge.py

Zoo.dev (KittyCAD) text-to-CAD integration for ARIA-OS.

Generates STEP files from natural-language descriptions via the Zoo.dev API.
Falls back gracefully when the kittycad SDK is not installed or ZOO_API_TOKEN
is not set.

Priority in the ARIA-OS pipeline:
  Template -> Zoo.dev -> Cloud LLM -> Deterministic fallback
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Auth helpers  (same .env-fallback pattern as llm_client.py)
# ---------------------------------------------------------------------------

def _get_zoo_token(repo_root: Path | None = None) -> str | None:
    """Return ZOO_API_TOKEN from env vars or .env file, or None."""
    token = os.environ.get("ZOO_API_TOKEN", "").strip()
    if token:
        return token
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    env_file = repo_root / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "ZOO_API_TOKEN":
                        val = v.strip().strip('"').strip("'")
                        return val or None
        except Exception:
            pass
    return None


def is_zoo_available(repo_root: Path | None = None) -> bool:
    """Return True if the kittycad SDK is installed AND ZOO_API_TOKEN is set."""
    try:
        import kittycad  # noqa: F401
    except ImportError:
        return False
    return bool(_get_zoo_token(repo_root))


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

def _goal_to_slug(goal: str) -> str:
    """Convert a goal string to a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", goal.lower().strip())
    slug = slug.strip("_")[:80]
    return slug or "zoo_part"


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------

def generate_step_from_zoo(
    goal: str,
    output_dir: str | Path,
    timeout: int = 120,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Generate a STEP file from Zoo.dev text-to-CAD API.

    Parameters
    ----------
    goal       : natural-language description of the part
    output_dir : directory for the STEP output (typically outputs/cad/step)
    timeout    : max seconds to wait for the API (default 120)
    repo_root  : project root (for .env lookup)

    Returns
    -------
    dict with keys:
        status     : "ok" | "error" | "unavailable"
        step_path  : path to generated STEP file (only if status == "ok")
        kcl_code   : generated KCL source code (only if status == "ok")
        duration_s : API execution time in seconds
        error      : error message (only if status == "error")
    """
    # ── Check SDK availability ──────────────────────────────────────────
    try:
        from kittycad import Client as ZooClient, MlAPI
        from kittycad import TextToCadCreateBody, FileExportFormat
    except ImportError:
        print("[Zoo] kittycad SDK not installed. Run: pip install kittycad")
        return {"status": "unavailable", "error": "kittycad SDK not installed"}

    # ── Check token ─────────────────────────────────────────────────────
    token = _get_zoo_token(repo_root)
    if not token:
        print("[Zoo] ZOO_API_TOKEN not set. Set it in .env or environment.")
        return {"status": "unavailable", "error": "ZOO_API_TOKEN not set"}

    # Ensure the token is available
    os.environ["ZOO_API_TOKEN"] = token
    os.environ["KITTYCAD_API_TOKEN"] = token

    # ── Prepare output paths ────────────────────────────────────────────
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    slug = _goal_to_slug(goal)
    step_path = output_dir / f"zoo_{slug}.step"

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    kcl_dir = repo_root / "outputs" / "cad" / "generated_code"
    kcl_dir.mkdir(parents=True, exist_ok=True)
    kcl_path = kcl_dir / f"zoo_{slug}.kcl"

    # ── Submit text-to-CAD request ──────────────────────────────────────
    print(f"[Zoo] Submitting: {goal!r}")
    t0 = time.time()

    try:
        client = ZooClient(token=token)
        ml = MlAPI(client)

        response = ml.create_text_to_cad(
            output_format=FileExportFormat.STEP,
            body=TextToCadCreateBody(prompt=goal),
        )

        if response is None:
            elapsed = time.time() - t0
            print("[Zoo] API returned None response.")
            return {"status": "error", "error": "API returned None", "duration_s": elapsed}

        # ── Poll until complete or timeout ──────────────────────────────
        poll_interval = 5  # seconds
        while not response.completed_at:
            elapsed = time.time() - t0
            if elapsed > timeout:
                print(f"[Zoo] Timed out after {timeout}s.")
                return {
                    "status": "error",
                    "error": f"Timed out after {timeout}s",
                    "duration_s": elapsed,
                }
            time.sleep(poll_interval)
            raw = ml.get_text_to_cad_part_for_user(id=response.id)
            response = raw.root if hasattr(raw, "root") else raw
            if response is None:
                elapsed = time.time() - t0
                print("[Zoo] Lost response during polling.")
                return {
                    "status": "error",
                    "error": "Lost response during polling",
                    "duration_s": elapsed,
                }

        elapsed = time.time() - t0

        # ── Check for API-level errors ──────────────────────────────────
        status_str = str(getattr(response, "status", "")).lower()
        if "fail" in status_str or "error" in status_str:
            error_msg = getattr(response, "error", status_str)
            print(f"[Zoo] API error: {error_msg}")
            return {"status": "error", "error": str(error_msg), "duration_s": elapsed}

        # ── Extract STEP output ─────────────────────────────────────────
        import base64

        outputs = response.outputs or {}
        step_saved = False

        for name, content in outputs.items():
            if name.lower().endswith(".step") or name.lower().endswith(".stp"):
                # Content may be base64-encoded bytes or raw bytes
                if isinstance(content, (bytes, bytearray)):
                    step_bytes = bytes(content)
                elif isinstance(content, str):
                    step_bytes = base64.b64decode(content)
                else:
                    # kittycad SDK may wrap in a special type
                    step_bytes = base64.b64decode(str(content))

                step_path.write_bytes(step_bytes)
                step_saved = True
                print(f"[Zoo] STEP saved: {step_path} ({len(step_bytes):,} bytes)")
                break

        if not step_saved:
            print(f"[Zoo] No STEP file in response. Output keys: {list(outputs.keys())}")
            return {
                "status": "error",
                "error": f"No STEP in outputs (keys: {list(outputs.keys())})",
                "duration_s": elapsed,
            }

        # ── Extract KCL code if present ─────────────────────────────────
        kcl_code = ""
        kcl_attr = getattr(response, "kcl", None)
        if kcl_attr:
            kcl_code = str(kcl_attr)
            kcl_path.write_text(kcl_code, encoding="utf-8")
            print(f"[Zoo] KCL saved: {kcl_path}")

        print(f"[Zoo] Done in {elapsed:.1f}s")
        return {
            "status": "ok",
            "step_path": str(step_path),
            "kcl_code": kcl_code,
            "duration_s": elapsed,
        }

    except Exception as exc:
        elapsed = time.time() - t0
        print(f"[Zoo] Error: {exc}")
        return {"status": "error", "error": str(exc), "duration_s": elapsed}
