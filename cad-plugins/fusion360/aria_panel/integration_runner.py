"""W12.1 — Fusion integration test runner.

Lives inside the aria_panel add-in. Accepts a plan JSON via the
existing executeFeaturePlan WebView channel OR via a side-loaded
file at INPUT_PATH (env: ARIA_INTEGRATION_PLAN). Executes every
op, captures a viewport screenshot after each, writes a structured
result JSON to OUTPUT_DIR.

Why not plain pytest: pytest can't drive Fusion. The Fusion API
runs inside Fusion's Python interpreter (adsk module is unavailable
elsewhere). So this script is what an integration run does.

Invocation modes:

  Mode A — from inside Fusion's Scripts & Add-Ins, "Run":
      Pick an `aria_integration_runner.py` script that imports +
      runs `run_integration(plan_path, out_dir)` from this module.

  Mode B — automated harness (scripts/integration_test.py):
      Writes the plan JSON + the trigger file, signals Fusion to
      run via Fusion's command-line API (`fusion360.exe /run`),
      polls for the result JSON.

Output schema (one file per op + one summary):

  outputs/integration/<ts>/fusion/
    summary.json
    op_001_beginPlan.json
    op_001_beginPlan.png
    op_002_newSketch.json
    op_002_newSketch.png
    ...

summary.json shape:
  {
    timestamp_utc:    str,
    host:             "fusion",
    fusion_version:   str,
    plan_id:          str,
    n_ops:            int,
    n_passed:         int,
    n_failed:         int,
    failed_at:        int | -1,
    elapsed_total_s:  float,
    ops:              [{seq, kind, ok, error?, screenshot, elapsed_s}]
  }

This is the canonical JSON the W12.4 aggregator parses to build
the cross-host report.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# These imports only succeed inside Fusion. The runner is a no-op
# outside Fusion (returns a clear "host_unavailable" result) so the
# unit tests + the W12.4 aggregator can import it without the host.
try:
    import adsk.core   # type: ignore
    import adsk.fusion  # type: ignore
    _HAS_FUSION = True
except ImportError:
    _HAS_FUSION = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_kind_for_filename(kind: str | None, seq: int) -> str:
    """Sanitize op kind for the screenshot/result filename."""
    safe = (kind or "unknown").replace("/", "_").replace(" ", "_")
    return f"op_{seq:03d}_{safe}"


def _capture_viewport(out_path: Path) -> bool:
    """Save a PNG of the active viewport. Returns True on success.

    Uses Fusion's `viewport.saveAsImageFile` which works reliably
    on Win10/11 host. Width/height set to 1024x768 — enough to
    eyeball the feature, small enough to not balloon the test
    artifact dir."""
    if not _HAS_FUSION:
        return False
    try:
        app = adsk.core.Application.get()
        if app is None:
            return False
        view = app.activeViewport
        if view is None:
            return False
        view.refresh()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return bool(view.saveAsImageFile(str(out_path), 1024, 768))
    except Exception:
        return False


def _fusion_version() -> str:
    if not _HAS_FUSION:
        return "host_unavailable"
    try:
        return adsk.core.Application.get().version or ""
    except Exception:
        return ""


def run_integration(plan_path: str | Path,
                       out_dir: str | Path | None = None,
                       *, plan_id: str | None = None) -> dict:
    """Run a plan against Fusion and write per-op + summary JSON.

    Returns the summary dict. Safe to call outside Fusion — returns
    a stub summary with host_unavailable=True so the W12.4
    aggregator can record "skipped" and move on."""
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if isinstance(plan, dict) and "plan" in plan:
        plan_id = plan_id or plan.get("id") or plan_path.stem
        ops = plan["plan"]
    elif isinstance(plan, list):
        ops = plan
        plan_id = plan_id or plan_path.stem
    else:
        raise ValueError(
            f"Plan file {plan_path} must be a list of ops OR a "
            "dict with a 'plan' key.")

    ts = _now_iso()
    if out_dir is None:
        out_dir = (Path.cwd() / "outputs" / "integration"
                    / ts / "fusion")
    else:
        out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict = {
        "timestamp_utc":   ts,
        "host":            "fusion",
        "fusion_version":  _fusion_version(),
        "plan_id":         plan_id,
        "plan_path":       str(plan_path),
        "n_ops":           len(ops),
        "host_unavailable": not _HAS_FUSION,
        "ops":             [],
    }
    if not _HAS_FUSION:
        summary["error"] = ("adsk module not importable — runner "
                              "must be invoked from inside Fusion's "
                              "Scripts & Add-Ins panel.")
        (out_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8")
        return summary

    # Lazily import the in-Fusion executor — we can't import at
    # module load because the panel may not be running yet.
    try:
        from aria_panel import _execute_feature, _execute_feature_plan
    except ImportError:
        summary["error"] = ("aria_panel module not loadable — "
                              "ensure the add-in is loaded via "
                              "Scripts & Add-Ins → Run before "
                              "calling integration_runner.")
        (out_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8")
        return summary

    t_total_start = time.time()
    n_passed = 0
    n_failed = 0
    failed_at = -1

    for i, op in enumerate(ops, start=1):
        kind = (op.get("kind") if isinstance(op, dict) else None) or "?"
        params = (op.get("params") if isinstance(op, dict) else {}) or {}
        stem = _safe_kind_for_filename(kind, i)
        op_record = {
            "seq":      i,
            "kind":     kind,
            "params":   params,
            "ok":       False,
            "error":    None,
            "result":   None,
            "screenshot": None,
            "elapsed_s": 0.0,
        }
        t0 = time.time()
        try:
            res = _execute_feature(kind, params)
            op_record["ok"] = True
            op_record["result"] = res
            n_passed += 1
        except Exception as exc:
            op_record["error"] = (
                f"{type(exc).__name__}: {exc}\n"
                + "".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__))[:2000])
            n_failed += 1
            if failed_at < 0:
                failed_at = i

        op_record["elapsed_s"] = round(time.time() - t0, 2)

        # Screenshot the viewport AFTER the op (showing the result).
        # We always try; failure to screenshot doesn't fail the op.
        png_path = out_dir / f"{stem}.png"
        if _capture_viewport(png_path):
            op_record["screenshot"] = png_path.name

        # Write per-op JSON immediately so a Fusion crash doesn't
        # lose the data we already collected.
        (out_dir / f"{stem}.json").write_text(
            json.dumps(op_record, indent=2, default=str),
            encoding="utf-8")

        summary["ops"].append({
            k: op_record[k] for k in
            ("seq", "kind", "ok", "error", "screenshot", "elapsed_s")
        })

        if not op_record["ok"]:
            # First-failure halt mirrors _execute_feature_plan's
            # behavior. The W12.4 aggregator can re-run from the
            # failure point if needed.
            break

    summary["n_passed"] = n_passed
    summary["n_failed"] = n_failed
    summary["failed_at"] = failed_at
    summary["elapsed_total_s"] = round(time.time() - t_total_start, 2)

    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8")
    return summary


def run_default_from_env() -> dict:
    """Convenience entry point for invocation from a Fusion script.

    Reads ARIA_INTEGRATION_PLAN (path to plan JSON) +
    ARIA_INTEGRATION_OUT (output dir, optional). Defaults to a
    timestamped dir under outputs/integration/.

    Drop a 1-line script in Fusion that does:

        from aria_panel.integration_runner import run_default_from_env
        run_default_from_env()

    and configure it to run on add-in load when the env vars are
    present. The W12.4 aggregator sets these before it pokes
    Fusion."""
    plan_path = os.environ.get("ARIA_INTEGRATION_PLAN")
    if not plan_path:
        return {"error": "ARIA_INTEGRATION_PLAN env var unset"}
    out_dir = os.environ.get("ARIA_INTEGRATION_OUT")
    return run_integration(plan_path, out_dir)


__all__ = ["run_integration", "run_default_from_env"]
