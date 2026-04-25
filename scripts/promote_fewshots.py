"""Auto-promote accepted feedback runs into the few-shot library.

Walks outputs/feedback/, finds entries with decision="accept",
groups by part-family inferred from the plan's op vocabulary, and
writes the top-N per family into aria_os/native_planner/fewshots/
as `auto_<family>_<hash>.json`.

Idempotent — re-running is safe. Existing auto-promoted shots are
overwritten if their plan_hash matches; new ones are added.
Hand-curated shots (`flange.json`, `gyroid_bracket.json`, etc.)
are NEVER touched.

Run nightly via the existing scripts/weekly_audit pattern, or once
on demand:

    python scripts/promote_fewshots.py [--dry-run] [--top N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Op-kind buckets that hint at the part family. Order matters: the
# first matching bucket wins — so the more specific ones come first.
_FAMILY_BUCKETS: list[tuple[str, set[str]]] = [
    ("hardware",       {"threadFeature", "gearFeature"}),
    ("sheet_metal",    {"sheetMetalBase", "sheetMetalFlange",
                        "sheetMetalBend", "sheetMetalLouver"}),
    ("assembly",       {"asmBegin", "addComponent", "mateConcentric",
                        "mateGear", "mateSlider"}),
    ("drawing",        {"beginDrawing", "addView", "gdtFrame",
                        "datumLabel", "sectionView"}),
    ("implicit",       {"implicitInfill", "implicitChannel",
                        "implicitLattice", "implicitField",
                        "meshImportAndCombine"}),
    ("revolve",        {"revolve"}),
    ("sweep",          {"sweep"}),
    ("loft",           {"loft"}),
    ("coil",           {"coil", "helix"}),
    ("circular_pattern", {"circularPattern"}),
    ("extrude",        {"extrude"}),
]


def _classify_family(plan: list[dict]) -> str:
    """Pick the most specific family that matches the plan's ops."""
    kinds = {op.get("kind") for op in plan or []
              if isinstance(op, dict)}
    for fam, bucket in _FAMILY_BUCKETS:
        if kinds & bucket:
            return fam
    return "generic"


def _ops_used(plan: list[dict]) -> list[str]:
    return sorted({op.get("kind") for op in plan or []
                    if isinstance(op, dict) and op.get("kind")})


def _is_curated(p: Path) -> bool:
    """Hand-curated few-shots don't start with 'auto_'."""
    return not p.name.startswith("auto_")


def collect_accepted(feedback_dir: Path) -> list[dict]:
    """Read every <run_id>.json with decision == accept."""
    out = []
    for f in sorted(feedback_dir.glob("*.json")):
        if f.name == "INDEX.jsonl":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("decision") == "accept" and d.get("plan"):
            out.append(d)
    return out


def rank_per_family(accepted: list[dict],
                      *, top_n: int = 5) -> dict[str, list[dict]]:
    """Group accepted entries by family and pick the top-N per family
    by op-coverage (plans using more distinct ops are richer
    examples) + recency tiebreak."""
    by_family: dict[str, list[dict]] = {}
    for e in accepted:
        fam = _classify_family(e["plan"])
        by_family.setdefault(fam, []).append(e)
    out: dict[str, list[dict]] = {}
    for fam, entries in by_family.items():
        # Score: distinct op count + 0.001 * recency (so newer wins ties)
        def score(e):
            ts = e.get("timestamp_utc") or ""
            return (len(_ops_used(e["plan"])), ts)
        entries.sort(key=score, reverse=True)
        out[fam] = entries[:top_n]
    return out


def write_fewshot_files(ranked: dict[str, list[dict]],
                          target_dir: Path,
                          *, dry_run: bool = False) -> list[Path]:
    """Write top-ranked entries into the few-shots dir as
    auto_<family>_<plan_hash>.json. Skips entries whose hash already
    landed (de-dup) and never overwrites curated files."""
    written: list[Path] = []
    existing_hashes = set()
    for f in target_dir.glob("auto_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            existing_hashes.add(d.get("plan_hash"))
        except Exception:
            continue
    for fam, entries in ranked.items():
        for e in entries:
            ph = e.get("plan_hash")
            if not ph:
                continue
            target = target_dir / f"auto_{fam}_{ph}.json"
            if _is_curated(target):
                # impossibility — auto_ prefix guarantees this — but be safe.
                continue
            if target.exists() and ph in existing_hashes:
                continue
            payload = {
                "id":       f"auto_{fam}_{ph}",
                "goal":     e.get("goal", ""),
                "tags":     [fam] + _ops_used(e["plan"]),
                "ops_used": _ops_used(e["plan"]),
                "plan":     e["plan"],
                "_source":  "auto-promoted from feedback",
                "_run_id":  e.get("run_id"),
                "_promoted_at": e.get("timestamp_utc"),
            }
            if not dry_run:
                target.write_text(
                    json.dumps(payload, indent=2, default=str),
                    encoding="utf-8")
            written.append(target)
    return written


def prune_stale_auto_shots(target_dir: Path,
                             keep_hashes: set[str],
                             *, dry_run: bool = False) -> list[Path]:
    """Remove auto_*.json files whose plan_hash isn't in the current
    keep_hashes set — i.e. their source feedback was deleted or
    they fell out of the top-N."""
    removed: list[Path] = []
    for f in target_dir.glob("auto_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("plan_hash") not in keep_hashes:
            if not dry_run:
                f.unlink()
            removed.append(f)
    return removed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                    help="Don't write files; print what would change.")
    p.add_argument("--top", type=int, default=5,
                    help="Top-N accepts per family (default 5).")
    args = p.parse_args()

    feedback_dir = REPO_ROOT / "outputs" / "feedback"
    fewshots_dir = REPO_ROOT / "aria_os" / "native_planner" / "fewshots"

    if not feedback_dir.is_dir():
        print(f"No feedback dir at {feedback_dir} — nothing to promote.")
        return 0

    accepted = collect_accepted(feedback_dir)
    print(f"Collected {len(accepted)} accepted feedback entries.")
    if not accepted:
        return 0

    ranked = rank_per_family(accepted, top_n=args.top)
    print(f"Ranked {sum(len(v) for v in ranked.values())} entries "
           f"across {len(ranked)} families: "
           f"{sorted(ranked.keys())}")

    written = write_fewshot_files(ranked, fewshots_dir,
                                     dry_run=args.dry_run)
    keep = {e["plan_hash"] for v in ranked.values()
             for e in v if e.get("plan_hash")}
    pruned = prune_stale_auto_shots(fewshots_dir, keep,
                                       dry_run=args.dry_run)

    action = "Would write" if args.dry_run else "Wrote"
    print(f"{action} {len(written)} few-shot files; pruned {len(pruned)} stale.")
    for f in written[:10]:
        print(f"  + {f.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
