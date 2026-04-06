"""
aria_os/llm_client.py

Unified LLM client. Two priority chains depending on task type:

Code generation tasks (call_llm):
1. Anthropic Claude  — if ANTHROPIC_API_KEY is set (best code quality)
2. Google Gemini     — if GOOGLE_API_KEY is set (fast, good code gen)
3. Gemma 4 31B      — if pulled in Ollama (strong local code gen)
4. Ollama default    — if Ollama is running (fallback local model)
5. Returns None      — caller falls back to heuristics

Non-code tasks (call_llm_local_first):
1. Gemma 4 31B      — if pulled in Ollama (free, fast, good reasoning)
2. Google Gemini     — if GOOGLE_API_KEY is set
3. Anthropic Claude  — if ANTHROPIC_API_KEY is set
4. Ollama default    — if Ollama is running
5. Returns None

Never raises. Logs which backend was used on every call.

Environment variables
---------------------
GOOGLE_API_KEY     — Google Gemini API key (optional; enables Gemini backend)
ANTHROPIC_API_KEY  — Anthropic API key (optional; enables Anthropic backend)
GEMINI_MODEL       — Gemini model name (default: gemini-2.0-flash)
GEMMA_MODEL        — Gemma 4 model name for Ollama (default: gemma4:31b)
OLLAMA_HOST        — Ollama base URL (default: http://localhost:11434)
OLLAMA_MODEL       — Model name for Ollama (default: deepseek-coder)
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_OLLAMA_HOST   = "http://localhost:11434"
_DEFAULT_OLLAMA_MODEL  = "qwen2.5-coder:7b"
_DEFAULT_GEMINI_MODEL  = "gemini-2.0-flash"
_DEFAULT_GEMMA_MODEL   = "gemma4:31b"

# Note injected into system prompt when using a local model
_LOCAL_MODEL_NOTE = (
    "\n\nNOTE: You are running as a local model. "
    "Prefer conservative, well-tested RhinoCommon/CadQuery patterns. "
    "Avoid creative or experimental geometry approaches. "
    "Use only documented API methods; do not invent method names."
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_anthropic_key(repo_root: Path | None = None) -> str | None:
    """Return ANTHROPIC_API_KEY from env or .env file, or None if absent."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
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
                    if k.strip() == "ANTHROPIC_API_KEY":
                        val = v.strip().strip('"').strip("'")
                        return val or None
        except Exception:
            pass
    return None


def _read_env_var(key: str, default: str, repo_root: Path | None = None) -> str:
    """Read an env var from os.environ first, then .env file, then return default."""
    val = os.environ.get(key, "").strip()
    if val:
        return val
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
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'") or default
        except Exception:
            pass
    return default


def get_google_key(repo_root: Path | None = None) -> str | None:
    """Return GOOGLE_API_KEY from env or .env file, or None if absent."""
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if key:
        return key
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
                    if k.strip() == "GOOGLE_API_KEY":
                        val = v.strip().strip('"').strip("'")
                        return val or None
        except Exception:
            pass
    return None


def _gemini_model(repo_root: Path | None = None) -> str:
    return _read_env_var("GEMINI_MODEL", _DEFAULT_GEMINI_MODEL, repo_root)


