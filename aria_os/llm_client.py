"""
aria_os/llm_client.py

Unified LLM client. Two priority chains depending on task type:

Code generation tasks (call_llm):
1. Anthropic Claude  — if ANTHROPIC_API_KEY is set (best code quality)
2. Google Gemini     — if GOOGLE_API_KEY is set (fast, good code gen)
3. Gemma 4 26B MoE   — if pulled in Ollama (strong local code gen, RAM-light)
4. Ollama default    — if Ollama is running (fallback local model)
5. Returns None      — caller falls back to heuristics

Non-code tasks (call_llm_local_first):
1. Gemma 4 26B MoE   — if pulled in Ollama (free, fast, good reasoning)
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
GEMMA_MODEL        — Gemma 4 model name override. If unset, the tag is
                     auto-selected from host RAM (see recommended_gemma_model):
                       >= 32 GB  → gemma4:31b   (dense, full quality)
                       >= 16 GB  → gemma4:26b   (MoE, RAM-light)
                       >=  8 GB  → gemma4:4b    (dense 4B)
                       >=  4 GB  → gemma4:1b    (tiny)
                        <  4 GB  → Gemma skipped entirely
OLLAMA_HOST        — Ollama base URL (default: http://localhost:11434)
OLLAMA_MODEL       — Model name for Ollama (default: qwen2.5-coder:7b)
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
_DEFAULT_GEMMA_MODEL   = "gemma4:4b"   # safe baseline when RAM detect fails

# RAM tiers for Gemma model auto-selection. First entry whose threshold
# is met (highest first) wins. Approximate Q4-quantized footprints:
# weights + KV cache + Ollama overhead. Below the smallest tier we skip
# Gemma entirely so we don't pull a model the host can't run.
_GEMMA_RAM_TIERS: tuple[tuple[float, str], ...] = (
    (32.0, "gemma4:31b"),   # dense, full quality
    (16.0, "gemma4:26b"),   # MoE, ~3.8B active params per token
    ( 8.0, "gemma4:4b"),    # dense 4B
    ( 4.0, "gemma4:1b"),    # tiny — fits 4GB RAM machines
)


def _total_ram_gb() -> float | None:
    """Total system RAM in GB. Pure stdlib; returns None if undetectable."""
    if os.name == "nt":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStatus()
            stat.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys / (1024 ** 3)
        except Exception:
            return None
        return None

    # Linux: sysconf is reliable. macOS lacks SC_PHYS_PAGES, falls through.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        pass

    # macOS: parse `sysctl hw.memsize`.
    try:
        import subprocess
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return int(out.stdout.strip()) / (1024 ** 3)
    except Exception:
        pass

    return None


def recommended_gemma_model() -> str | None:
    """Largest Gemma tag this host's RAM can run, or None if it can't run any.

    Pure RAM-based recommendation — does NOT consult GEMMA_MODEL env var.
    Use this from setup/install tooling to decide what to `ollama pull`.
    Use _gemma_model() from runtime code (it layers env override on top).

    When RAM detection fails, returns the smallest tier as a safe default
    rather than None — better to attempt Gemma and have Ollama fail than
    to silently skip on hosts where detection happens to be flaky.
    """
    ram = _total_ram_gb()
    if ram is None:
        return _DEFAULT_GEMMA_MODEL
    for threshold, tag in _GEMMA_RAM_TIERS:
        if ram >= threshold:
            return tag
    return None

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


def get_groq_key(repo_root: Path | None = None) -> str | None:
    """Return GROQ_API_KEY from env or .env file, or None if absent."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
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
                    if k.strip() == "GROQ_API_KEY":
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


