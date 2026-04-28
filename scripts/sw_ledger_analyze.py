r"""sw_ledger_analyze.py - turn the learning ledger into actionable insight.

Reads sw_learning_ledger.json (and any per-CAD <cad>_learning_ledger.json) and
emits:
  - A printable feature support matrix
  - A list of features that need NEW workarounds (status=needs_workaround +
    no workaround set)
  - A list of features ready for variation expansion (status=ok)
  - A category-level health rollup

This is the human-readable face of what the system has learned.

Usage:
  python scripts/sw_ledger_analyze.py
  python scripts/sw_ledger_analyze.py --all-cads
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def load_ledger(name: str) -> dict:
    path = REPO / "outputs" / f"{name}_learning_ledger.json"
    if not path.exists():
        # Fall back to the canonical SW name
        if name == "sw":
            path = REPO / "outputs" / "sw_learning_ledger.json"
            if not path.exists():
                return {}
        else:
            return {}
    return json.loads(path.read_text(encoding="utf-8"))


def analyze(ledger: dict, label: str = "sw") -> dict:
    """Compute summary stats."""
    by_status: Counter = Counter()
    by_category: dict[str, Counter] = defaultdict(Counter)
    needs_workaround_no_fix: list[str] = []
    ready_for_variation: list[str] = []

    for feat, e in ledger.items():
        status = e.get("status", "unknown")
        by_status[status] += 1
        # Infer category from the kind/feature_key namespace
        cat = "misc"
        for prefix, name in [
            ("sketch", "sketch"), ("extrude", "feat"), ("revolve", "feat"),
            ("helix", "feat"), ("loft", "feat"), ("sweep", "feat"),
            ("fillet", "feat"), ("chamfer", "feat"), ("shell", "feat"),
            ("rib", "feat"), ("draft", "feat"), ("hole", "feat"),
            ("circular_pattern", "pattern"), ("circularPattern", "pattern"),
            ("linear_pattern", "pattern"), ("mirror", "pattern"),
            ("multibody", "multibody"), ("combine", "multibody"),
            ("sm_", "sm"), ("sheet", "sm"),
            ("surface", "surf"), ("weld", "weldment"),
            ("mold", "mold"), ("compound", "cswe"),
            ("addParameter", "config"),
        ]:
            if prefix in feat.lower():
                cat = name
                break
        by_category[cat][status] += 1

        if status == "needs_workaround" and not e.get("workaround"):
            needs_workaround_no_fix.append(feat)
        if status == "ok" and e.get("pass_count", 0) >= 1:
            ready_for_variation.append(feat)

    return {
        "label": label,
        "total": len(ledger),
        "by_status": dict(by_status),
        "by_category": {k: dict(v) for k, v in by_category.items()},
        "needs_workaround_no_fix": needs_workaround_no_fix,
        "ready_for_variation": ready_for_variation,
    }


def render(report: dict) -> str:
    """Markdown rendering of the analysis."""
    out = [f"# Learning ledger analysis ({report['label']})\n"]
    out.append(f"Total features tracked: **{report['total']}**\n")

    out.append("\n## By status")
    for s, n in sorted(report["by_status"].items(),
                        key=lambda kv: -kv[1]):
        out.append(f"  - {s}: {n}")

    out.append("\n## By category")
    out.append("| category | ok | flaky | needs_workaround | unsupported | unknown |")
    out.append("|----------|----|----|------------------|-------------|---------|")
    for cat in sorted(report["by_category"]):
        c = report["by_category"][cat]
        out.append(f"| {cat} | {c.get('ok', 0)} | {c.get('flaky', 0)} | "
                   f"{c.get('needs_workaround', 0)} | "
                   f"{c.get('unsupported', 0)} | {c.get('unknown', 0)} |")

    if report["needs_workaround_no_fix"]:
        out.append("\n## Features needing NEW workarounds")
        out.append("(status=needs_workaround, no workaround registered)\n")
        for f in report["needs_workaround_no_fix"]:
            out.append(f"  - `{f}`")

    if report["ready_for_variation"]:
        out.append(f"\n## Features ready for deeper coverage ({len(report['ready_for_variation'])})")
        out.append("Run: `python scripts/run_sw_feature_matrix.py --variations 3`")

    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-cads", action="store_true",
                    help="Combine ledgers from sw, rhino, fusion, onshape, autocad")
    ap.add_argument("--cad", default="sw",
                    help="Single CAD to analyze (default sw)")
    ap.add_argument("--save", action="store_true",
                    help="Write outputs/learning_analysis.md")
    args = ap.parse_args()

    if args.all_cads:
        cads = ["sw", "rhino", "fusion", "onshape", "autocad"]
        reports = []
        for c in cads:
            led = load_ledger(c)
            if led:
                reports.append(analyze(led, label=c))
        if not reports:
            print("No ledgers found yet.")
            return
        text = "\n---\n".join(render(r) for r in reports)
    else:
        led = load_ledger(args.cad)
        if not led:
            print(f"Ledger {args.cad} is empty or missing.")
            return
        text = render(analyze(led, label=args.cad))

    print(text)
    if args.save:
        out = REPO / "outputs" / "learning_analysis.md"
        out.write_text(text, encoding="utf-8")
        print(f"Saved: {out}")


if __name__ == "__main__":
    main()
