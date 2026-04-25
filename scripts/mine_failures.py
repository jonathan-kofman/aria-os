"""Failure-mode miner — cluster reject reasons + suggest fixes.

Walks outputs/feedback/, finds entries with decision="reject", then:
  1. Tokenizes each reason
  2. Clusters by Jaccard similarity ≥ 0.5 (cheap, no embeddings)
  3. For each cluster, suggests a remediation:
       - Recurring failed_op kind → new validator rule
       - Cluster keyword + ops_used → new few-shot template
       - Goal pattern → new dispatcher hardcoded planner

Outputs outputs/feedback/FAILURE_DIGEST.json + a printable summary.
Run weekly via scripts/weekly_audit, or once on demand:

    python scripts/mine_failures.py [--min-cluster-size N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z_]+")
_STOP = {
    "the", "a", "an", "is", "was", "be", "of", "in", "on", "for",
    "to", "and", "or", "but", "not", "this", "that", "with", "it",
}


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")
             if t.lower() not in _STOP}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cluster(reasons: list[tuple[int, str]],
              *, threshold: float = 0.5) -> list[list[int]]:
    """Greedy clustering by Jaccard ≥ threshold. Returns list of
    cluster index lists. Each input item is (id, text)."""
    clusters: list[list[int]] = []
    cluster_tokens: list[set[str]] = []
    for idx, text in reasons:
        toks = _tokens(text)
        placed = False
        for ci, ctoks in enumerate(cluster_tokens):
            if _jaccard(toks, ctoks) >= threshold:
                clusters[ci].append(idx)
                cluster_tokens[ci] = ctoks | toks
                placed = True
                break
        if not placed:
            clusters.append([idx])
            cluster_tokens.append(toks)
    return clusters


def _suggest_remediation(cluster_entries: list[dict]) -> dict:
    """For a cluster, propose what to add/fix."""
    op_failures = Counter()
    op_kinds = Counter()
    keywords = Counter()
    for e in cluster_entries:
        plan = e.get("plan") or []
        idx = e.get("failed_op_index")
        if idx is not None and 0 <= idx < len(plan):
            op_failures[plan[idx].get("kind", "?")] += 1
        for op in plan:
            if isinstance(op, dict) and op.get("kind"):
                op_kinds[op["kind"]] += 1
        for tok in _tokens(e.get("reason", "")):
            keywords[tok] += 1

    suggestions: list[str] = []
    if op_failures:
        most_common, n = op_failures.most_common(1)[0]
        if n >= 2:
            suggestions.append(
                f"Add validator rule: {n} rejects target failed_op="
                f"{most_common!r}. Investigate the rule that should "
                f"have caught this preflight.")
    if len(cluster_entries) >= 3 and op_kinds:
        top_ops = ", ".join(o for o, _ in op_kinds.most_common(4))
        suggestions.append(
            f"Add few-shot covering ops [{top_ops}] — cluster of "
            f"{len(cluster_entries)} rejects share these ops.")
    if keywords:
        top_kws = [k for k, _ in keywords.most_common(5)]
        suggestions.append(
            f"Investigate the recurring failure-mode keywords: "
            f"{', '.join(top_kws)}.")

    return {
        "n_entries":   len(cluster_entries),
        "op_failures": dict(op_failures),
        "ops_used":    dict(op_kinds.most_common(8)),
        "top_keywords": [k for k, _ in keywords.most_common(8)],
        "suggestions": suggestions,
        "example_run_ids": [e.get("run_id") for e in cluster_entries[:3]],
        "example_reasons": [e.get("reason", "")[:120]
                              for e in cluster_entries[:3]],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-cluster-size", type=int, default=2,
                    help="Drop clusters with fewer entries than this.")
    p.add_argument("--threshold", type=float, default=0.5,
                    help="Jaccard threshold for clustering.")
    p.add_argument("--out",
                    default="outputs/feedback/FAILURE_DIGEST.json")
    args = p.parse_args()

    feedback_dir = REPO_ROOT / "outputs" / "feedback"
    if not feedback_dir.is_dir():
        print(f"No feedback dir at {feedback_dir}.")
        return 0

    rejects: list[dict] = []
    for f in sorted(feedback_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("decision") == "reject" and (d.get("reason") or "").strip():
            rejects.append(d)

    print(f"Found {len(rejects)} reject entries with non-empty reasons.")
    if not rejects:
        return 0

    indexed = [(i, r["reason"]) for i, r in enumerate(rejects)]
    clusters = _cluster(indexed, threshold=args.threshold)
    big = [c for c in clusters if len(c) >= args.min_cluster_size]
    print(f"{len(clusters)} clusters total; {len(big)} ≥ "
           f"{args.min_cluster_size} entries.")

    digest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "n_rejects":     len(rejects),
        "n_clusters":    len(clusters),
        "n_significant": len(big),
        "clusters":      [],
    }
    for cluster_idx, cluster in enumerate(big):
        entries = [rejects[i] for i in cluster]
        digest["clusters"].append({
            "cluster_index": cluster_idx,
            **_suggest_remediation(entries),
        })

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(digest, indent=2, default=str),
                          encoding="utf-8")
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}")
    print()
    for c in digest["clusters"][:5]:
        print(f"--- Cluster ({c['n_entries']} entries) ---")
        print(f"  ops: {list(c['ops_used'].keys())[:5]}")
        for s in c["suggestions"]:
            print(f"  → {s}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
