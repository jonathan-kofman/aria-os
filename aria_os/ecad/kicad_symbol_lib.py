"""
Real KiCad symbol library lookup — walks KiCad's bundled symbols/ dir,
indexes every `.kicad_sym` library, and maps BOM component values
(STM32F405, MPU6000, ATmega328, etc.) to real symbols with proper pin
electrical types.

Why: the schematic writer (kicad_sch_writer.py) emits generic N-pin
rectangles with all pins typed `passive`. ERC with all-passive pins
catches typos but NOT real electrical issues (missing power flags,
input with two drivers, power pin on a signal net).

This module unlocks:
  - real ERC that catches electrical mistakes
  - schematic symbols a pro would recognize (STM32 with labeled ports)
  - footprint association so PCB footprints match the schematic pin map

Scope
-----
- Index is built once per KiCad install, cached to
  outputs/.cache/kicad_symbol_index.json
- Fuzzy value match: "STM32F405RGT6" → "STM32F405RGTx"
- Falls back to `None` for unknown values; caller uses generic symbol

Usage
-----
    from aria_os.ecad.kicad_symbol_lib import lookup_symbol, index_libs

    sym = lookup_symbol("STM32F405RGT6")
    # sym = {
    #   "lib_path": "C:\\...\\MCU_ST_STM32F4.kicad_sym",
    #   "lib_name": "MCU_ST_STM32F4",
    #   "symbol_name": "STM32F405RGTx",
    #   "pins": [{"number":"1","name":"VBAT","etype":"power_in",
    #             "x":-12.7,"y":30.48,"rot":0}, ...],
    # }
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .drc_check import kicad_share_dir


_CACHE_PATH_ENV = "ARIA_KICAD_SYMBOL_INDEX"


def _cache_path() -> Path:
    override = os.environ.get(_CACHE_PATH_ENV)
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parent.parent.parent
    d = repo_root / "outputs" / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "kicad_symbol_index.json"


def _find_symbols_dir() -> str | None:
    share = kicad_share_dir()
    if share is None:
        return None
    candidate = os.path.join(share, "symbols")
    return candidate if os.path.isdir(candidate) else None


# --------------------------------------------------------------------------- #
# Tokenising s-expressions — same tolerant parser pattern as diy_fab.py
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+')


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _parse_sexpr(tokens, i: int = 0):
    """Recursive-descent parse; returns (node, next_index)."""
    if tokens[i] == "(":
        lst = []
        i += 1
        while tokens[i] != ")":
            child, i = _parse_sexpr(tokens, i)
            lst.append(child)
        return lst, i + 1
    tok = tokens[i]
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1], i + 1
    return tok, i + 1


# --------------------------------------------------------------------------- #
# Index builder
# --------------------------------------------------------------------------- #

def index_libs(*, force: bool = False) -> dict:
    """Scan KiCad's symbols/ dir, build value→(lib,symbol) index. Cached.

    Returns {"symbols_dir": str, "libs": {lib_name: [symbol_name, ...]},
             "by_value": {normalized_value: {"lib": ..., "sym": ...}}}
    """
    cache = _cache_path()
    if cache.is_file() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("_version") == 1:
                return data
        except Exception:
            pass

    sdir = _find_symbols_dir()
    if sdir is None:
        return {"_version": 1, "symbols_dir": None,
                "libs": {}, "by_value": {}}

    libs: dict[str, list[str]] = {}
    by_value: dict[str, dict] = {}

    for fname in sorted(os.listdir(sdir)):
        if not fname.endswith(".kicad_sym"):
            continue
        lib_name = fname[:-len(".kicad_sym")]
        lib_path = os.path.join(sdir, fname)
        try:
            # Grab symbol names without fully parsing each body — cheap regex
            with open(lib_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            names = re.findall(r'\(symbol\s+"([^"]+)"', text)
            # Strip unit suffixes: "STM32F405RGTx_1_1" → "STM32F405RGTx"
            top_level = sorted({re.sub(r"_\d+_\d+$", "", n) for n in names})
            libs[lib_name] = top_level
            for n in top_level:
                by_value[_normalize_value(n)] = {
                    "lib": lib_name, "sym": n, "path": lib_path,
                }
        except Exception:
            continue

    data = {
        "_version": 1,
        "symbols_dir": sdir,
        "libs": libs,
        "by_value": by_value,
    }
    try:
        cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return data


def _normalize_value(v: str) -> str:
    """Normalize a component value for fuzzy matching.
    Produces multiple candidate keys to improve hit rate — the lookup
    tries each in turn.
    """
    v = v.upper().replace("_", "").replace(" ", "")
    # Strip KiCad's 'x' placeholder and any hyphens
    v = v.replace("-", "").rstrip("X")
    return v


def _candidate_keys(v: str) -> list[str]:
    """Return a list of progressively less-specific lookup keys."""
    base = _normalize_value(v)
    keys = [base]
    # Also try progressively stripping trailing package/temp codes
    stripped = base
    # Strip common package suffixes: -PU, -PDIP, -TSSOP, -SOIC8
    for _ in range(4):
        if len(stripped) <= 5:
            break
        new = re.sub(r"(P|N)U?$", "", stripped)  # -PU
        new = re.sub(r"PDIP$|TSSOP$|SOIC\d*$|LQFP$|QFN\d*$", "", new)
        new = re.sub(r"[0-9]$", "", new)
        if new == stripped:
            break
        stripped = new
        if len(stripped) >= 5:
            keys.append(stripped)
    return keys


# --------------------------------------------------------------------------- #
# Symbol resolver
# --------------------------------------------------------------------------- #

def lookup_symbol(value: str, *, idx: dict | None = None) -> dict | None:
    """Map a BOM component value → real KiCad symbol with pins.
    Returns None if no match.
    """
    if idx is None:
        idx = index_libs()
    if not idx.get("by_value"):
        return None

    # Try each candidate key from most-specific to least
    keys = _candidate_keys(value)
    hit = None
    for k in keys:
        if k in idx["by_value"]:
            hit = idx["by_value"][k]
            break
    # Prefix fallback on the most-specific key, bidirectional
    if hit is None and keys:
        norm = keys[0]
        best_score = 0
        for k, v in idx["by_value"].items():
            if len(k) < 5 or len(norm) < 5:
                continue
            # Score = length of common prefix (both ways)
            n = min(len(k), len(norm))
            match_len = 0
            for i in range(n):
                if k[i] == norm[i]:
                    match_len += 1
                else:
                    break
            if match_len >= 6 and match_len > best_score:
                best_score, hit = match_len, v
    if hit is None:
        return None

    pins = _load_symbol_pins(hit["path"], hit["sym"])
    if not pins:
        return None
    return {
        "lib_path": hit["path"],
        "lib_name": hit["lib"],
        "symbol_name": hit["sym"],
        "pins": pins,
    }


_EXTENDS_RE = re.compile(r'\(extends\s+"([^"]+)"\)')
_PIN_RE = re.compile(
    r'\(pin\s+(\w+)\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
    r'\s*\(length\s+([-\d.]+)\)'
    r'[\s\S]*?\(name\s+"([^"]*)"'
    r'[\s\S]*?\(number\s+"([^"]*)"')


def _extract_symbol_block(text: str, sym_name: str) -> str | None:
    """Return the (symbol "sym_name" ...) block, or None if missing."""
    key = f'(symbol "{sym_name}"'
    i = text.find(key)
    if i < 0:
        return None
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    return text[i:j]


def _load_symbol_pins(lib_path: str, sym_name: str,
                      *, _visited: set | None = None) -> list[dict]:
    """Parse pins from a KiCad symbol, following (extends ...) inheritance
    and walking all unit sub-symbols.

    KiCad top-level symbols like "LM358" may:
      - extend another symbol ("(extends \"LM2904\")") whose pins they inherit
      - contain unit sub-symbols (LM358_1_1, LM358_0_1) where pins live
    """
    if _visited is None:
        _visited = set()
    if (lib_path, sym_name) in _visited:
        return []
    _visited.add((lib_path, sym_name))

    try:
        with open(lib_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return []

    block = _extract_symbol_block(text, sym_name)
    if block is None:
        return []

    # Inheritance: if pins are defined on a parent, walk there first.
    # extends refs point into the SAME library file by convention.
    parent_match = _EXTENDS_RE.search(block)
    inherited: list[dict] = []
    if parent_match:
        parent_name = parent_match.group(1)
        inherited = _load_symbol_pins(lib_path, parent_name, _visited=_visited)

    seen_numbers: set[str] = {p["number"] for p in inherited}
    pins: list[dict] = list(inherited)
    for m in _PIN_RE.finditer(block):
        etype, x, y, rot, length, name, number = m.groups()
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        pins.append({
            "number": number,
            "name": name,
            "etype": etype,
            "x": float(x), "y": float(y), "rot": float(rot),
            "length": float(length),
        })
    return pins


# --------------------------------------------------------------------------- #
# CLI: quick index + stats
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys
    data = index_libs(force="--force" in sys.argv)
    if data["symbols_dir"] is None:
        print("KiCad symbols dir not found (is KiCad installed?)")
        raise SystemExit(2)
    print(f"Symbols dir: {data['symbols_dir']}")
    print(f"Libraries:   {len(data['libs'])}")
    total = sum(len(v) for v in data["libs"].values())
    print(f"Symbols:     {total}")
    print(f"By-value:    {len(data['by_value'])}")
    # Spot-check a few probes
    for probe in ("STM32F405RGT6", "MPU6000", "ATmega328P-PU",
                  "LM358", "AMS1117-3.3", "ESP32-WROOM-32",
                  "74HC595", "555", "L298N"):
        r = lookup_symbol(probe, idx=data)
        status = f"{r['lib_name']}:{r['symbol_name']} ({len(r['pins'])} pins)" if r else "-- no match"
        print(f"  {probe:20s} -> {status}")