def _ollama_host() -> str:
    return _read_env_var("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST).rstrip("/")


def _ollama_model() -> str:
    return _read_env_var("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)


def _gemma_model() -> str:
    return _read_env_var("GEMMA_MODEL", _DEFAULT_GEMMA_MODEL)


def _ensure_lightning_tunnel() -> None:
    """Auto-reconnect the Lightning AI SSH tunnel if it's down.

    Reads the session ID from .lightning_session file and re-establishes
    the SSH tunnel to the remote GPU. No-op if tunnel is already alive
    or if no session file exists.
    """
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    session_file = repo_root / ".lightning_session"
    key_file = Path.home() / ".ssh" / "lightning_rsa"

    if not session_file.exists() or not key_file.exists():
        return

    # Check if tunnel is already alive
    host = _ollama_host()
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            json.loads(resp.read())
        return  # tunnel is fine
    except Exception:
        pass

    # Read session and reconnect
    session = session_file.read_text().strip()
    if not session:
        return

    print(f"[LLM] Reconnecting Lightning AI tunnel (session: {session[:16]}...)")
    try:
        # Parse port from OLLAMA_HOST
        port = 11435
        if ":" in host.rsplit(":", 1)[-1]:
            try:
                port = int(host.rsplit(":", 1)[-1])
            except ValueError:
                pass

        subprocess.run([
            "ssh", "-i", str(key_file),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-N", "-f",
            "-L", f"{port}:localhost:11434",
            f"s_{session}@ssh.lightning.ai",
        ], capture_output=True, timeout=15)

        import time
        time.sleep(2)
        # Verify
        try:
            req = urllib.request.Request(f"{host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                json.loads(resp.read())
            print("[LLM] Lightning AI tunnel reconnected")
        except Exception:
            print("[LLM] Lightning AI tunnel failed to reconnect")
    except Exception as exc:
        print(f"[LLM] Lightning AI reconnect error: {exc}")


def is_gemma_available() -> bool:
    """Check if Gemma 4 is pulled in Ollama.

    Queries Ollama's /api/tags endpoint and checks if any pulled model
    matches the configured GEMMA_MODEL (default: gemma4:31b).
    Returns False if Ollama is not running or Gemma 4 is not pulled.
    """
    host = _ollama_host()
    model = _gemma_model()
    model_base = model.split(":")[0]  # e.g. "gemma4" from "gemma4:31b"
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        return any(model_base in m for m in models)
    except Exception:
        return False


def get_ollama_status() -> dict[str, Any]:
    """Check Ollama availability and loaded models. Returns dict for /api/health."""
    host = _ollama_host()
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        active_model = _ollama_model()
        loaded = any(active_model in m for m in models) if models else False
        return {
            "available": True,
            "host": host,
            "model": active_model,
            "model_loaded": loaded,
            "all_models": models,
        }
    except Exception as exc:
        return {
            "available": False,
            "host": host,
            "model": _ollama_model(),
            "reason": str(exc),
        }


# ---------------------------------------------------------------------------
# Internal backends
# ---------------------------------------------------------------------------

def _try_anthropic(prompt: str, system: str, repo_root: "Path | None" = None) -> "str | None":
    """Try Anthropic API. Returns text response or None on any failure."""
    api_key = get_anthropic_key(repo_root)
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None
    try:
        from . import event_bus  # noqa: F401 — optional
        client = anthropic.Anthropic(api_key=api_key)
        kwargs: dict[str, Any] = {
            "max_tokens": 4096,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        # Try current model, fall back to older on model-not-found errors
        import time as _time
        for model in ("claude-sonnet-4-6", "claude-3-5-sonnet-20241022"):
            for _retry in range(3):
                try:
                    msg = client.messages.create(model=model, **kwargs)
                    text = "".join(
                        b.text for b in msg.content if hasattr(b, "text")
                    )
                    try:
                        event_bus.emit(
                            "llm_output",
                            f"[LLM] anthropic/{model}",
                            {"backend": "anthropic", "model": model},
                        )
                    except Exception:
                        pass
                    print(f"[LLM] anthropic/{model}")
                    return text
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if "model" in exc_str:
                        break  # try next model
                    if "overloaded" in exc_str or "529" in exc_str:
                        wait = 5 * (2 ** _retry)  # 5s, 10s, 20s
                        print(f"[LLM] anthropic overloaded, retry in {wait}s...")
                        _time.sleep(wait)
                        continue
                    raise
    except Exception as exc:
        print(f"[LLM] anthropic failed: {exc}")
    return None


def _try_gemini(prompt: str, system: str, repo_root: Path | None = None) -> str | None:
    """Try Google Gemini API. Returns text response or None on any failure.

    Tries google-genai (new SDK) first, falls back to google-generativeai (legacy).
    """
    api_key = get_google_key(repo_root)
    if not api_key:
        return None
    model_name = _gemini_model(repo_root)

    # --- Attempt 1: new google-genai SDK (pip install google-genai) -----------
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        client = genai.Client(api_key=api_key)
        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=0.0,
            max_output_tokens=4096,
        )
        # Try preferred model, then progressively lighter/cheaper models
        for try_model in (model_name, "gemini-2.0-flash-lite", "gemini-2.5-flash"):
            try:
                response = client.models.generate_content(
                    model=try_model,
                    contents=prompt,
                    config=cfg,
                )
                text = response.text or ""
                if text:
                    print(f"[LLM] gemini/{try_model}")
                    return text
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    if try_model != "gemini-2.5-flash":
                        continue  # try next fallback model
                    print(f"[LLM] gemini quota exhausted — billing may need enabling")
                    return None
                raise
    except ImportError:
        pass  # try legacy SDK below
    except Exception as exc:
        print(f"[LLM] gemini (google-genai) failed: {exc}")
        return None

    # --- Attempt 2: legacy google-generativeai SDK ---------------------------
    try:
        import google.generativeai as genai_legacy  # type: ignore
        genai_legacy.configure(api_key=api_key)
        gen_model = genai_legacy.GenerativeModel(
            model_name=model_name,
            system_instruction=system or None,
        )
        response = gen_model.generate_content(
            prompt,
            generation_config={"temperature": 0.0, "max_output_tokens": 4096},
        )
        text = response.text or ""
        if text:
            print(f"[LLM] gemini/{model_name} (legacy sdk)")
            return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"[LLM] gemini (google-generativeai) failed: {exc}")

    return None


def _try_ollama(prompt: str, system: str) -> str | None:
    """Try Ollama local inference. Returns text response or None on any failure."""
    host = _ollama_host()
    model = _ollama_model()
    # Inject local-model note into system prompt
    effective_system = (system + _LOCAL_MODEL_NOTE) if system else _LOCAL_MODEL_NOTE.strip()
    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode("utf-8")

    for attempt in range(2):  # retry once on transient 500 (OOM/crash)
        try:
            req = urllib.request.Request(
                f"{host}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("message", {}).get("content", "")
            print(f"[LLM] ollama/{model}")
            return text if text else None
        except urllib.error.HTTPError as exc:
            if exc.code == 500 and attempt == 0:
                print(f"[LLM] ollama HTTP 500 (likely OOM) — retrying once...")
                continue
            print(f"[LLM] ollama failed: HTTP {exc.code}")
            return None
        except Exception as exc:
            print(f"[LLM] ollama failed: {exc}")
            return None
    return None


def _try_gemma(prompt: str, system: str) -> str | None:
    """Try Gemma 4 via Ollama. Returns text or None.

    Gemma 4 31B (Apache 2.0) is a strong local model for both code generation
    and reasoning tasks. It runs via Ollama: ``ollama pull gemma4:31b``

    Uses the same Ollama /api/chat endpoint but targets the Gemma model
    specifically, separate from the default Ollama model configuration.
    Falls through gracefully if Gemma 4 is not pulled or Ollama is down.
    """
    if not is_gemma_available():
        # Try auto-reconnecting the Lightning AI tunnel
        _ensure_lightning_tunnel()
        if not is_gemma_available():
            return None

    host = _ollama_host()
    model = _gemma_model()

    # Inject local-model note into system prompt
    effective_system = (system + _LOCAL_MODEL_NOTE) if system else _LOCAL_MODEL_NOTE.strip()
    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode("utf-8")

    for attempt in range(2):  # retry once on transient 500 (OOM/crash)
        try:
            req = urllib.request.Request(
                f"{host}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("message", {}).get("content", "")
            print(f"[LLM] gemma/{model}")
            return text if text else None
        except urllib.error.HTTPError as exc:
            if exc.code == 500 and attempt == 0:
                print(f"[LLM] gemma HTTP 500 (likely OOM) — retrying once...")
                continue
            print(f"[LLM] gemma failed: HTTP {exc.code}")
            return None
        except Exception as exc:
            print(f"[LLM] gemma failed: {exc}")
            return None
    return None


# ---------------------------------------------------------------------------
# Image analysis
# ---------------------------------------------------------------------------

_IMAGE_ANALYSIS_SYSTEM = """\
You are a mechanical engineering analyst helping an AI-driven CAD pipeline.
The user will show you a photo of a physical part or assembly.

Your task: extract a precise, single-paragraph goal description that can be
fed directly into the CAD pipeline to recreate this part.

Rules:
- Estimate visible dimensions in mm (use context clues: standard bolt sizes,
  hand scale, grid markings, ruler if present).  If truly unknown, omit the dim.
- Identify the part type (bracket, housing, pulley, flange, etc.).
- List key features: holes, slots, ribs, threads, chamfers, wall thickness.
- Specify material if obvious from colour/finish (aluminium, steel, PLA, etc.).
- Output ONLY the goal string — no preamble, no explanation, no JSON.
  Example output: "aluminium bracket 120x60x5mm with 4x M6 mounting holes on
  80mm bolt circle and a central 30mm bore"
"""

_IMAGE_MIMETYPES = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def _try_anthropic_vision(
    image_bytes: bytes,
    media_type: str,
    prompt: str,
    repo_root: "Path | None" = None,
) -> "str | None":
    """Try Anthropic vision API. Returns goal string or None."""
    api_key = get_anthropic_key(repo_root)
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore
        import base64 as _b64
    except ImportError:
        return None
    image_data = _b64.standard_b64encode(image_bytes).decode("ascii")
    client = anthropic.Anthropic(api_key=api_key)
    for model in ("claude-sonnet-4-6", "claude-3-5-sonnet-20241022"):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                system=_IMAGE_ANALYSIS_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
            if text:
                print(f"[IMAGE] anthropic/{model}: {text[:120]}{'...' if len(text) > 120 else ''}")
                return text
        except Exception as exc:
            if "model" in str(exc).lower():
                continue
            print(f"[IMAGE] anthropic vision failed ({model}): {exc}")
            return None
    return None


def _try_gemini_vision(
    image_bytes: bytes,
    media_type: str,
    prompt: str,
    repo_root: "Path | None" = None,
) -> "str | None":
    """Try Google Gemini vision API. Returns goal string or None."""
    api_key = get_google_key(repo_root)
    if not api_key:
        return None
    model_name = _gemini_model(repo_root)

    # --- Attempt 1: new google-genai SDK -----------------------------------
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        client = genai.Client(api_key=api_key)
        cfg = types.GenerateContentConfig(
            system_instruction=_IMAGE_ANALYSIS_SYSTEM,
            temperature=0.0,
            max_output_tokens=512,
        )
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=media_type)
        for try_model in (model_name, "gemini-2.0-flash", "gemini-2.0-flash-lite"):
            try:
                response = client.models.generate_content(
                    model=try_model,
                    contents=[image_part, prompt],
                    config=cfg,
                )
                text = (response.text or "").strip()
                if text:
                    print(f"[IMAGE] gemini/{try_model}: {text[:120]}{'...' if len(text) > 120 else ''}")
                    return text
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    if try_model != "gemini-2.0-flash-lite":
                        continue
                    print("[IMAGE] gemini quota exhausted")
                    return None
                if "model" in err_str.lower():
                    continue
                raise
    except ImportError:
        pass
    except Exception as exc:
        print(f"[IMAGE] gemini vision (google-genai) failed: {exc}")
        return None

    # --- Attempt 2: legacy google-generativeai SDK -------------------------
    try:
        import google.generativeai as genai_legacy  # type: ignore
        import PIL.Image as _PIL  # type: ignore
        import io as _io
        genai_legacy.configure(api_key=api_key)
        gen_model = genai_legacy.GenerativeModel(
            model_name=model_name,
            system_instruction=_IMAGE_ANALYSIS_SYSTEM,
        )
        pil_image = _PIL.open(_io.BytesIO(image_bytes))
        response = gen_model.generate_content(
            [pil_image, prompt],
            generation_config={"temperature": 0.0, "max_output_tokens": 512},
        )
        text = (response.text or "").strip()
        if text:
            print(f"[IMAGE] gemini/{model_name} (legacy sdk): {text[:120]}{'...' if len(text) > 120 else ''}")
            return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"[IMAGE] gemini vision (legacy sdk) failed: {exc}")

    return None


def _try_ollama_vision(
    image_bytes: bytes,
    media_type: str,
    prompt: str,
) -> "str | None":
    """
    Try Ollama vision inference using a multimodal model (llava, llava-llama3, etc.).
    Ollama supports images via the 'images' field in the message payload (base64).
    Auto-detects the best available vision model; falls back to the configured model.
    Returns goal string or None on any failure.
    """
    host  = _ollama_host()
    b64   = base64.b64encode(image_bytes).decode("utf-8")

    # Prefer dedicated vision models; fall back to whatever is configured
    vision_candidates = ["llava-llama3", "llava:13b", "llava", "llava:7b", _ollama_model()]

    # Ask Ollama which models are available
    available: list[str] = []
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        available = [m["name"].split(":")[0] for m in data.get("models", [])]
    except Exception:
        pass  # can't list models — just try in order

    model = next(
        (c for c in vision_candidates
         if not available or any(c.startswith(a) or a.startswith(c.split(":")[0])
                                 for a in available)),
        vision_candidates[-1],
    )

    payload = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [b64],
        }],
        "stream": False,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{host}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("message", {}).get("content", "").strip()
        if text:
            print(f"[IMAGE] ollama/{model} vision")
            return text
        return None
    except urllib.error.HTTPError as exc:
        print(f"[IMAGE] ollama vision failed: HTTP {exc.code} (is {model} pulled?)")
        return None
    except Exception as exc:
        print(f"[IMAGE] ollama vision failed: {exc}")
        return None


