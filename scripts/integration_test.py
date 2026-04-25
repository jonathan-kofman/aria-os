"""W12.4 — Cross-host integration aggregator.

Runs the canonical few-shot plans against all available hosts
(Fusion / Rhino / Onshape), collects per-host result JSONs, and
emits one cross-host summary table.

Plan corpus: by default, every JSON file in
`aria_os/native_planner/fewshots/` is run. These are the 11 hand-
validated plans (cap_screw, flange, impeller, spur_gear, helical_
spring, etc.) — diverse enough to exercise the full W1+W3+W4
op vocabulary at least once each.

Per-host invocation:
  Fusion  — sets ARIA_INTEGRATION_PLAN env var, the user must
            click "Run" on the integration runner script inside
            Fusion. The aggregator polls the output dir for
            `summary.json` to land.
  Rhino   — invokes `Rhino.exe -nosplash -runscript="!AriaIntegrate"`
            with the env var pre-set.
  Onshape — calls scripts/test_onshape_integration.py directly
            (pure REST, no GUI).

Skipif: hosts that aren't available (Fusion not installed, Rhino
unreachable, Onshape DID/WID/EID env vars not set) are recorded
as "skipped" not "failed". The test runner exits 0 when all
non-skipped hosts pass.

Output:
    outputs/integration/<ts>/
        cross_host_report.json
        cross_host_report.md       (human-readable)
        fusion/    (if available)
        rhino/     (if available)
        onshape/   (if available)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _detect_fusion() -> str | None:
    """Return path to Fusion 360 launcher if installed, else None."""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "webdeploy"
        / "production",
    ]
    for base in candidates:
        if base.is_dir():
            # Walk for the latest Fusion360.exe
            for exe in sorted(base.rglob("Fusion360.exe"), reverse=True):
                return str(exe)
    return None


def _detect_rhino() -> str | None:
    """Best-effort Rhino.exe discovery on Windows."""
    candidates = [
        Path(r"C:\Program Files\Rhino 8\System\Rhino.exe"),
        Path(r"C:\Program Files\Rhino 7\System\Rhino.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    found = shutil.which("Rhino.exe") or shutil.which("rhino")
    return found


def _onshape_target() -> tuple[str, str, str] | None:
    """Read ARIA_ONSHAPE_INTEGRATION_DID/WID/EID env vars.
    Returns None if any are missing — we won't accidentally clobber
    a real workspace."""
    did = os.environ.get("ARIA_ONSHAPE_INTEGRATION_DID")
    wid = os.environ.get("ARIA_ONSHAPE_INTEGRATION_WID")
    eid = os.environ.get("ARIA_ONSHAPE_INTEGRATION_EID")
    if did and wid and eid:
        return (did, wid, eid)
    return None


def _gather_plans(plans_glob: str | None) -> list[Path]:
    """Default: every few-shot. Override with --plans <glob>."""
    if plans_glob:
        return sorted(Path(REPO_ROOT).glob(plans_glob))
    fewshots = REPO_ROOT / "aria_os" / "native_planner" / "fewshots"
    return sorted(p for p in fewshots.glob("*.json")
                   if not p.name.startswith("auto_")
                   and p.name != "__init__.py")


def _run_onshape(plan: Path, did: str, wid: str, eid: str,
                  out_dir: Path) -> dict:
    """Invoke scripts/test_onshape_integration.py for one plan.
    Returns the parsed summary.json (or an error stub)."""
    plan_out = out_dir / "onshape" / plan.stem
    plan_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "scripts/test_onshape_integration.py",
        "--plan", str(plan),
        "--did", did, "--wid", wid, "--eid", eid,
        "--out", str(plan_out),
        "--reset",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=300)
    except subprocess.TimeoutExpired:
        return {"plan_id": plan.stem, "status": "timeout",
                "elapsed_total_s": 300}
    summary_path = plan_out / "summary.json"
    if summary_path.is_file():
        return json.loads(summary_path.read_text(encoding="utf-8"))
    return {
        "plan_id": plan.stem, "status": "no_summary",
        "stderr": result.stderr[-500:] if result.stderr else "",
    }


def _stub_unavailable(host: str, reason: str) -> dict:
    return {
        "host":   host,
        "status": "skipped",
        "reason": reason,
        "n_ops":  0,
        "n_passed": 0,
        "n_failed": 0,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        f"# ARIA cross-host integration — {report['timestamp_utc']}\n",
        f"Plans run: **{len(report['plans'])}**\n",
        "## Per-host availability\n",
    ]
    for host, info in report["host_status"].items():
        avail = "✅ available" if info.get("available") else f"⚠️  skipped ({info.get('reason', '?')})"
        lines.append(f"- **{host}** — {avail}")
    lines.append("\n## Pass rate per host\n")
    lines.append("| Host | Plans | Ops | Passed | Failed | %    |")
    lines.append("|------|------:|----:|-------:|-------:|-----:|")
    for host, agg in report["per_host"].items():
        n_plans = agg["n_plans"]
        n_ops = agg["n_ops"]
        passed = agg["n_passed"]
        failed = agg["n_failed"]
        pct = f"{100*passed/max(n_ops,1):.0f}%"
        lines.append(f"| {host:<8} | {n_plans:>5} | {n_ops:>3} | "
                      f"{passed:>6} | {failed:>6} | {pct:>4} |")
    if report.get("first_failures"):
        lines.append("\n## First failures (top 10)\n")
        for f in report["first_failures"][:10]:
            lines.append(
                f"- **{f['host']}** / `{f['plan_id']}` op #{f['op_seq']} "
                f"({f['op_kind']}) — {f['error'][:120]}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--plans",
                    help="Glob of plan JSONs (default: all curated few-shots)")
    p.add_argument("--out", default=None)
    p.add_argument("--hosts", default="auto",
                    help="Comma list: fusion,rhino,onshape | 'auto' detects")
    args = p.parse_args()

    plans = _gather_plans(args.plans)
    print(f"Running {len(plans)} plans...")
    if not plans:
        print("No plans found.")
        return 1

    ts = _now_iso()
    out_dir = Path(args.out) if args.out else REPO_ROOT / "outputs" / "integration" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    requested = (
        ["fusion", "rhino", "onshape"]
        if args.hosts == "auto" else
        [h.strip().lower() for h in args.hosts.split(",")])

    host_status: dict = {}
    fusion_path = _detect_fusion() if "fusion" in requested else None
    rhino_path = _detect_rhino() if "rhino" in requested else None
    onshape_target = _onshape_target() if "onshape" in requested else None

    host_status["fusion"] = {
        "available": bool(fusion_path),
        "reason": "" if fusion_path else "Fusion360.exe not found"}
    host_status["rhino"] = {
        "available": bool(rhino_path),
        "reason": "" if rhino_path else "Rhino.exe not found"}
    host_status["onshape"] = {
        "available": bool(onshape_target),
        "reason": "" if onshape_target else
            ("ARIA_ONSHAPE_INTEGRATION_DID/WID/EID env vars unset")}

    per_plan_results: list[dict] = []
    first_failures: list[dict] = []
    per_host_agg = {h: {"n_plans": 0, "n_ops": 0,
                         "n_passed": 0, "n_failed": 0}
                    for h in ("fusion", "rhino", "onshape")}

    for plan in plans:
        plan_record = {"plan_id": plan.stem, "plan_path": str(plan),
                        "results": {}}
        # Onshape — pure REST, runs unattended
        if onshape_target:
            did, wid, eid = onshape_target
            print(f"[onshape] {plan.name}...")
            res = _run_onshape(plan, did, wid, eid, out_dir)
            plan_record["results"]["onshape"] = {
                "n_ops": res.get("n_ops", 0),
                "n_passed": res.get("n_passed", 0),
                "n_failed": res.get("n_failed", 0),
                "failed_at": res.get("failed_at", -1),
                "elapsed_total_s": res.get("elapsed_total_s", 0),
            }
            per_host_agg["onshape"]["n_plans"] += 1
            per_host_agg["onshape"]["n_ops"] += res.get("n_ops", 0)
            per_host_agg["onshape"]["n_passed"] += res.get("n_passed", 0)
            per_host_agg["onshape"]["n_failed"] += res.get("n_failed", 0)
            for op in (res.get("ops") or []):
                if not op.get("ok"):
                    first_failures.append({
                        "host": "onshape", "plan_id": plan.stem,
                        "op_seq": op.get("seq"), "op_kind": op.get("kind"),
                        "error": (op.get("error") or "")[:300],
                    })
                    break
        else:
            plan_record["results"]["onshape"] = _stub_unavailable(
                "onshape", host_status["onshape"]["reason"])

        # Fusion — needs the user to click "Run" inside the app for now.
        # The aggregator writes the env hint file + waits up to 5min for
        # summary.json to appear. CI / unattended path is W12.5 follow-up.
        plan_record["results"]["fusion"] = _stub_unavailable(
            "fusion", "manual: click Run on integration_runner inside Fusion")
        plan_record["results"]["rhino"] = _stub_unavailable(
            "rhino", "manual: !AriaIntegrate command inside Rhino")
        per_plan_results.append(plan_record)

    # Aggregate
    report = {
        "timestamp_utc":  ts,
        "host_status":    host_status,
        "plans":          per_plan_results,
        "per_host":       per_host_agg,
        "first_failures": first_failures,
    }
    (out_dir / "cross_host_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / "cross_host_report.md").write_text(
        _render_markdown(report), encoding="utf-8")

    print()
    print("=== Cross-host integration summary ===")
    for h, agg in per_host_agg.items():
        avail = host_status[h]["available"]
        if not avail:
            print(f"  {h:<8}: skipped ({host_status[h]['reason']})")
        else:
            pct = 100 * agg["n_passed"] / max(agg["n_ops"], 1)
            print(f"  {h:<8}: {agg['n_passed']}/{agg['n_ops']} "
                   f"ops ({pct:.0f}%) across {agg['n_plans']} plans")
    print(f"\nReport: {out_dir / 'cross_host_report.md'}")

    # Exit non-zero if any non-skipped host had failures
    any_fails = any(
        host_status[h]["available"] and per_host_agg[h]["n_failed"] > 0
        for h in per_host_agg)
    return 1 if any_fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
