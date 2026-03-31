"""Memory consolidation system for ARIA-OS part generation history.

Agents read consolidated knowledge before generating. The consolidation
runner compacts raw generation logs into structured knowledge files.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_MEMORY_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "memory"
_LOCK_FILE = _MEMORY_DIR / ".consolidation_lock"
_LAST_FILE = _MEMORY_DIR / ".last_consolidation"
_INDEX_FILE = _MEMORY_DIR / "INDEX.md"

# Memory files
GEOMETRY_PATTERNS = _MEMORY_DIR / "geometry_patterns.md"
VALIDATION_LESSONS = _MEMORY_DIR / "validation_lessons.md"
MATERIAL_KNOWLEDGE = _MEMORY_DIR / "material_knowledge.md"
MACHINE_PREFERENCES = _MEMORY_DIR / "machine_preferences.md"

# Three-gate thresholds
_TIME_GATE_HOURS = 24
_ACTIVITY_GATE_JOBS = 5


def ensure_memory_dir() -> None:
    """Create memory directory and seed files if they don't exist."""
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    for f in (GEOMETRY_PATTERNS, VALIDATION_LESSONS, MATERIAL_KNOWLEDGE, MACHINE_PREFERENCES):
        if not f.exists():
            f.write_text(f"# {f.stem.replace('_', ' ').title()}\n\nNo data yet.\n", encoding="utf-8")
    if not _INDEX_FILE.exists():
        _INDEX_FILE.write_text(
            "# ARIA-OS Memory Index\n\n"
            "- [Geometry Patterns](geometry_patterns.md)\n"
            "- [Validation Lessons](validation_lessons.md)\n"
            "- [Material Knowledge](material_knowledge.md)\n"
            "- [Machine Preferences](machine_preferences.md)\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Memory reader — used by agents
# ---------------------------------------------------------------------------

def read_memory(filename: str, tags: list[str] | None = None, max_chars: int = 2000) -> str:
    """Read a memory file. Optionally filter sections by tags."""
    ensure_memory_dir()
    path = _MEMORY_DIR / filename
    if not path.exists():
        return ""

    content = path.read_text(encoding="utf-8")
    if not tags:
        return content[:max_chars]

    # Filter: keep only sections whose header contains any of the tags
    sections = content.split("\n## ")
    relevant = []
    for section in sections:
        header = section.split("\n")[0].lower()
        if any(tag.lower() in header for tag in tags):
            relevant.append(section)

    result = "\n## ".join(relevant)
    return result[:max_chars] if result else content[:max_chars // 2]


def search_memory(query: str, max_chars: int = 1500) -> str:
    """Search all memory files for content matching query keywords."""
    ensure_memory_dir()
    keywords = [w.lower() for w in query.split() if len(w) > 2]
    results = []

    for f in (GEOMETRY_PATTERNS, VALIDATION_LESSONS, MATERIAL_KNOWLEDGE, MACHINE_PREFERENCES):
        if not f.exists():
            continue
        content = f.read_text(encoding="utf-8")
        # Score each line by keyword matches
        for line in content.split("\n"):
            score = sum(1 for kw in keywords if kw in line.lower())
            if score >= 2:
                results.append((score, f.stem, line.strip()))

    results.sort(key=lambda x: -x[0])
    text = "\n".join(f"[{src}] {line}" for _, src, line in results[:15])
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Memory writer — called after generation runs
# ---------------------------------------------------------------------------

def record_generation(
    part_type: str,
    material: str,
    params: dict[str, Any],
    passed: bool,
    failures: list[str],
    bbox: dict[str, float] | None = None,
    cam_data: dict[str, Any] | None = None,
) -> None:
    """Record a generation outcome to the raw log for later consolidation."""
    ensure_memory_dir()
    log_path = _MEMORY_DIR / "raw_log.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "part_type": part_type,
        "material": material,
        "params": params,
        "passed": passed,
        "failures": failures,
        "bbox": bbox,
        "cam_data": cam_data,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Three-gate consolidation trigger
# ---------------------------------------------------------------------------

def should_consolidate() -> bool:
    """Check the three gates: time, activity, lock."""
    ensure_memory_dir()

    # Gate 1: Lock — another consolidation is running
    if _LOCK_FILE.exists():
        lock_age = time.time() - _LOCK_FILE.stat().st_mtime
        if lock_age < 3600:  # stale lock = 1 hour
            return False
        # Stale lock — remove it
        _LOCK_FILE.unlink(missing_ok=True)

    # Gate 2: Time — 24 hours since last consolidation
    if _LAST_FILE.exists():
        last_time = _LAST_FILE.stat().st_mtime
        hours_since = (time.time() - last_time) / 3600
        if hours_since < _TIME_GATE_HOURS:
            return False

    # Gate 3: Activity — 5+ new jobs since last consolidation
    log_path = _MEMORY_DIR / "raw_log.jsonl"
    if not log_path.exists():
        return False
    n_entries = sum(1 for _ in open(log_path, encoding="utf-8"))
    return n_entries >= _ACTIVITY_GATE_JOBS


def consolidate() -> None:
    """Run memory consolidation — compact raw logs into knowledge files."""
    ensure_memory_dir()
    log_path = _MEMORY_DIR / "raw_log.jsonl"
    if not log_path.exists():
        return

    # Acquire lock
    _LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")

    try:
        entries = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if not entries:
            return

        # Consolidate geometry patterns
        _consolidate_geometry(entries)
        # Consolidate validation lessons
        _consolidate_validation(entries)
        # Consolidate material knowledge
        _consolidate_materials(entries)
        # Consolidate machine preferences
        _consolidate_machines(entries)

        # Mark consolidation complete
        _LAST_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")

        # Archive raw log
        archive = _MEMORY_DIR / f"raw_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        log_path.rename(archive)

        print(f"  [MEMORY] Consolidated {len(entries)} entries into knowledge files")

    finally:
        _LOCK_FILE.unlink(missing_ok=True)


def _consolidate_geometry(entries: list[dict]) -> None:
    """Extract geometry patterns from successful generations."""
    patterns: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("passed"):
            pt = e.get("part_type", "unknown")
            patterns.setdefault(pt, []).append({
                "params": e.get("params", {}),
                "bbox": e.get("bbox"),
            })

    lines = ["# Geometry Patterns\n", "Learned from successful part generations.\n"]
    for pt, examples in sorted(patterns.items()):
        lines.append(f"\n## {pt}\n")
        for ex in examples[-5:]:  # keep last 5 per type
            params = ex.get("params", {})
            bb = ex.get("bbox", {})
            dims = ", ".join(f"{k}={v}" for k, v in params.items()
                            if isinstance(v, (int, float)) and v > 0)
            lines.append(f"- Params: {dims}")
            if bb:
                lines.append(f"  Bbox: {bb.get('x', 0):.1f} x {bb.get('y', 0):.1f} x {bb.get('z', 0):.1f}")

    GEOMETRY_PATTERNS.write_text("\n".join(lines), encoding="utf-8")


def _consolidate_validation(entries: list[dict]) -> None:
    """Extract common failure patterns and fixes."""
    failures: dict[str, int] = {}
    for e in entries:
        for f in e.get("failures", []):
            # Normalize failure message
            key = f.strip()[:100]
            failures[key] = failures.get(key, 0) + 1

    lines = ["# Validation Lessons\n", "Common failures encountered during generation.\n"]
    for msg, count in sorted(failures.items(), key=lambda x: -x[1])[:20]:
        lines.append(f"\n- **{count}x**: {msg}")

    VALIDATION_LESSONS.write_text("\n".join(lines), encoding="utf-8")


def _consolidate_materials(entries: list[dict]) -> None:
    """Extract material-specific constraints."""
    mat_data: dict[str, dict] = {}
    for e in entries:
        mat = e.get("material", "")
        if not mat:
            continue
        mat_data.setdefault(mat, {"passes": 0, "fails": 0, "params": []})
        if e.get("passed"):
            mat_data[mat]["passes"] += 1
            mat_data[mat]["params"].append(e.get("params", {}))
        else:
            mat_data[mat]["fails"] += 1

    lines = ["# Material Knowledge\n", "Material-specific manufacturing data.\n"]
    for mat, data in sorted(mat_data.items()):
        rate = data["passes"] / max(data["passes"] + data["fails"], 1) * 100
        lines.append(f"\n## {mat}")
        lines.append(f"- Success rate: {rate:.0f}% ({data['passes']} pass / {data['fails']} fail)")

    MATERIAL_KNOWLEDGE.write_text("\n".join(lines), encoding="utf-8")


def _consolidate_machines(entries: list[dict]) -> None:
    """Extract machine-specific preferences from CAM data."""
    machine_data: dict[str, list[dict]] = {}
    for e in entries:
        cam = e.get("cam_data")
        if not cam:
            continue
        machine = cam.get("machine", "unknown")
        machine_data.setdefault(machine, []).append(cam)

    lines = ["# Machine Preferences\n", "Optimal parameters learned from CAM runs.\n"]
    for machine, runs in sorted(machine_data.items()):
        lines.append(f"\n## {machine}")
        lines.append(f"- {len(runs)} runs recorded")

    MACHINE_PREFERENCES.write_text("\n".join(lines), encoding="utf-8")
