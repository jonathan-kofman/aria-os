"""
Simple synchronous pub/sub event bus for the ARIA-OS pipeline.
Pipeline components call emit() — sync, no asyncio required.
The SSE endpoint in aria_server.py polls the queue.
"""
import queue
import datetime
import json
from typing import Any

_queue: queue.Queue = queue.Queue(maxsize=500)


def emit(event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
    """
    Publish a pipeline event. Safe to call from any thread or context.

    event_type: step | tool_call | llm_output | validation | cem | error | complete | grasshopper
    """
    try:
        _queue.put_nowait({
            "type": event_type,
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "data": data or {},
        })
    except queue.Full:
        pass  # never crash the pipeline


def get_events(timeout: float = 0.1) -> list[dict]:
    """Drain all available events, blocking up to timeout for the first one."""
    events = []
    try:
        events.append(_queue.get(timeout=timeout))
        while not _queue.empty():
            events.append(_queue.get_nowait())
    except queue.Empty:
        pass
    return events
