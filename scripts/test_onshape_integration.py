"""W12.3 — Onshape integration test runner.

Runs ARIA plans against a live Onshape Part Studio. Unlike Fusion
(adsk only inside Fusion) and Rhino (RhinoCommon only inside
Rhino), Onshape is just a REST API — so this runs anywhere the
keys are available.

Invocation:
    python scripts/test_onshape_integration.py \\
        --plan tests/plans/m6_cap_screw.json \\
        --did <DOC_ID> --wid <WORKSPACE_ID> --eid <ELEMENT_ID> \\
        [--out outputs/integration/<ts>/onshape/]

A dedicated test Part Studio is recommended — every run appends
features to the same studio. The script can clear features before
each run if `--reset` is passed (it walks the existing features
list and DELETEs each one).

Output schema mirrors the Fusion + Rhino runners so the W12.4
aggregator can build one cross-host report.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_name(kind: str, seq: int) -> str:
    safe = (kind or "unknown").replace("/", "_").replace(" ", "_")
    return f"op_{seq:03d}_{safe}"


def _list_features(client, did: str, wid: str, eid: str) -> list[dict]:
    """Walk the part studio and return every existing feature.
    Used by --reset."""
    try:
        path = (f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features")
        resp = client.request("GET", path)
        if not isinstance(resp, dict):
            return []
        return resp.get("features") or []
    except Exception:
        return []


def _delete_feature(client, did: str, wid: str, eid: str,
                      feature_id: str) -> bool:
    try:
        path = (f"/api/partstudios/d/{did}/w/{wid}/e/{eid}"
                 f"/features/featureid/{feature_id}")
        client.request("DELETE", path)
        return True
    except Exception:
        return False


def _reset_studio(client, did: str, wid: str, eid: str) -> int:
    """Delete every existing feature in the studio. Returns count."""
    feats = _list_features(client, did, wid, eid)
    n = 0
    for f in feats:
        fid = f.get("featureId") or f.get("id")
        if fid and _delete_feature(client, did, wid, eid, fid):
            n += 1
    return n


def run_integration(plan_path: Path,
                      did: str, wid: str, eid: str,
                      *, out_dir: Path | None = None,
                      reset: bool = False) -> dict:
    """Execute every op of `plan_path` against the named Part Studio.
    Writes per-op JSON + summary.json mirroring the other host
    runners' shape."""
    from aria_os.onshape.executor import OnshapeExecutor
    from aria_os.onshape.client import get_client

    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    if isinstance(plan_data, dict) and "plan" in plan_data:
        ops = plan_data["plan"]
        plan_id = plan_data.get("id") or plan_path.stem
    elif isinstance(plan_data, list):
        ops = plan_data
        plan_id = plan_path.stem
    else:
        raise ValueError(f"Plan {plan_path} malformed")

    ts = _now_iso()
    if out_dir is None:
        out_dir = REPO_ROOT / "outputs" / "integration" / ts / "onshape"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict = {
        "timestamp_utc": ts,
        "host":          "onshape",
        "plan_id":       plan_id,
        "plan_path":     str(plan_path),
        "did":           did,
        "wid":           wid,
        "eid":           eid,
        "n_ops":         len(ops),
        "ops":           [],
    }

    client = get_client(repo_root=REPO_ROOT)
    if reset:
        n_reset = _reset_studio(client, did, wid, eid)
        summary["reset_features"] = n_reset
        print(f"[onshape] reset removed {n_reset} pre-existing features")

    executor = OnshapeExecutor(did, wid, eid, client=client)

    t_total = time.time()
    n_passed = 0
    n_failed = 0
    failed_at = -1

    for i, op in enumerate(ops, start=1):
        kind = (op.get("kind") if isinstance(op, dict) else None) or "?"
        params = (op.get("params") if isinstance(op, dict) else {}) or {}
        rec = {
            "seq": i, "kind": kind, "params": params,
            "ok": False, "error": None, "result": None,
            "elapsed_s": 0.0,
        }
        t0 = time.time()
        try:
            res = executor.execute(kind, params)
            rec["ok"] = True
            rec["result"] = res
            n_passed += 1
        except Exception as exc:
            tb = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))[:2000]
            rec["error"] = f"{type(exc).__name__}: {exc}\n{tb}"
            n_failed += 1
            if failed_at < 0:
                failed_at = i
        rec["elapsed_s"] = round(time.time() - t0, 2)

        # Per-op JSON immediately so a network drop doesn't lose history
        stem = _safe_name(kind, i)
        (out_dir / f"{stem}.json").write_text(
            json.dumps(rec, indent=2, default=str), encoding="utf-8")
        summary["ops"].append({
            k: rec[k] for k in
            ("seq", "kind", "ok", "error", "elapsed_s")
        })
        print(f"  [{i:3d}/{len(ops)}] {'PASS' if rec['ok'] else 'FAIL'} "
               f"{kind} ({rec['elapsed_s']}s)")
        if not rec["ok"]:
            break   # halt on first failure (matches Fusion + Rhino)

    summary["n_passed"] = n_passed
    summary["n_failed"] = n_failed
    summary["failed_at"] = failed_at
    summary["elapsed_total_s"] = round(time.time() - t_total, 2)

    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--plan", required=True,
                    help="Path to plan JSON ({plan: [...]} or [op,...])")
    p.add_argument("--did", required=True, help="Onshape document ID")
    p.add_argument("--wid", required=True, help="Workspace ID")
    p.add_argument("--eid", required=True,
                    help="Part Studio element ID")
    p.add_argument("--out", default=None,
                    help="Output directory (default: outputs/integration/<ts>/onshape/)")
    p.add_argument("--reset", action="store_true",
                    help="Delete every existing feature before running")
    args = p.parse_args()

    summary = run_integration(
        Path(args.plan), args.did, args.wid, args.eid,
        out_dir=Path(args.out) if args.out else None,
        reset=args.reset)
    print()
    print(f"=== Onshape integration: {summary['n_passed']}/{summary['n_ops']} "
           f"passed in {summary['elapsed_total_s']}s")
    if summary.get("failed_at", -1) > 0:
        print(f"=== First failure at op #{summary['failed_at']}")
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
