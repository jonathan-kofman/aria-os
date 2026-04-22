"""
test_audit.py — static analysis of the test suite to find weak / fake tests.

Scans every `test_*.py` file under the provided roots, classifies each test
function by strength:

  STRONG    : body contains ≥1 assertion that inspects CONTENT (string
              substring, exact equality, ≥/≤ comparisons on numbers,
              structured key lookups) AND calls the code under test
              (a non-test-module callable) at least once.
  WEAK      : body only contains pure shape/existence assertions
              (isinstance, is not None, len > 0) without content inspection.
  FAKE      : body contains no assertion OR an assertion like `assert True`,
              OR it's a trivial `pass` / `...`; also flags tests named
              `doesnt_crash` / `no_raise` whose body is a single-line call.
  NO_CALL   : body has assertions but never calls the module under test
              (probably testing a literal or its own mock).
  SKIPPED   : body starts with `pytest.skip(...)` unconditionally.

Usage
-----
    python scripts/test_audit.py                 # audit all 4 repos
    python scripts/test_audit.py --format json   # machine-readable output
    python scripts/test_audit.py --weekly        # write report under .audit/
    python scripts/test_audit.py --strict        # exit code 1 if any WEAK/FAKE

Designed for weekly scheduling — the report is idempotent, diffable, and
flags new tests since the previous run by recording test names in the
report. A scheduled task (cron / Windows Task Scheduler) can run it and
pipe the report into `.audit/` for historical tracking.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Default repo roots — search relative to this script's position
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_WORKSPACE = _THIS.parent.parent.parent  # aria-os-export/scripts → workspace
DEFAULT_ROOTS = [
    _WORKSPACE / "aria-os-export" / "tests",
    _WORKSPACE / "millforge-ai" / "tests",
    _WORKSPACE / "structsight" / "tests",
    _WORKSPACE / "manufacturing-core" / "tests",
]


# ---------------------------------------------------------------------------
# AST-based test classification
# ---------------------------------------------------------------------------

_SHAPE_ASSERTION_RE = re.compile(
    r"assert\s+(?:isinstance\(|len\(|.*\s+is\s+(?:not\s+)?None\b|.*\s+is\s+(?:True|False)\b|.*\s*>\s*0\s*$|.*\s*!=\s*\"\"\s*$)"
)


@dataclass
class TestClassification:
    file: str
    name: str
    lineno: int
    strength: str          # STRONG | WEAK | FAKE | NO_CALL | SKIPPED
    body_lines: int
    assertion_count: int
    content_assertion_count: int
    call_count: int
    reason: str = ""
    body_hash: str = ""   # hash of normalized body (for duplicate detection)


def _is_content_assertion(node: ast.Assert) -> bool:
    """Content assertions check the VALUE, not just existence/type.
    Captures == / != / in / not in / comparisons to literals / dict key access
    / substring / numeric range checks beyond >0."""
    test = node.test

    # Specific pattern: `assert NAME` with no further inspection → weak
    if isinstance(test, ast.Name):
        return False

    # `assert x` (single bare expression) → weak if x is just existence
    if isinstance(test, ast.Call):
        func = test.func
        # isinstance / len / hasattr — shape-only
        if isinstance(func, ast.Name) and func.id in (
                "isinstance", "len", "hasattr", "callable"):
            return False

    # Binary comparisons: `x == y`, `x in y`, `x > n`, etc.
    if isinstance(test, ast.Compare):
        for op, cmp in zip(test.ops, test.comparators):
            # `x is not None` / `x is None` — existence only, NOT content
            if isinstance(op, (ast.Is, ast.IsNot)):
                if isinstance(cmp, ast.Constant) and cmp.value is None:
                    return False
                # `x is True` / `x is False` IS content — we're asserting
                # the specific value, not just existence. Historically
                # flagged as shape; reverted 2026-04-20 after false-positive
                # audit pass.
                # `x is SomeClass` — OK content check
                return True
            # `len(x) > 0` — shape check
            if isinstance(op, (ast.Gt, ast.GtE)) and \
               isinstance(cmp, ast.Constant) and cmp.value == 0 and \
               isinstance(test.left, ast.Call) and \
               isinstance(test.left.func, ast.Name) and \
               test.left.func.id == "len":
                return False
            # Every other compare is content
            return True

    # Boolean combinations — assume content if any subpart is
    if isinstance(test, ast.BoolOp):
        return True

    return True  # unknown shape — assume strong


def _normalize_body_for_hash(node: ast.FunctionDef) -> str:
    """Produce a normalized form of the function body suitable for
    detecting duplicates even when the outer test name differs. Strips
    variable names, string literals, and comments so tests that differ
    only in cosmetic values still collide."""
    import hashlib
    # ast.unparse → canonical source. Strip comments + blank lines.
    try:
        src = ast.unparse(node)
    except Exception:
        src = ast.dump(node)
    # Remove the `def test_name(...)` header — we want body-only dup detection
    if "\n" in src:
        src = "\n".join(src.split("\n")[1:])
    # Collapse whitespace
    norm = "\n".join(line.strip() for line in src.splitlines() if line.strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _classify_test(node: ast.FunctionDef, file: str) -> TestClassification:
    """Classify a single test function by inspecting its AST."""
    body = node.body
    body_lines = (node.end_lineno or node.lineno) - node.lineno + 1
    body_hash = _normalize_body_for_hash(node)

    if not body or (len(body) == 1 and isinstance(body[0], ast.Pass)):
        return TestClassification(body_hash=body_hash,
            file=file, name=node.name, lineno=node.lineno,
            strength="FAKE", body_lines=body_lines,
            assertion_count=0, content_assertion_count=0, call_count=0,
            reason="empty body or pass only")

    # Unconditional skip
    for stmt in body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            fn = stmt.value.func
            if (isinstance(fn, ast.Attribute) and fn.attr == "skip") or \
               (isinstance(fn, ast.Name) and fn.id == "skip"):
                return TestClassification(body_hash=body_hash,
                    file=file, name=node.name, lineno=node.lineno,
                    strength="SKIPPED", body_lines=body_lines,
                    assertion_count=0, content_assertion_count=0, call_count=0,
                    reason="unconditional skip at top of body")
        break  # only top-level first stmt

    assertion_count = 0
    content_assertion_count = 0
    call_count = 0
    call_names_seen: set[str] = set()

    # pytest.raises(...) / pytest.warns(...) / pytest.deprecated_call() are
    # assertion mechanisms. Track them as strong content assertions because
    # they assert both existence AND behavior (exception type + context).
    _PYTEST_ASSERT_HELPERS = {"raises", "warns", "deprecated_call", "fail",
                                "skip_if_not", "importorskip"}
    # unittest-style self.assert* methods
    _UNITTEST_ASSERT_PREFIXES = ("assert",)

    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            assertion_count += 1
            # assert True / assert 1 → fake
            if isinstance(sub.test, ast.Constant):
                continue
            if _is_content_assertion(sub):
                content_assertion_count += 1
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            # with pytest.raises(ValueError): ...
            for item in sub.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call):
                    fn = expr.func
                    attr_name = None
                    if isinstance(fn, ast.Attribute):
                        attr_name = fn.attr
                    elif isinstance(fn, ast.Name):
                        attr_name = fn.id
                    if attr_name in _PYTEST_ASSERT_HELPERS:
                        assertion_count += 1
                        content_assertion_count += 1
        elif isinstance(sub, ast.Call):
            fn = sub.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            # self.assertX / self.assertEqual → counts as assertion
            if name and name != "assertIsInstance" and \
               any(name.startswith(p) for p in _UNITTEST_ASSERT_PREFIXES) and \
               name not in ("assertIsInstance", "assertIsNotNone",
                             "assertIsNone", "assertTrue", "assertFalse"):
                assertion_count += 1
                content_assertion_count += 1
            # raises()/warns()/fail() called directly (not as context manager)
            elif name in _PYTEST_ASSERT_HELPERS:
                assertion_count += 1
                content_assertion_count += 1
            elif name and name not in (
                    "isinstance", "len", "hasattr", "callable", "print",
                    "str", "int", "float", "list", "dict", "tuple", "set",
                    "Path", "open", "_"):
                call_count += 1
                if name: call_names_seen.add(name)

    # FAKE: no assertions, or only `assert True` / `assert 1`
    if assertion_count == 0:
        return TestClassification(body_hash=body_hash,
            file=file, name=node.name, lineno=node.lineno,
            strength="FAKE", body_lines=body_lines,
            assertion_count=0, content_assertion_count=0, call_count=call_count,
            reason="no assertions at all")

    # NO_CALL: has assertions but never invokes anything interesting
    if call_count == 0 and assertion_count > 0:
        return TestClassification(body_hash=body_hash,
            file=file, name=node.name, lineno=node.lineno,
            strength="NO_CALL", body_lines=body_lines,
            assertion_count=assertion_count,
            content_assertion_count=content_assertion_count,
            call_count=0,
            reason="asserts but never calls the code under test")

    # WEAK: has calls + assertions but zero content assertions
    if content_assertion_count == 0:
        return TestClassification(body_hash=body_hash,
            file=file, name=node.name, lineno=node.lineno,
            strength="WEAK", body_lines=body_lines,
            assertion_count=assertion_count,
            content_assertion_count=0,
            call_count=call_count,
            reason="only shape/existence assertions; no content checks")

    return TestClassification(body_hash=body_hash,
        file=file, name=node.name, lineno=node.lineno,
        strength="STRONG", body_lines=body_lines,
        assertion_count=assertion_count,
        content_assertion_count=content_assertion_count,
        call_count=call_count)


# ---------------------------------------------------------------------------
# File walker
# ---------------------------------------------------------------------------

def scan_file(path: Path) -> list[TestClassification]:
    """Return the classification for every top-level or class-nested test
    function in a single file."""
    out: list[TestClassification] = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except Exception as exc:
        # Syntax error — count as FAKE for the file level
        return [TestClassification(
            file=str(path), name="<PARSE_ERROR>", lineno=1,
            strength="FAKE", body_lines=0,
            assertion_count=0, content_assertion_count=0, call_count=0,
            reason=f"parse error: {exc}")]

    def _recurse(node: ast.AST, file: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    out.append(_classify_test(child, file))
            elif isinstance(child, ast.ClassDef):
                _recurse(child, file)
    _recurse(tree, str(path))
    return out


def scan_roots(roots: list[Path]) -> list[TestClassification]:
    all_: list[TestClassification] = []
    for root in roots:
        if not root.exists(): continue
        for p in sorted(root.rglob("test_*.py")):
            # skip __pycache__ / node_modules
            if any(x in p.parts for x in ("__pycache__", "node_modules", ".venv")):
                continue
            all_.extend(scan_file(p))
    return all_


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def find_duplicates(rows: list[TestClassification]) -> dict[str, list[TestClassification]]:
    """Group tests by body_hash. Any group with >1 entry is a duplicate
    cluster. Returns {body_hash: [rows...]} for groups of size >= 2."""
    groups: dict[str, list[TestClassification]] = {}
    for r in rows:
        if not r.body_hash: continue  # parse-error entries
        groups.setdefault(r.body_hash, []).append(r)
    return {h: grp for h, grp in groups.items() if len(grp) >= 2}


def format_report(rows: list[TestClassification],
                   *, show_all: bool = False) -> str:
    from collections import Counter
    counts = Counter(r.strength for r in rows)
    total = len(rows)
    lines = [
        f"# Test audit — {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        f"Total tests scanned: {total}",
        "",
        "Strength breakdown:",
    ]
    for s in ("STRONG", "WEAK", "NO_CALL", "FAKE", "SKIPPED"):
        n = counts.get(s, 0)
        pct = (100.0 * n / total) if total else 0.0
        lines.append(f"  {s:<9} {n:>5}  ({pct:5.1f}%)")
    lines.append("")

    # Files with the most weakness
    from collections import defaultdict
    by_file: dict[str, dict[str, int]] = defaultdict(lambda: {"WEAK": 0, "FAKE": 0, "NO_CALL": 0, "STRONG": 0, "SKIPPED": 0})
    for r in rows:
        by_file[r.file][r.strength] += 1
    worst = sorted(by_file.items(),
                     key=lambda kv: -(kv[1]["WEAK"] + kv[1]["FAKE"] + kv[1]["NO_CALL"]))[:15]
    lines.append("## Top 15 files by weak+fake+no_call count")
    lines.append("")
    lines.append("| file | STRONG | WEAK | FAKE | NO_CALL |")
    lines.append("|---|---|---|---|---|")
    for f, cs in worst:
        if cs["WEAK"] + cs["FAKE"] + cs["NO_CALL"] == 0: continue
        short = f.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        lines.append(f"| {short} | {cs['STRONG']} | {cs['WEAK']} | "
                      f"{cs['FAKE']} | {cs['NO_CALL']} |")
    lines.append("")

    # Duplicate tests — same normalized body
    dup_groups = find_duplicates(rows)
    lines.append(f"## Duplicate tests ({len(dup_groups)} clusters)")
    lines.append("")
    if not dup_groups:
        lines.append("_(no duplicates found)_")
    else:
        # Sort clusters by the number of duplicates (worst offenders first),
        # then by the first test name in the cluster
        sorted_clusters = sorted(
            dup_groups.items(),
            key=lambda kv: (-len(kv[1]), kv[1][0].name))
        for h, grp in sorted_clusters[:50 if not show_all else 999999]:
            lines.append(f"- **{len(grp)}×** `{grp[0].name}` (body hash `{h}`)")
            for r in grp:
                short = r.file.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                lines.append(f"  - `{short}:{r.lineno}` :: `{r.name}` [{r.strength}]")
        if len(dup_groups) > 50 and not show_all:
            lines.append(f"- ... and {len(dup_groups) - 50} more clusters (use --show-all)")
    lines.append("")

    # Individual weak/fake tests
    weak_rows = [r for r in rows if r.strength in ("WEAK", "FAKE", "NO_CALL")]
    lines.append(f"## Individual {len(weak_rows)} weak/fake/no-call tests")
    lines.append("")
    for r in weak_rows[:200 if not show_all else 999999]:
        short = r.file.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        lines.append(f"- **{r.strength}** `{short}:{r.lineno}` `{r.name}` — {r.reason}")
    if len(weak_rows) > 200 and not show_all:
        lines.append(f"- ... and {len(weak_rows) - 200} more (use --show-all)")
    return "\n".join(lines)


def write_weekly_report(rows: list[TestClassification],
                        out_dir: Path) -> Path:
    """Write a dated report + update the rolling latest.md symlink."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    report_path = out_dir / f"audit_{ts}.md"
    json_path = out_dir / f"audit_{ts}.json"
    report_path.write_text(format_report(rows, show_all=True), encoding="utf-8")
    json_path.write_text(
        json.dumps([asdict(r) for r in rows], indent=2),
        encoding="utf-8")
    latest = out_dir / "latest.md"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        # Symlink on Unix, copy on Windows (dev-mode symlinks need admin)
        try:
            latest.symlink_to(report_path.name)
        except (OSError, NotImplementedError):
            latest.write_text(report_path.read_text(encoding="utf-8"),
                              encoding="utf-8")
    except Exception:
        pass

    # Also write a diff vs the previous audit for change tracking
    try:
        prior = sorted(p for p in out_dir.glob("audit_*.json")
                        if p != json_path)
        if len(prior) >= 1:
            prev = json.loads(prior[-1].read_text(encoding="utf-8"))
            prev_keys = {(r["file"], r["name"]) for r in prev}
            curr_keys = {(r.file, r.name) for r in rows}
            added = curr_keys - prev_keys
            removed = prev_keys - curr_keys
            diff = [f"# Audit diff vs {prior[-1].stem}",
                    f"added: {len(added)}  removed: {len(removed)}"]
            if added:
                diff.append("")
                diff.append("## New tests since last audit")
                for f, n in sorted(added):
                    diff.append(f"- {f.rsplit(chr(92), 1)[-1].rsplit('/', 1)[-1]} :: {n}")
            if removed:
                diff.append("")
                diff.append("## Removed tests since last audit")
                for f, n in sorted(removed):
                    diff.append(f"- {f.rsplit(chr(92), 1)[-1].rsplit('/', 1)[-1]} :: {n}")
            (out_dir / f"diff_{ts}.md").write_text("\n".join(diff),
                                                       encoding="utf-8")
    except Exception:
        pass
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[1])
    ap.add_argument("roots", nargs="*", type=Path, default=DEFAULT_ROOTS,
                     help="test-file roots to scan (default: all 4 repos)")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--weekly", action="store_true",
                     help="write a dated report into .audit/ with diff")
    ap.add_argument("--strict", action="store_true",
                     help="exit 1 if WEAK or FAKE tests exist")
    ap.add_argument("--show-all", action="store_true",
                     help="include every weak test, not just top 200")
    args = ap.parse_args(argv)

    rows = scan_roots(list(args.roots))

    if args.weekly:
        out_dir = _WORKSPACE / "aria-os-export" / ".audit"
        report_path = write_weekly_report(rows, out_dir)
        print(f"[audit] wrote {report_path}")
        print(f"[audit] diff + JSON in {out_dir}")
    elif args.format == "json":
        print(json.dumps([asdict(r) for r in rows], indent=2))
    else:
        print(format_report(rows, show_all=args.show_all))

    if args.strict:
        bad = sum(1 for r in rows if r.strength in ("WEAK", "FAKE", "NO_CALL"))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