def analyze_image_for_cad(
    image_path: "str | Path",
    hint: str = "",
    *,
    repo_root: "Path | None" = None,
) -> "str | None":
    """
    Use vision AI to analyse a photo and return a CAD goal string.

    Priority: Gemini → Anthropic (fallback) → Ollama (llava) → None

    Gemini is tried first to conserve Anthropic quota.

    Parameters
    ----------
    image_path : path to the image file (jpg / png / gif / webp)
    hint       : optional free-text hint from the user (e.g. "it's a bracket")
    repo_root  : repo root for .env key lookup

    Returns
    -------
    A goal string suitable for aria_os.run(), or None if vision unavailable.
    """
    from pathlib import Path as _Path

    image_path = _Path(image_path)
    if not image_path.exists():
        print(f"[IMAGE] File not found: {image_path}")
        return None

    suffix = image_path.suffix.lower()
    media_type = _IMAGE_MIMETYPES.get(suffix, "image/jpeg")
    image_bytes = image_path.read_bytes()

    prompt = _IMAGE_ANALYSIS_SYSTEM + "\n\nAnalyse this part and produce a CAD goal description."
    if hint:
        prompt += f"\n\nUser hint: {hint}"

    # 1. Try Gemini vision (primary)
    try:
        result = _try_gemini_vision(image_bytes, media_type, prompt, repo_root)
        if result:
            return result
    except Exception as exc:
        print(f"[IMAGE] gemini unexpected error: {exc}")

    # 2. Try Anthropic vision (fallback)
    try:
        result = _try_anthropic_vision(image_bytes, media_type, prompt, repo_root)
        if result:
            return result
    except Exception as exc:
        print(f"[IMAGE] anthropic unexpected error: {exc}")

    # 3. Try Ollama vision (llava / llava-llama3)
    try:
        result = _try_ollama_vision(image_bytes, media_type, prompt)
        if result:
            return result
    except Exception as exc:
        print(f"[IMAGE] ollama vision unexpected error: {exc}")

    print("[IMAGE] No vision backend available — provide a text description instead.")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm(
    prompt: str,
    system: str = "",
    *,
    repo_root: Path | None = None,
) -> str | None:
    """
    Call the best available LLM backend for code generation tasks.

    Priority: Anthropic Claude → Gemini 2.5 Flash → Gemma 4 31B (Ollama) → Ollama default → None

    Cloud models are tried first for code generation quality.

    Parameters
    ----------
    prompt     : user message / prompt
    system     : system prompt (optional)
    repo_root  : repo root for .env lookup (optional)

    Returns
    -------
    Response text, or None if all backends unavailable.
    Never raises.
    """
    # 1. Try Anthropic (best code quality)
    try:
        result = _try_anthropic(prompt, system, repo_root)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] anthropic unexpected error: {exc}")

    # 2. Try Gemini (fast, good code gen)
    try:
        result = _try_gemini(prompt, system, repo_root)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] gemini unexpected error: {exc}")

    # 3. Try Gemma 4 31B via Ollama (strong local code gen)
    try:
        result = _try_gemma(prompt, system)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] gemma unexpected error: {exc}")

    # 4. Try Ollama default model
    try:
        result = _try_ollama(prompt, system)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] ollama unexpected error: {exc}")

    # 5. All backends down
    print("[LLM] no backend available — returning None")
    return None


