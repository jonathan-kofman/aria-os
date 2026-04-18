"""
Content-addressed cache for expensive CAD operations.

The biggest universal perf win in this codebase is caching STEP→STL
conversion. STL conversion takes 0.5-2s per part and is invoked 3-4 times
per pipeline run (validator, slicer, render, drawings). With a cache keyed
on STEP file content hash, repeats are essentially free.

Cache location: outputs/.cache/  (gitignored)
Key: SHA-256 of the STEP file's bytes + the requested operation
Value: pointer to cached artifact + metadata

Usage:
    from aria_os.caching import cached_stl, cached_op

    # STL conversion with cache
    stl_path = cached_stl(step_path, tolerance=0.05)

    # Generic memoization for any expensive op
    @cached_op("validation")
    def validate_geometry(step_path, ...):
        ...
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

# In-memory hash cache to avoid re-hashing the same file repeatedly within
# a single process run. Cleared on process exit.
_HASH_CACHE: dict[str, str] = {}
_HASH_LOCK = threading.Lock()

# Default cache root (lazy-resolved so tests can override via env)
def _cache_root() -> Path:
    p = os.environ.get("ARIA_CACHE_DIR")
    if p:
        root = Path(p)
    else:
        root = Path(__file__).resolve().parent.parent / "outputs" / ".cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def file_hash(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of file bytes. Cached per-process by (path, mtime, size)."""
    path = str(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ""
    key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    with _HASH_LOCK:
        cached = _HASH_CACHE.get(key)
        if cached is not None:
            return cached
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    with _HASH_LOCK:
        _HASH_CACHE[key] = digest
    return digest


def cached_stl(
    step_path: str | Path,
    *,
    tolerance: float = 0.05,
    out_dir: str | Path | None = None,
) -> Path:
    """Convert STEP to STL with content-addressed caching.

    Same STEP + same tolerance returns the same cached STL path. STEP file
    edits invalidate the cache automatically (mtime + size change → re-hash).
    """
    step_path = Path(step_path)
    if not step_path.is_file():
        raise FileNotFoundError(f"STEP file not found: {step_path}")
    digest = file_hash(step_path)
    cache_dir = (Path(out_dir) if out_dir else _cache_root() / "stl")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{digest}_t{int(tolerance * 1000):04d}"
    cached_path = cache_dir / f"{cache_key}.stl"
    if cached_path.is_file() and cached_path.stat().st_size > 0:
        return cached_path
    # Cache miss — generate
    import cadquery as cq
    shape = cq.importers.importStep(str(step_path))
    cq.exporters.export(shape, str(cached_path), exportType="STL", tolerance=tolerance)
    return cached_path


def cached_op(namespace: str, *, key_args: tuple[int, ...] | None = None):
    """Decorator: cache function results in a namespace by hash of args.

    Args are JSON-serialized for the key. For arg-by-position selection, pass
    `key_args=(0, 1)` to only use the first 2 positional args as the cache key.
    Returns are JSON-serialized to disk; only JSON-able returns are supported.
    """
    cache_dir = _cache_root() / namespace
    cache_dir.mkdir(parents=True, exist_ok=True)

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Build a deterministic key from selected args + kwargs
            if key_args is not None:
                key_parts = [args[i] for i in key_args if i < len(args)]
            else:
                key_parts = list(args)
            # Hash file paths instead of including their full text
            key_serialized = []
            for p in key_parts:
                if isinstance(p, (str, Path)) and Path(str(p)).is_file():
                    key_serialized.append({"file_hash": file_hash(p)})
                else:
                    key_serialized.append(p)
            key_serialized.append(kwargs)
            key_str = json.dumps(key_serialized, sort_keys=True, default=str)
            digest = hashlib.sha256(key_str.encode()).hexdigest()[:16]
            cache_path = cache_dir / f"{digest}.json"
            if cache_path.is_file():
                try:
                    return json.loads(cache_path.read_text(encoding="utf-8"))
                except Exception:
                    pass  # corrupted cache — recompute
            result = fn(*args, **kwargs)
            try:
                cache_path.write_text(json.dumps(result, default=str), encoding="utf-8")
            except Exception:
                # Result not JSON-able — return without caching
                pass
            return result

        wrapper._cache_dir = cache_dir
        return wrapper
    return deco


def clear_cache(namespace: str | None = None) -> int:
    """Delete all entries in *namespace*, or the whole cache if namespace is None.

    Returns number of files deleted.
    """
    root = _cache_root()
    target = root / namespace if namespace else root
    if not target.is_dir():
        return 0
    n = sum(1 for _ in target.rglob("*") if _.is_file())
    shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    return n


def cache_stats() -> dict:
    """Return per-namespace file counts + total bytes."""
    root = _cache_root()
    stats = {}
    if not root.is_dir():
        return {"total_namespaces": 0, "total_files": 0, "total_bytes": 0}
    total_files = 0
    total_bytes = 0
    for ns_dir in root.iterdir():
        if not ns_dir.is_dir():
            continue
        files = list(ns_dir.rglob("*"))
        files = [f for f in files if f.is_file()]
        sz = sum(f.stat().st_size for f in files)
        stats[ns_dir.name] = {"files": len(files), "bytes": sz}
        total_files += len(files)
        total_bytes += sz
    stats["_total"] = {"files": total_files, "bytes": total_bytes}
    return stats