def _cloud_only() -> bool:
    """
    True when Ollama-based providers should be skipped entirely.

    Set ARIA_CLOUD_ONLY=1 in production environments that have no local
    Ollama (e.g. Railway). Skipping the probe shaves 5-10s off every
    LLM-bound agent step that would otherwise wait for Ollama's HTTP
    connect to fail or for is_gemma_available()'s 2-second timeout.
    """
    val = os.environ.get("ARIA_CLOUD_ONLY", "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Per-process LLM call counter.
# Used by run_manifest.py to surface llm_calls in pipeline_stats. Each
# successful provider call increments its bucket. Resettable per run.
# ---------------------------------------------------------------------------

_LLM_CALL_COUNTS: dict[str, int] = {}


def llm_call_counts() -> dict[str, int]:
    """Return a snapshot of LLM call counts by provider."""
    return dict(_LLM_CALL_COUNTS)


def reset_llm_call_counts() -> None:
    """Reset call counts (call at the start of each pipeline run)."""
    _LLM_CALL_COUNTS.clear()


def _record_llm_call(provider: str) -> None:
    """Increment the call counter for a provider. Never raises."""
    try:
        _LLM_CALL_COUNTS[provider] = _LLM_CALL_COUNTS.get(provider, 0) + 1
    except Exception:
        pass


def _gemma_model() -> str:
    """Resolved Gemma tag for this host. Empty string means skip Gemma.

    Priority: explicit GEMMA_MODEL env/.env override → RAM-based recommendation
    → empty string when host RAM is below the smallest Gemma tier.
    """
    override = _read_env_var("GEMMA_MODEL", "")
    if override:
        return override
    return recommended_gemma_model() or ""


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
    """Check if a Gemma model fits this host's RAM AND is pulled in Ollama.

    Returns False without contacting Ollama when host RAM is below the
    smallest tier (4 GB). On a 16-32 GB host, Ollama pages MoE experts
    between VRAM and system RAM on demand — runs acceptably on small
    GPUs (e.g. RTX 1000 Ada, 6 GB).
    """
    model = _gemma_model()
    if not model:
        return False
    host = _ollama_host()
    model_base = model.split(":")[0]  # e.g. "gemma4" from "gemma4:26b"
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

def _try_anthropic(prompt: str, system: str, repo_root: "Path | None" = None,
                    *, model_tier: str = "premium") -> "str | None":
    """Try Anthropic API. Returns text response or None on any failure.

    `model_tier`:
      - "premium"   → claude-sonnet-4-6 (current default, highest cost)
      - "fast"      → claude-haiku-4-5 (5-10× cheaper, sufficient for
                       spec extraction, route decisions, short answers)
    Overloaded (529) retries are capped at 1 — Gemini fallback in the
    caller handles it faster than repeated Anthropic billing.
    """
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
        if model_tier == "fast":
            models = ("claude-haiku-4-5-20251001", "claude-3-5-haiku-20241022")
        else:
            models = ("claude-sonnet-4-6", "claude-3-5-sonnet-20241022")
        import time as _time
        for model in models:
            for _retry in range(1):  # no backoff retry — fall through to Gemini
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
                    _record_llm_call("anthropic")
                    return text
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if "model" in exc_str:
                        break  # try next model
                    if "overloaded" in exc_str or "529" in exc_str:
                        print(f"[LLM] anthropic overloaded, falling through")
                        return None  # let Gemini handle it
                    raise
    except Exception as exc:
        print(f"[LLM] anthropic failed: {exc}")
    return None


def _try_groq(prompt: str, system: str, repo_root: Path | None = None) -> str | None:
    """Try Groq's chat-completion API. Returns text or None on failure.

    Groq's free tier is generous (30 RPM / 14400/day on llama-3.3-70b-
    versatile), and the OpenAI-compatible API runs sub-second. We use
    the same model as the structured-output path so behaviour stays
    consistent across structured vs. free-text fallbacks."""
    api_key = get_groq_key(repo_root)
    if not api_key:
        return None
    try:
        from groq import Groq  # type: ignore
    except ImportError:
        return None
    try:
        client = Groq(api_key=api_key)
    except Exception:
        return None
    for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system or ""},
                    {"role": "user", "content": prompt},
                ],
                max_completion_tokens=4096,
                temperature=0.2,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "rate" in msg or "429" in msg or "quota" in msg:
                continue   # try smaller model
            if "decommissioned" in msg or "not_found" in msg or "not found" in msg:
                continue
            print(f"[LLM] groq/{model} failed: {exc}")
            return None
        text = response.choices[0].message.content or ""
        if text.strip():
            print(f"[LLM] groq/{model}")
            _record_llm_call("groq")
            return text
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
                    _record_llm_call("gemini")
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
            _record_llm_call("gemini")
            return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"[LLM] gemini (google-generativeai) failed: {exc}")

    return None


