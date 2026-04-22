"""
Pub/sub event bus for the ARIA-OS pipeline.

Design (rewritten 2026-04-21 to fix late-subscriber event loss):

  - Events are APPENDED to a bounded ring buffer (last 500). They are NEVER
    drained — the buffer is read-only from consumers' perspectives.
  - Each consumer (SSE connection, dashboard tab, etc.) uses `subscribe()` to
    get a subscriber id and read cursor. On every `get_events(sub_id)` call
    the cursor advances, so each subscriber sees each event exactly once
    without blocking the others.
  - `get_history(n)` returns the most recent n events without touching any
    cursor — useful for "catching up" a fresh subscriber on context.
  - Thread-safe: a single `threading.Lock` guards the buffer + cursors.

Backward compatibility:

  - The legacy `get_events(timeout=...)` call (no sub_id) is preserved for
    the old single-reader path. Internally it just reads from a shared
    "default" subscriber cursor. This keeps `dashboard/aria_server.py`
    working without changes, while the new SSE generator gets to pass its
    own sub_id for clean per-connection semantics.
"""
from __future__ import annotations

import datetime
import threading
import uuid
from typing import Any

_BUFFER_MAX = 500

_lock = threading.Lock()
_buffer: list[dict] = []                  # ring: tail = newest
_total_emitted = 0                         # monotonic counter — every event gets one
_cursors: dict[str, int] = {"__default__": 0}


def _now_hms() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def emit(event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
    """Publish a pipeline event. Safe to call from any thread."""
    global _total_emitted
    ev = {
        "type": event_type,
        "timestamp": _now_hms(),
        "message": message,
        "data": data or {},
        "seq": 0,   # filled under lock
    }
    with _lock:
        _total_emitted += 1
        ev["seq"] = _total_emitted
        _buffer.append(ev)
        if len(_buffer) > _BUFFER_MAX:
            # Drop oldest. Keep cursors pointing at a valid range by
            # clamping any cursor < earliest-remaining seq.
            _buffer.pop(0)
        if _buffer:
            earliest = _buffer[0]["seq"] - 1
            for k, c in list(_cursors.items()):
                if c < earliest:
                    _cursors[k] = earliest


def subscribe() -> str:
    """Register a new subscriber. Returns an opaque sub_id. The cursor
    starts at the CURRENT tail, so the subscriber only sees events emitted
    AFTER this call. Use `get_history()` separately to catch up."""
    sid = uuid.uuid4().hex[:12]
    with _lock:
        _cursors[sid] = _total_emitted
    return sid


def unsubscribe(sub_id: str) -> None:
    with _lock:
        _cursors.pop(sub_id, None)


def get_history(n: int = 50) -> list[dict]:
    """Return the last n events in chronological order without advancing
    any cursor. Intended for connection-warmup."""
    with _lock:
        return list(_buffer[-n:])


def get_events(timeout: float = 0.1, sub_id: str | None = None) -> list[dict]:
    """Drain events since the subscriber's last read. `timeout` is kept for
    API compatibility with the old blocking call — this function does a
    short sleep if there's nothing new yet to mimic the old signature's
    back-pressure behavior.

    If `sub_id` is None we use a shared "__default__" cursor (legacy single-
    reader behavior). Modern SSE generators should call `subscribe()` once
    and pass the returned id on every call.
    """
    key = sub_id or "__default__"
    # Short initial wait — old callers pass ~0.5s; honour that budget once.
    import time
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        with _lock:
            cursor = _cursors.get(key, 0)
            new_events = [e for e in _buffer if e["seq"] > cursor]
            if new_events:
                _cursors[key] = new_events[-1]["seq"]
                return new_events
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.05)


def stats() -> dict:
    """For debugging — how many events and subscribers, how big the buffer is."""
    with _lock:
        return {
            "buffer_size": len(_buffer),
            "total_emitted": _total_emitted,
            "subscribers": len(_cursors),
            "oldest_seq": _buffer[0]["seq"] if _buffer else None,
            "newest_seq": _buffer[-1]["seq"] if _buffer else None,
        }
