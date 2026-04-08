"""
MillForge submission retry queue.

When the circuit breaker is OPEN, failed ARIA → MillForge job submissions
are written to a JSONL file here. On each new coordinator run (and when
the circuit recovers), `drain()` retries queued submissions.

File: outputs/millforge_retry_queue.jsonl
Max retries per job: 3
Max age: 24 hours (older entries are pruned on drain)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

_QUEUE_PATH = Path(__file__).parent.parent.parent / "outputs" / "millforge_retry_queue.jsonl"
_MAX_RETRIES = 3
_MAX_AGE_S = 86_400  # 24 hours


def enqueue(job_payload: dict, aria_job_id: str, error: str = "") -> None:
    """Append a failed submission to the retry queue. Thread-safe via append mode."""
    entry = {
        "aria_job_id": aria_job_id,
        "payload": job_payload,
        "queued_at": time.time(),
        "attempts": 0,
        "last_error": error,
    }
    try:
        _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _QUEUE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        logger.info("Queued ARIA job '%s' for retry.", aria_job_id)
    except Exception as exc:
        logger.warning("Failed to enqueue job '%s': %s", aria_job_id, exc)


async def drain(
    submit_fn: Callable[[dict], Awaitable[Optional[dict]]],
    circuit_is_open_fn: Callable[[], bool],
) -> dict:
    """
    Attempt to resubmit queued jobs.

    submit_fn   — async callable that takes a payload dict and returns
                  the MillForge ack response or None on failure.
    circuit_is_open_fn — callable returning True if circuit is still OPEN.

    Returns a summary dict: {processed, succeeded, requeued, pruned}.
    """
    if not _QUEUE_PATH.exists():
        return {"processed": 0, "succeeded": 0, "requeued": 0, "pruned": 0}

    try:
        lines = _QUEUE_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {"processed": 0, "succeeded": 0, "requeued": 0, "pruned": 0}

    now = time.time()
    remaining: list[dict] = []
    stats = {"processed": 0, "succeeded": 0, "requeued": 0, "pruned": 0}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Prune expired entries
        age = now - entry.get("queued_at", 0)
        if age > _MAX_AGE_S:
            stats["pruned"] += 1
            logger.debug("Pruned expired retry entry for '%s'.", entry.get("aria_job_id"))
            continue

        # Prune exhausted entries
        if entry.get("attempts", 0) >= _MAX_RETRIES:
            stats["pruned"] += 1
            logger.warning(
                "Dropping '%s' after %d failed attempts.",
                entry.get("aria_job_id"), entry.get("attempts"),
            )
            continue

        # If circuit is still open, keep without retrying
        if circuit_is_open_fn():
            remaining.append(entry)
            continue

        stats["processed"] += 1
        entry["attempts"] = entry.get("attempts", 0) + 1

        try:
            result = await submit_fn(entry["payload"])
            if result:
                stats["succeeded"] += 1
                logger.info(
                    "Retry succeeded for '%s' on attempt %d → MillForge job #%s",
                    entry.get("aria_job_id"),
                    entry["attempts"],
                    result.get("millforge_job_id", "?"),
                )
                # Don't re-add to remaining — job is done
            else:
                entry["last_error"] = "submit_fn returned None"
                remaining.append(entry)
                stats["requeued"] += 1
        except Exception as exc:
            entry["last_error"] = str(exc)
            remaining.append(entry)
            stats["requeued"] += 1
            logger.warning(
                "Retry failed for '%s' (attempt %d/%d): %s",
                entry.get("aria_job_id"), entry["attempts"], _MAX_RETRIES, exc,
            )

    # Rewrite the queue file with only remaining entries
    try:
        if remaining:
            _QUEUE_PATH.write_text(
                "\n".join(json.dumps(e, default=str) for e in remaining) + "\n",
                encoding="utf-8",
            )
        else:
            _QUEUE_PATH.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Failed to rewrite retry queue: %s", exc)

    if stats["processed"] or stats["pruned"]:
        logger.info(
            "Retry queue drain: processed=%d succeeded=%d requeued=%d pruned=%d",
            stats["processed"], stats["succeeded"], stats["requeued"], stats["pruned"],
        )
    return stats


def queue_depth() -> int:
    """Return the number of entries currently in the queue."""
    if not _QUEUE_PATH.exists():
        return 0
    try:
        return sum(1 for line in _QUEUE_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0
