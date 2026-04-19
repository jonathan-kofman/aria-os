"""
Real KiCad footprint library lookup -- walks KiCad's bundled footprints/ dir,
indexes every .pretty/ library, and maps BOM component package hints
(LQFP-64, QFN-32, SOT-23, 0805, etc.) to real .kicad_mod files.

Why: kicad_pcb_writer.py emits minimal placeholder pads (1.0x0.6mm, 2mm pitch
2-row grid). A 64-pin STM32 in a 10mm body produces 205+ DRC violations from
solder-mask bridges and wrong clearances. Real footprints eliminate those.

This module unlocks:
  - correct pad geometry for the actual package (LQFP-64_10x10mm_P0.5mm etc.)
  - proper courtyard / silkscreen / fab layers
  - near-zero DRC violations for matched parts

Scope
-----
- Index is built once per KiCad install, cached to
  outputs/.cache/kicad_footprint_index.json
- Fuzzy package-hint match: "LQFP-64" -> LQFP-64_10x10mm_P0.5mm
- Size-based heuristic fallback: 64-pin 10x10mm part -> search LQFP*10x10*
- Falls back to None for unknown packages; caller uses placeholder pads

Usage
-----
    from aria_os.ecad.kicad_footprint_lib import lookup_footprint, index_footprints

    fp = lookup_footprint("STM32F405RGT6", package="LQFP-64")
    # fp = {
    #   "lib":  "Package_QFP",
    #   "fp":   "LQFP-64_10x10mm_P0.5mm",
    #   "path": "C:\\...\\Package_QFP.pretty\\LQFP-64_10x10mm_P0.5mm.kicad_mod",
    # }

    sexpr = load_footprint_sexpr(fp["path"], fp["fp"])
    # Returns the (footprint ...) block ready to embed in .kicad_pcb;
    # Reference and Value properties are stripped so the caller can set them.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .drc_check import kicad_share_dir


_CACHE_PATH_ENV = "ARIA_KICAD_FOOTPRINT_INDEX"


def _cache_path() -> Path:
    override = os.environ.get(_CACHE_PATH_ENV)
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parent.parent.parent
    d = repo_root / "outputs" / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "kicad_footprint_index.json"


def _find_footprints_dir() -> str | None:
    """Return path to KiCad's bundled footprints/ directory.

    KiCad's share layout: <install>/share/kicad/footprints/
    Contains ~155 .pretty/ directories each holding many .kicad_mod files.
    """
    share = kicad_share_dir()
    if share is None:
        return None
    candidate = os.path.join(share, "footprints")
    return candidate if os.path.isdir(candidate) else None


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

def _normalize_fp_name(name: str) -> str:
    """Normalize a footprint name for index lookup.

    Unlike the symbol normalizer, we preserve the full descriptor because
    package geometry is load-bearing:
      LQFP-64_10x10mm_P0.5mm  !=  LQFP-64_7x7mm_P0.4mm

    Rules:
    - Uppercase
    - Collapse spaces and underscores to nothing
    - Keep hyphens (they delimit pin-count: QFN-32)
    - Keep digits (10x10mm, P0.5mm matter)
    """
    name = name.upper()
    name = name.replace(" ", "").replace("_", "")
    return name


def _candidate_keys(pkg: str) -> list[str]:
    """Return a list of progressively less-specific lookup keys for pkg.

    "LQFP-64_10x10mm_P0.5mm" -> ["LQFP-6410X10MMP0.5MM",
                                   "LQFP-64", "LQFP"]
    "0805"                    -> ["0805", "R0805", "C0805"]
    """
    base = _normalize_fp_name(pkg)
    keys = [base]

    # Strip thermal-pad / ThermalVias suffix (1EP...)
    stripped = re.sub(r"-?1EP.*$", "", base)
    if stripped != base and stripped:
        keys.append(stripped)

    # Strip dimension descriptor: everything from first digit block onward
    # LQFP-6410X10MMP0.5MM -> LQFP-64
    no_dims = re.sub(r"\d+X\d+.*$", "", base).rstrip("-")
    if no_dims and no_dims != base and no_dims != stripped:
        keys.append(no_dims)

    # Strip the pin count entirely for family search: LQFP-64 -> LQFP
    family = re.sub(r"-?\d+$", "", no_dims)
    if family and family != no_dims and len(family) >= 2:
        keys.append(family)

    return keys


# --------------------------------------------------------------------------- #
# Index builder
# --------------------------------------------------------------------------- #

def index_footprints(*, force: bool = False) -> dict:
    """Scan KiCad's footprints/ dir, build name->(.pretty, .kicad_mod) index.

    Returns:
      {
        "_version": 2,
        "footprints_dir": str | None,
        "libs": {lib_name: [fp_name, ...]},
        "by_name": {normalized_name: {"lib": ..., "fp": ..., "path": ...}},
      }

    Expected scale: ~15k footprints across ~155 libs in KiCad 10.
    Building from disk takes ~2s; after that the cache is used.
    """
    cache = _cache_path()
    if cache.is_file() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("_version") == 2:
                return data
        except Exception:
            pass

    fdir = _find_footprints_dir()
    if fdir is None:
        return {"_version": 2, "footprints_dir": None,
                "libs": {}, "by_name": {}}

    libs: dict[str, list[str]] = {}
    by_name: dict[str, dict] = {}

    for entry in sorted(os.listdir(fdir)):
        if not entry.endswith(".pretty"):
            continue
        lib_name = entry[:-len(".pretty")]
        lib_dir = os.path.join(fdir, entry)
        if not os.path.isdir(lib_dir):
            continue
        fp_names: list[str] = []
        try:
            for fname in sorted(os.listdir(lib_dir)):
                if not fname.endswith(".kicad_mod"):
                    continue
                fp_name = fname[:-len(".kicad_mod")]
                fp_path = os.path.join(lib_dir, fname)
                fp_names.append(fp_name)
                norm = _normalize_fp_name(fp_name)
                # First match wins for exact normalized key; prefer shorter
                # (non-thermal-via) variants when a key collision occurs.
                if norm not in by_name:
                    by_name[norm] = {"lib": lib_name, "fp": fp_name,
                                     "path": fp_path}
            libs[lib_name] = fp_names
        except Exception:
            continue

    data = {
        "_version": 2,
        "footprints_dir": fdir,
        "libs": libs,
        "by_name": by_name,
    }
    try:
        cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return data


# --------------------------------------------------------------------------- #
# Package-hint matching
# --------------------------------------------------------------------------- #

# Map common shorthand package names to the canonical KiCad library and a
# prefix pattern.  This covers the most frequent DRC-causing packages and
# ensures the heuristic has a starting lib to search in.
_PKG_HINTS: list[tuple[list[str], str, str]] = [
    # (aliases,                  preferred_lib,         search_prefix)
    (["LQFP-64", "LQFP64"],    "Package_QFP",         "LQFP-64"),
    (["LQFP-48", "LQFP48"],    "Package_QFP",         "LQFP-48"),
    (["LQFP-32", "LQFP32"],    "Package_QFP",         "LQFP-32"),
    (["LQFP-100", "LQFP100"],  "Package_QFP",         "LQFP-100"),
    (["TQFP-32", "TQFP32"],    "Package_QFP",         "TQFP-32"),
    (["TQFP-44", "TQFP44"],    "Package_QFP",         "TQFP-44"),
    (["QFN-32", "QFN32"],      "Package_DFN_QFN",     "QFN-32"),
    (["QFN-24", "QFN24"],      "Package_DFN_QFN",     "QFN-24"),
    (["QFN-16", "QFN16"],      "Package_DFN_QFN",     "QFN-16"),
    (["QFN-48", "QFN48"],      "Package_DFN_QFN",     "QFN-48"),
    (["SOT-23", "SOT23"],      "Package_TO_SOT_SMD",  "SOT-23"),
    (["SOT-23-5", "SOT235"],   "Package_TO_SOT_SMD",  "SOT-23-5"),
    (["SOT-223", "SOT223"],    "Package_TO_SOT_SMD",  "SOT-223"),
    (["TO-92", "TO92"],        "Package_TO_SOT_THT",  "TO-92"),
    (["TO-220", "TO220"],      "Package_TO_SOT_THT",  "TO-220"),
    (["DIP-8", "DIP8"],        "Package_DIP",         "DIP-8"),
    (["DIP-14", "DIP14"],      "Package_DIP",         "DIP-14"),
    (["DIP-28", "DIP28"],      "Package_DIP",         "DIP-28"),
    (["0402"],                 "Resistor_SMD",        "R_0402"),
    (["0603"],                 "Resistor_SMD",        "R_0603"),
    (["0805"],                 "Resistor_SMD",        "R_0805"),
    (["1206"],                 "Resistor_SMD",        "R_1206"),
    (["C0402"],                "Capacitor_SMD",       "C_0402"),
    (["C0603"],                "Capacitor_SMD",       "C_0603"),
    (["C0805"],                "Capacitor_SMD",       "C_0805"),
    (["C1206"],                "Capacitor_SMD",       "C_1206"),
    (["USB-C", "USBC"],        "Connector_USB",       "USB_C_Receptacle"),
    (["USB-A", "USBA"],        "Connector_USB",       "USB_A"),
    (["USB-B", "USBB"],        "Connector_USB",       "USB_B"),
]

# Normalized alias -> (preferred_lib, search_prefix)
_PKG_ALIAS_MAP: dict[str, tuple[str, str]] = {}
for _aliases, _lib, _pfx in _PKG_HINTS:
    for _a in _aliases:
        _PKG_ALIAS_MAP[_normalize_fp_name(_a)] = (_lib, _pfx)


def _first_in_lib(idx: dict, lib_name: str, prefix: str) -> dict | None:
    """Return the first footprint in lib_name whose name starts with prefix.

    Three-pass preference order to pick the most generic variant:
      1. No exposed pad (no "1EP"), no ThermalVias  -- plain package
      2. Has exposed pad (1EP), no ThermalVias      -- exposed-pad variant
      3. Any match including ThermalVias            -- last resort
    """
    fps = idx["libs"].get(lib_name, [])
    norm_pfx = _normalize_fp_name(prefix)
    fdir = idx["footprints_dir"]

    def _make_hit(fp: str) -> dict:
        path = os.path.join(fdir, lib_name + ".pretty", fp + ".kicad_mod")
        return {"lib": lib_name, "fp": fp, "path": path}

    for reject_ep, reject_thermal in [(True, True), (False, True), (False, False)]:
        for fp in fps:
            n = _normalize_fp_name(fp)
            if not n.startswith(norm_pfx):
                continue
            if reject_ep and "1EP" in n:
                continue
            if reject_thermal and "THERMALVIAS" in n:
                continue
            return _make_hit(fp)
    return None


def _heuristic_lookup(idx: dict, pkg: str) -> dict | None:
    """Size/pin-count heuristic when pkg didn't match directly.

    Extracts numeric patterns from pkg and scans by_name for partial match.
    e.g. "LQFP-64 10x10mm" -> search for LQFP-6410X10 in by_name.
    """
    norm = _normalize_fp_name(pkg)
    # Extract leading package family prefix (letters + hyphens before digits)
    family = re.match(r'^([A-Z][A-Z0-9-]+?)-?(\d)', norm)
    if not family:
        return None
    fam_prefix = family.group(1)  # e.g. "LQFP"

    # Find all by_name entries with that family prefix; score by overlap length
    best: dict | None = None
    best_score = 0
    for k, v in idx["by_name"].items():
        if not k.startswith(fam_prefix):
            continue
        # Score: length of common prefix between norm and k
        n = min(len(k), len(norm))
        match_len = 0
        for i in range(n):
            if k[i] == norm[i]:
                match_len += 1
            else:
                break
        if match_len > best_score:
            best_score = match_len
            best = v

    return best if best_score >= 4 else None


def lookup_footprint(value: str, package: str | None = None,
                     *, idx: dict | None = None) -> dict | None:
    """Map a BOM value + optional package hint to a real .kicad_mod entry.

    Resolution order:
    1. Exact normalized-name match in by_name (full descriptor)
    2. Candidate-key cascade (strip dimensions, then family)
    3. Package-alias map -> preferred lib -> search prefix
    4. Heuristic: family prefix longest-prefix scan

    Parameters
    ----------
    value:   BOM value string, e.g. "STM32F405RGT6" or "100nF" or "R_0805".
             If value itself looks like a package, it is treated as one.
    package: optional explicit package hint, e.g. "LQFP-64" or "0805".
             If provided, this is tried first and overrides value-based search.

    Returns
    -------
    {"lib": lib_name, "fp": fp_name, "path": absolute_path} or None.
    """
    if idx is None:
        idx = index_footprints()
    if not idx.get("by_name"):
        return None

    queries = []
    if package:
        queries.append(package)
    queries.append(value)

    for q in queries:
        # 1. Exact normalized match
        norm = _normalize_fp_name(q)
        if norm in idx["by_name"]:
            return idx["by_name"][norm]

        # 2. Candidate key cascade
        for key in _candidate_keys(q):
            if key in idx["by_name"]:
                return idx["by_name"][key]

        # 3. Package-alias map
        norm_q = _normalize_fp_name(q)
        if norm_q in _PKG_ALIAS_MAP:
            pref_lib, search_prefix = _PKG_ALIAS_MAP[norm_q]
            hit = _first_in_lib(idx, pref_lib, search_prefix)
            if hit:
                return hit

        # Also try alias map on the stripped (no-dims) version
        stripped = re.sub(r"\d+X\d+.*$", "", norm_q).rstrip("-")
        if stripped and stripped != norm_q and stripped in _PKG_ALIAS_MAP:
            pref_lib, search_prefix = _PKG_ALIAS_MAP[stripped]
            hit = _first_in_lib(idx, pref_lib, search_prefix)
            if hit:
                return hit

        # 4. Prefix scan across all by_name entries
        hit = _heuristic_lookup(idx, q)
        if hit:
            return hit

    return None


# --------------------------------------------------------------------------- #
# Footprint sexpr loader
# --------------------------------------------------------------------------- #

def load_footprint_sexpr(fp_path: str | Path, fp_name: str) -> str | None:
    """Read a .kicad_mod file and return the (footprint ...) block.

    The "Reference" and "Value" properties set by the library are stripped
    (their (at ...) positions and layer assignments are preserved as blank
    placeholders) so the caller can inject real reference/value strings.

    Returns the raw s-expression string, or None on read error.

    KiCad 10 format note: footprints use (version 20260206) and the top-level
    tag is `(footprint "name" ...)` -- the name attribute must be rewritten
    by the caller to match the board component reference.
    """
    try:
        path = Path(fp_path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Strip the "REF**" and value-echo properties so the PCB writer can set them
    # We replace the string content inside the two canonical properties:
    #   (property "Reference" "REF**" ...)
    #   (property "Value" "R_0805_2012Metric" ...)
    # with empty strings.  The property block stays intact (layer, at, effects).
    text = re.sub(
        r'(\(property\s+"Reference"\s+)"[^"]*"',
        r'\1""',
        text,
    )
    text = re.sub(
        r'(\(property\s+"Value"\s+)"[^"]*"',
        r'\1""',
        text,
    )
    return text


# --------------------------------------------------------------------------- #
# CLI: quick index + stats
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys
    data = index_footprints(force="--force" in sys.argv)
    if data["footprints_dir"] is None:
        print("KiCad footprints dir not found (is KiCad installed?)")
        raise SystemExit(2)
    print(f"Footprints dir: {data['footprints_dir']}")
    print(f"Libraries:      {len(data['libs'])}")
    total = sum(len(v) for v in data["libs"].values())
    print(f"Footprints:     {total}")
    print(f"By-name:        {len(data['by_name'])}")
    print()
    # Spot-check the 6 required probes + a few extras
    probes = [
        ("R_0805 (resistor)",    "R_0805",       None),
        ("C_0603 (capacitor)",   "C_0603",       None),
        ("LQFP-64 (STM32)",      "STM32F405RGT6","LQFP-64"),
        ("QFN-32",               "nRF52832",     "QFN-32"),
        ("SOT-23",               "LM4041",       "SOT-23"),
        ("USB-C receptacle",     "USB-C",        None),
        ("DIP-8",                "NE555",        "DIP-8"),
        ("SOT-223",              "LM1117",       "SOT-223"),
    ]
    for label, val, pkg in probes:
        r = lookup_footprint(val, package=pkg, idx=data)
        if r:
            status = f"{r['lib']}:{r['fp']}"
        else:
            status = "-- no match"
        print(f"  {label:25s} -> {status}")
