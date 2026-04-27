"""Cross-CAD parity test harness (rec #13).

Drives the SAME /op contract against each CAD's HTTP listener and
asserts the responses are structurally equivalent — so drift between
SW (7501), Rhino (7502), Fusion (7503), Onshape (7504) gets caught
BEFORE it lands in production via the orchestrator.

Today only SW + KiCad ship a stable HTTP listener. The harness
gracefully skips a CAD whose port doesn't answer rather than failing
the run — the scoreboard tells you which CADs are reachable + which
are passing, and we add new ops to the matrix as their bridges grow.

Usage:
    python scripts/cross_cad_parity.py
    python scripts/cross_cad_parity.py --json out.json
    python scripts/cross_cad_parity.py --only solidworks rhino

Exit code 0 = all reachable CADs passed, 1 = at least one failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Each CAD bridge's HTTP listener. Add an entry when a new CAD comes
# online. Default ports match the addin/plugin source.
CADS: dict[str, int] = {
    "solidworks": 7501,
    "rhino":      7502,
    "fusion":     7503,
    "onshape":    7504,
}


def _post(base: str, path: str, payload: dict, timeout: float = 30.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(base: str, path: str, timeout: float = 5.0) -> dict | None:
    try:
        req = urllib.request.Request(base + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


# Each entry is (op_kind, params, expected_response_keys). An op
# passes if (a) the bridge accepts the call (HTTP 200) and (b) the
# response shape includes the expected keys (loose structural check —
# we don't compare values, since each CAD's geometry kernel may pick
# different numerics).
PARITY_OPS: list[tuple[str, dict, set[str]]] = [
    # status — every bridge must answer with at least these keys.
    ("__status__", {}, {"sw_connected"}),
    # Most basic op: begin a fresh part document. Should return ok=True
    # and never crash even with empty params.
    ("beginPart",   {}, {"ok"}),
    # Geometric primitive: extrude a 50x30x10mm rectangle. Each CAD
    # implements this differently but every bridge exposes the kind.
    ("extrude",     {"width_mm": 50.0, "height_mm": 30.0,
                       "depth_mm": 10.0}, {"ok"}),
    # Verification op (rec #23) — asserts the resulting body has a
    # plausible bbox. Optional; bridges that don't have it skip.
    ("verifyBody",  {"min_volume_mm3": 1.0}, {"ok"}),
]


def probe_cad(name: str, port: int, ops: list, timeout: float
                ) -> dict:
    """Run all parity ops against one CAD. Returns a structured result."""
    base = f"http://localhost:{port}"
    record: dict = {"cad": name, "port": port, "reachable": False,
                     "ops": [], "passed": 0, "failed": 0, "skipped": 0}
    st = _get(base, "/status", timeout=timeout)
    if st is None:
        record["skip_reason"] = f"localhost:{port}/status unreachable"
        return record
    record["reachable"] = True
    record["status_keys"] = sorted(st.keys())[:12]
    for kind, params, expect_keys in ops:
        if kind == "__status__":
            ok = expect_keys.issubset(set(st.keys()))
            record["ops"].append({
                "kind": "__status__", "ok": ok,
                "missing": sorted(expect_keys - set(st.keys())),
            })
            if ok: record["passed"] += 1
            else:  record["failed"] += 1
            continue
        try:
            r = _post(base, "/op", {"kind": kind, "params": params},
                      timeout=timeout)
        except (urllib.error.URLError, TimeoutError) as exc:
            record["ops"].append({"kind": kind, "ok": False,
                                    "transport_error":
                                       f"{type(exc).__name__}: {exc}"})
            record["failed"] += 1
            continue
        # Bridges return either {ok, result: {...}} or just the bare
        # result. Normalize and check the response carries the
        # expected structural keys somewhere.
        all_keys = set((r or {}).keys())
        inner = (r or {}).get("result") or {}
        if isinstance(inner, dict):
            all_keys |= set(inner.keys())
        ok_call = bool(r.get("ok") if "ok" in r else inner.get("ok"))
        # For "beginPart" / "extrude" we also accept "todo" / "skipped"
        # so a bridge that hasn't impl'd the op yet doesn't fail the run.
        is_skipped = bool(inner.get("todo") or inner.get("skipped"))
        if is_skipped:
            record["ops"].append({"kind": kind, "ok": True,
                                    "skipped": True,
                                    "note": inner.get("todo")
                                              or inner.get("skipped")})
            record["skipped"] += 1
            continue
        if not expect_keys.issubset(all_keys):
            record["ops"].append({"kind": kind, "ok": False,
                                    "shape_mismatch": True,
                                    "missing": sorted(expect_keys - all_keys),
                                    "got_keys": sorted(all_keys)})
            record["failed"] += 1
            continue
        record["ops"].append({"kind": kind, "ok": ok_call,
                                "ok_call": ok_call,
                                "shape_ok": True})
        if ok_call: record["passed"] += 1
        else:       record["failed"] += 1
    return record


def render_report(results: list[dict]) -> str:
    lines = []
    lines.append("=" * 64)
    lines.append("CROSS-CAD PARITY HARNESS")
    lines.append("=" * 64)
    for r in results:
        if not r["reachable"]:
            lines.append(f"  {r['cad']:<12s} :{r['port']:<5d} "
                          f"SKIP  ({r['skip_reason']})")
            continue
        lines.append(f"  {r['cad']:<12s} :{r['port']:<5d} "
                      f"pass={r['passed']:<2d} "
                      f"fail={r['failed']:<2d} "
                      f"skip={r['skipped']:<2d}")
        for op in r["ops"]:
            mark = "OK  " if op["ok"] else ("SKIP" if op.get("skipped")
                                              else "FAIL")
            extra = ""
            if op.get("shape_mismatch"):
                extra = f" missing={op['missing']}"
            elif op.get("transport_error"):
                extra = f" {op['transport_error']}"
            elif op.get("missing"):
                extra = f" missing={op['missing']}"
            lines.append(f"      {mark}  {op['kind']}{extra}")
    lines.append("=" * 64)
    reachable = [r for r in results if r["reachable"]]
    total_pass = sum(r["passed"] for r in reachable)
    total_fail = sum(r["failed"] for r in reachable)
    lines.append(f"summary: {len(reachable)}/{len(results)} reachable, "
                  f"{total_pass} pass / {total_fail} fail")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="+", default=None,
                      help="restrict to a subset of CADs (e.g. solidworks rhino)")
    ap.add_argument("--json", default=None,
                      help="dump full structured results to this path")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    selected = (CADS if not args.only
                else {k: v for k, v in CADS.items() if k in args.only})
    if not selected:
        print(f"no matching CADs (have: {list(CADS)})")
        return 2

    results = []
    for name, port in selected.items():
        results.append(probe_cad(name, port, PARITY_OPS, args.timeout))

    print(render_report(results))
    if args.json:
        Path(args.json).write_text(
            json.dumps(results, indent=2), "utf-8")

    reachable = [r for r in results if r["reachable"]]
    if not reachable:
        # Nothing reachable — exit 0 (no signal) rather than 1 (failure)
        # so CI doesn't redden when SW isn't running locally.
        return 0
    any_failed = any(r["failed"] > 0 for r in reachable)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