def call_llm_local_first(
    prompt: str,
    system: str = "",
    *,
    repo_root: Path | None = None,
) -> str | None:
    """
    Call the best available LLM backend, preferring local models.

    For non-code tasks (spec extraction, routing, refinement) where local
    models are adequate and free. Uses expensive cloud models only as fallback.

    Priority: Gemma 4 31B (Ollama) → Gemini Flash → Anthropic → Ollama default → None

    Parameters
    ----------
    prompt     : user message / prompt
    system     : system prompt (optional)
    repo_root  : repo root for .env lookup (optional)

    Returns
    -------
    Response text, or None if all backends unavailable.
    Never raises.
    """
    # 1. Try Gemma 4 31B via Ollama (free, fast, good reasoning)
    try:
        result = _try_gemma(prompt, system)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] gemma unexpected error: {exc}")

    # 2. Try Gemini (free/cheap quota)
    try:
        result = _try_gemini(prompt, system, repo_root)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] gemini unexpected error: {exc}")

    # 3. Try Anthropic (fallback — preserves paid quota)
    try:
        result = _try_anthropic(prompt, system, repo_root)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] anthropic unexpected error: {exc}")

    # 4. Try Ollama default model
    try:
        result = _try_ollama(prompt, system)
        if result is not None:
            return result
    except Exception as exc:
        print(f"[LLM] ollama unexpected error: {exc}")

    # 5. All backends down
    print("[LLM] no backend available — returning None")
    return None