def _try_ollama(prompt: str, system: str) -> str | None:
    """Try Ollama local inference. Returns text response or None on any failure."""
    if _cloud_only():
        return None  # ARIA_CLOUD_ONLY=1 — skip local LLM probe
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
            if text:
                _record_llm_call("ollama")
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

    Gemma 4 26B MoE (Apache 2.0) is the default: ~3.8B active params per token,
    multimodal, configurable thinking mode. Runs locally on modest GPUs because
    Ollama pages experts between VRAM and system RAM. Pull with:
        ``ollama pull gemma4:26b``

    Uses the Ollama /api/chat endpoint targeting GEMMA_MODEL specifically,
    separate from OLLAMA_MODEL. Falls through gracefully if Gemma is not
    pulled or Ollama is down.

    If a Lightning AI session file exists at repo_root/.lightning_session,
    the tunnel is auto-reconnected before probing — kept for backward compat
    but no longer required.
    """
    if _cloud_only():
        return None  # ARIA_CLOUD_ONLY=1 — skip local LLM probe
    model = _gemma_model()
    if not model:
        ram = _total_ram_gb()
        ram_str = f"{ram:.1f} GB" if ram is not None else "unknown"
        print(f"[LLM] gemma skipped — host RAM ({ram_str}) below smallest Gemma tier (4 GB)")
        return None
    if not is_gemma_available():
        # Optional: auto-reconnect Lightning tunnel if a session file exists.
        # No-op when running purely locally.
        _ensure_lightning_tunnel()
        if not is_gemma_available():
            host = _ollama_host()
            print(f"[LLM] gemma skipped — model '{model}' not found in Ollama at {host} (try: ollama pull {model})")
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
            if text:
                _record_llm_call("gemma")
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
    if _cloud_only():
        return None  # ARIA_CLOUD_ONLY=1 — skip local LLM probe
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


# Dimension sanity bounds for common part types
_DIMENSION_SANITY_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "knob": {"diameter_mm": (15, 60), "height_mm": (10, 50)},
    "button": {"diameter_mm": (8, 40), "height_mm": (3, 30)},
    "lever": {"length_mm": (30, 150), "thickness_mm": (2, 15)},
    "bracket": {"width_mm": (20, 300), "height_mm": (20, 300), "depth_mm": (10, 100)},
    "bolt": {"diameter_mm": (2, 30), "length_mm": (10, 200)},
    "housing": {"width_mm": (30, 500), "height_mm": (30, 500), "depth_mm": (30, 500)},
    "shaft": {"diameter_mm": (2, 100), "length_mm": (20, 500)},
    "flange": {"diameter_mm": (20, 500), "thickness_mm": (2, 50)},
    "gear": {"diameter_mm": (15, 500), "thickness_mm": (5, 100)},
}


def _extract_part_type_from_goal(goal: str) -> str | None:
    """Extract a part type hint from goal string."""
    goal_lower = goal.lower()
    for part_type in _DIMENSION_SANITY_BOUNDS.keys():
        if part_type in goal_lower:
            return part_type
    return None


def _apply_dimension_sanity_check(
    goal: str,
    part_type: str | None = None,
) -> tuple[str, dict[str, float]]:
    """
    Check extracted dimensions against known bounds for the part type.
    Returns (corrected_goal, correction_log) where correction_log tracks what was adjusted.

    If LLM-extracted dimensions are implausibly large/small, re-prompt with anchors
    and return the corrected goal.
    """
    import re as _re

    if part_type is None:
        part_type = _extract_part_type_from_goal(goal)

    correction_log: dict[str, float] = {}

    if part_type not in _DIMENSION_SANITY_BOUNDS:
        return goal, correction_log  # no sanity check for unknown part types

    bounds = _DIMENSION_SANITY_BOUNDS[part_type]
    corrected_goal = goal
    has_violations = False

    # Scan goal for numeric dimensions matching the bounds keys
    for dim_name, (min_val, max_val) in bounds.items():
        # Pattern: "N*mm *{dim_name}" (e.g. "48mm diameter", "100mm height")
        pattern = rf'(\d+(?:\.\d+)?)\s*(?:mm)?\s+(?:dia(?:meter)?|{dim_name})'
        matches = _re.findall(pattern, goal, _re.IGNORECASE)

        for match in matches:
            val = float(match)
            if val < min_val or val > max_val:
                has_violations = True
                correction_log[dim_name] = val
                print(f"[DIM_SANITY] {dim_name}={val}mm out of bounds [{min_val}, {max_val}] for {part_type}")

    # If violations found, note them for the caller (but don't auto-correct here)
    # The LLM will be re-prompted in the pipeline with these anchors
    if has_violations:
        print(f"[DIM_SANITY] Original goal: {goal}")
        print(f"[DIM_SANITY] Violations: {correction_log}")

    return corrected_goal, correction_log


def _rebuild_goal_with_dimension_anchors(
    original_goal: str,
    correction_log: dict[str, float],
    part_type: str | None = None,
) -> str:
    """
    Rebuild the goal with explicit dimensional anchors for re-prompting the LLM.
    Used when dimension sanity check finds violations.
    """
    if not correction_log or part_type is None:
        return original_goal

    anchors = _DIMENSION_SANITY_BOUNDS.get(part_type, {})
    anchor_hints = []

    for dim_name, detected_val in correction_log.items():
        if dim_name in anchors:
            min_val, max_val = anchors[dim_name]
            mid_val = (min_val + max_val) / 2
            anchor_hints.append(
                f"{dim_name}: ~{mid_val:.0f}mm (typical range {min_val}–{max_val}mm for {part_type})"
            )

    if anchor_hints:
        anchor_str = "\n".join(anchor_hints)
        rebuilt = (
            f"Re-estimate: {original_goal}\n\n"
            f"This is a small {part_type}, likely:\n{anchor_str}\n\n"
            f"Revise the dimensions to be more realistic."
        )
        return rebuilt

    return original_goal


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

    Includes dimension sanity check: detects when LLM-extracted dimensions
    are implausibly large/small and logs violations for pipeline traceability.

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

    goal = None

    # 1. Try Gemini vision (primary)
    try:
        result = _try_gemini_vision(image_bytes, media_type, prompt, repo_root)
        if result:
            goal = result
    except Exception as exc:
        print(f"[IMAGE] gemini unexpected error: {exc}")

    # 2. Try Anthropic vision (fallback)
    if goal is None:
        try:
            result = _try_anthropic_vision(image_bytes, media_type, prompt, repo_root)
            if result:
                goal = result
        except Exception as exc:
            print(f"[IMAGE] anthropic unexpected error: {exc}")

    # 3. Try Ollama vision (llava / llava-llama3)
    if goal is None:
        try:
            result = _try_ollama_vision(image_bytes, media_type, prompt)
            if result:
                goal = result
        except Exception as exc:
            print(f"[IMAGE] ollama vision unexpected error: {exc}")

    if goal is None:
        print("[IMAGE] No vision backend available — provide a text description instead.")
        return None

    # Apply dimension sanity check
    corrected_goal, correction_log = _apply_dimension_sanity_check(goal)

    if correction_log:
        part_type = _extract_part_type_from_goal(goal)
        print(f"[DIM_SANITY] Dimension violations detected for {part_type}: {correction_log}")
        print(f"[DIM_SANITY] Original LLM goal: {goal}")
        print(f"[DIM_SANITY] Consider re-prompting with dimensional anchors.")

    return corrected_goal


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm(
    prompt: str,
    system: str = "",
    *,
    repo_root: Path | None = None,
    quality: str = "balanced",
) -> str | None:
    """
    Call the best available LLM backend with tier-based cost control.

    `quality` tiers (2026-04-20 rewrite to stop Anthropic credit bleed):

      - "fast"      : Gemini flash → Ollama (Gemma/qwen) → Claude Haiku
                       → None. Use for spec extraction, route decisions,
                       one-liners, classification — anywhere a premium
                       model is overkill. ~5-10× cheaper than premium.

      - "balanced"  : Gemini 2.0 flash → Gemma 4 26B MoE (local Ollama) →
                       Claude Sonnet → local Ollama default → None. DEFAULT.
                       Use for code generation where Gemini produces
                       acceptable CadQuery/Rhino code and Sonnet is
                       reserved for actual retries.

      - "premium"   : Claude Sonnet → Gemini → Gemma → Ollama → None.
                       Use ONLY when caller explicitly needs top-tier
                       quality (final CAD refinement, complex assembly
                       generation). Burns credits fast.

    Never raises. Returns None if all backends unavailable.
    """
    # Groq is the cost-conscious primary: 30 RPM free + sub-second
    # inference + tool_use support. Place it ahead of Gemini in
    # balanced/fast so a 50-prompt eval can run on free credits.
    if quality == "premium":
        chain = [
            (lambda: _try_anthropic(prompt, system, repo_root, model_tier="premium"),
             "anthropic/sonnet"),
            (lambda: _try_gemini(prompt, system, repo_root), "gemini"),
            (lambda: _try_groq(prompt, system, repo_root), "groq"),
            (lambda: _try_gemma(prompt, system), "gemma"),
            (lambda: _try_ollama(prompt, system), "ollama"),
        ]
    elif quality == "fast":
        chain = [
            (lambda: _try_groq(prompt, system, repo_root), "groq"),
            (lambda: _try_gemini(prompt, system, repo_root), "gemini"),
            (lambda: _try_gemma(prompt, system), "gemma"),
            (lambda: _try_ollama(prompt, system), "ollama"),
            (lambda: _try_anthropic(prompt, system, repo_root, model_tier="fast"),
             "anthropic/haiku"),
        ]
    else:  # balanced (default)
        chain = [
            (lambda: _try_groq(prompt, system, repo_root), "groq"),
            (lambda: _try_gemini(prompt, system, repo_root), "gemini"),
            (lambda: _try_gemma(prompt, system), "gemma"),
            (lambda: _try_anthropic(prompt, system, repo_root, model_tier="premium"),
             "anthropic/sonnet"),
            (lambda: _try_ollama(prompt, system), "ollama"),
        ]

    for fn, label in chain:
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as exc:
            print(f"[LLM] {label} unexpected error: {exc}")

    print(f"[LLM] no backend available (quality={quality}) — returning None")
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

    Priority: Gemma 4 26B MoE (Ollama) → Gemini Flash → Anthropic → Ollama default → None

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
    # 1. Try Gemma 4 26B MoE via Ollama (free, fast, good reasoning)
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
