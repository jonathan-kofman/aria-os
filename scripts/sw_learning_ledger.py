r"""sw_learning_ledger.py - persisted per-feature pass/fail history.

The ledger is the system's long-term memory of which SW features actually
work, which need workarounds, and which workaround is current. It accumulates
across runs so future planners can route around known-broken ops without
re-discovering the failure each time.

Schema (outputs/sw_learning_ledger.json):
{
  "<feature_key>": {
    "status": "ok" | "flaky" | "needs_workaround" | "unsupported",
    "workaround": "<short description>" | null,
    "first_seen": "<iso8601>",
    "last_pass": "<iso8601>" | null,
    "last_fail": "<iso8601>" | null,
    "pass_count": int,
    "fail_count": int,
    "common_errors": ["...", "..."],
    "successful_call_path": "<short description>" | null,
  }
}
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_PATH = (Path(__file__).resolve().parents[1]
               / "outputs" / "sw_learning_ledger.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict[str, dict]:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save(ledger: dict[str, dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def _entry_default(feature: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "workaround": None,
        "first_seen": _now(),
        "last_pass": None,
        "last_fail": None,
        "pass_count": 0,
        "fail_count": 0,
        "common_errors": [],
        "successful_call_path": None,
    }


def record_result(
    ledger: dict[str, dict],
    feature: str,
    *,
    passed: bool,
    error: str | None = None,
    call_path: str | None = None,
    workaround: str | None = None,
) -> None:
    """Update a single feature's ledger entry."""
    e = ledger.setdefault(feature, _entry_default(feature))
    now = _now()
    if passed:
        e["pass_count"] += 1
        e["last_pass"] = now
        if call_path:
            e["successful_call_path"] = call_path
        if workaround:
            e["workaround"] = workaround
    else:
        e["fail_count"] += 1
        e["last_fail"] = now
        if error:
            e["common_errors"] = (e.get("common_errors", []) + [error[:200]])[-10:]

    pc, fc = e["pass_count"], e["fail_count"]
    if pc and not fc:
        e["status"] = "ok"
    elif pc and fc:
        e["status"] = "flaky" if pc >= fc else "needs_workaround"
    elif fc and not pc:
        # All-fails: needs workaround unless we've explicitly marked unsupported
        if e["status"] != "unsupported":
            e["status"] = "needs_workaround"


def summary(ledger: dict[str, dict]) -> str:
    """Render a concise markdown summary of the ledger."""
    if not ledger:
        return "_(empty ledger)_\n"
    rows = sorted(ledger.items(), key=lambda kv: (kv[1]["status"], kv[0]))
    out = ["| feature | status | pass | fail | workaround | last_error |",
           "|---------|--------|------|------|------------|------------|"]
    for feat, e in rows:
        last_err = (e["common_errors"][-1] if e["common_errors"] else "")[:50]
        wa = (e.get("workaround") or "")[:40]
        out.append(f"| {feat} | {e['status']} | {e['pass_count']} | "
                   f"{e['fail_count']} | {wa} | {last_err} |")
    return "\n".join(out) + "\n"


def status_by_category(ledger: dict[str, dict]) -> dict[str, Counter]:
    """Group features by namespace prefix (sketch/feat/pattern/sm/surf/drw)."""
    groups: dict[str, Counter] = {}
    for feat, e in ledger.items():
        prefix = feat.split("_", 1)[0] if "_" in feat else "misc"
        groups.setdefault(prefix, Counter())[e["status"]] += 1
    return groups


if __name__ == "__main__":
    led = load()
    print(summary(led))
    print("\nBy category:")
    for cat, ctr in status_by_category(led).items():
        print(f"  {cat:10s}  {dict(ctr)}")
