"""Auto-router soak-test for ARIA-OS.

Runs 20 mixed prompts through dashboard.aria_server._auto_detect_mode,
cross-checks against expected routing buckets, and reports accuracy +
mismatches + recommended marker-list additions.

Routing-correctness audit only — does NOT touch SW/KiCad or run the
full pipeline. Just instantiates FullBuildRequest for the system-mode
prompts to confirm the branch is reachable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "dashboard"))

from dashboard.aria_server import (              # noqa: E402
    _auto_detect_mode, FullBuildRequest, _MCAD_MARKERS, _ECAD_MARKERS)


class Case(NamedTuple):
    prompt: str
    expected: str
    note: str = ""


CASES: list[Case] = [
    # native (5)
    Case("aluminium L-bracket 80mm wide 60mm tall 6mm thick", "native"),
    Case("stepped shaft 200mm long, 20mm dia center, 12mm dia ends, keyway",
         "native"),
    Case("ratchet ring 8 teeth aluminium 60mm OD", "native",
         "adversarial: 'ring' must not pull to system"),
    Case("centrifugal impeller 90mm OD, 6 backward-swept blades, "
         "20mm bore", "native"),
    Case("PCB drill bit holder, machined steel, 50x50x30mm",
         "native", "adversarial: 'PCB' is part of a machined-fixture name"),

    # kicad (4)
    Case("led indicator board, 2 layer kicad pcb, 30x20mm", "kicad"),
    Case("ESP32-S3 dev board layout with USB-C and 4 status LEDs",
         "kicad", "no enclosure / no mech body"),
    Case("4-layer flight controller PCB with IMU and barometer",
         "kicad"),
    Case("buck converter PCB schematic, 5V to 3.3V, 1A", "kicad"),

    # system (5) — combined MCAD + ECAD
    Case("100x60x25mm IoT enclosure 6061 aluminium with USB-C cutout, "
         "4 M3 mounting bosses for ESP32-S3 dev board, and a matching "
         "50x35mm 2-layer PCB", "system"),
    Case("250mm carbon fibre drone frame with 4 motor mounts and "
         "matching 36x36mm flight controller PCB", "system"),
    Case("robot arm joint housing with integrated motor driver board "
         "and 3-pin servo header", "system"),
    Case("aluminium L-bracket with M3 holes for an Arduino "
         "and matching breakout PCB", "system",
         "adversarial: bracket + arduino"),
    Case("battery pack chassis, 4 cells, with BMS PCB inside", "system"),

    # sheetmetal (2)
    Case("formed sheet metal bracket, 1.5mm steel, 90 degree bend "
         "radius 2mm, 50x40mm", "sheetmetal"),
    Case("flanged enclosure with bend radius 1.5mm, two flanges, "
         "0.8mm aluminium", "sheetmetal"),

    # dwg (2)
    Case("create technical drawing with GD&T for the L-bracket, "
         "datum A on bottom face", "dwg"),
    Case("mechanical drawing with dimensions and tolerances for "
         "the flange plate", "dwg"),

    # asm (2)
    Case("assembly: mate the gear concentric to the shaft and "
         "mount the bracket to the chassis", "asm"),
    Case("sub-assembly mating motor flange coincident with frame "
         "boss, attached to base plate", "asm"),
]


def main() -> int:
    rows: list[dict] = []
    mismatches: list[dict] = []
    surprises: list[str] = []

    print("=" * 76)
    print("ARIA-OS Auto-Router Soak Test")
    print("=" * 76)

    for c in CASES:
        actual = _auto_detect_mode(c.prompt)
        ok = (actual == c.expected)
        rows.append({
            "prompt":   c.prompt,
            "expected": c.expected,
            "actual":   actual,
            "ok":       ok,
            "note":     c.note,
        })
        marker = "OK" if ok else "MISS"
        print(f"  [{marker:4s}] {c.expected:10s} -> {actual:10s}  "
              f"{c.prompt[:60]}")
        if not ok:
            mismatches.append({
                "prompt":   c.prompt,
                "expected": c.expected,
                "actual":   actual,
                "note":     c.note,
                "hypothesis": _diagnose(c.prompt, c.expected, actual),
            })
        if c.note and ok and "adversarial" in c.note:
            surprises.append(f"adversarial PASSED: {c.prompt[:60]}")

    # 3. Confirm full_build branch is reachable for system prompts
    system_branch_ok = True
    try:
        sys_prompts = [r for r in rows if r["expected"] == "system"]
        for r in sys_prompts:
            req = FullBuildRequest(
                goal=r["prompt"],
                mcad_cad="solidworks",
                quality_tier="fast",
                bundle_name="soak_test_dryrun",
            )
            assert req.goal == r["prompt"]
        print(f"\n  full_build branch reachable for {len(sys_prompts)} "
              f"system prompts (FullBuildRequest instantiation OK)")
    except Exception as exc:
        system_branch_ok = False
        print(f"\n  ! FullBuildRequest instantiation FAILED: {exc}")

    # 4. Native prompts: hardcoded planner reachability
    native_results: list[dict] = []
    try:
        from aria_os.native_planner.dispatcher import make_plan
        from aria_os.spec_extractor import extract_spec
    except Exception as exc:
        print(f"\n  ! Could not import dispatcher: {exc}")
        make_plan = None        # type: ignore
        extract_spec = None     # type: ignore

    if make_plan is not None and extract_spec is not None:
        print(f"\n  Hardcoded-only planner check (allow_llm=False):")
        native_prompts = [r for r in rows if r["expected"] == "native"]
        for r in native_prompts:
            try:
                spec = extract_spec(r["prompt"]) or {}
                plan = make_plan(r["prompt"], spec, allow_llm=False)
                native_results.append({
                    "prompt": r["prompt"], "hardcoded": True,
                    "n_ops":  len(plan),
                    "first":  plan[0]["kind"] if plan else None,
                })
                print(f"    [HARD] {len(plan):2d} ops  "
                      f"{r['prompt'][:60]}")
            except NotImplementedError:
                native_results.append({
                    "prompt": r["prompt"], "hardcoded": False,
                    "n_ops":  0, "first": None,
                })
                print(f"    [LLM ] (no hardcoded match) "
                      f"{r['prompt'][:60]}")
            except Exception as exc:
                native_results.append({
                    "prompt": r["prompt"], "hardcoded": False,
                    "error":  str(exc)[:120],
                })
                print(f"    [ERR ] {exc}: {r['prompt'][:60]}")

    # 5. Build per-bucket accuracy
    buckets = sorted(set(c.expected for c in CASES))
    per_bucket = {}
    for b in buckets:
        n = sum(1 for r in rows if r["expected"] == b)
        ok = sum(1 for r in rows if r["expected"] == b and r["ok"])
        per_bucket[b] = (ok, n)

    total_ok = sum(1 for r in rows if r["ok"])
    accuracy = total_ok / len(rows)

    # 6. Markdown report
    out_path = REPO_ROOT / "outputs" / "auto_router_soak.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md: list[str] = [
        "# Auto-Router Soak Test",
        "",
        f"**Total: {total_ok}/{len(rows)} ({accuracy*100:.0f}%)**",
        "",
        "## Per-bucket accuracy", "",
    ]
    for b, (ok, n) in per_bucket.items():
        md.append(f"- **{b}**: {ok}/{n}")
    md.append("")
    md.append("## Mismatches")
    md.append("")
    if not mismatches:
        md.append("_None — every prompt routed to the expected bucket._")
    else:
        for m in mismatches:
            md.append(f"- `{m['prompt'][:90]}`")
            md.append(f"  - expected: **{m['expected']}**, "
                      f"actual: **{m['actual']}**")
            if m["note"]:
                md.append(f"  - note: {m['note']}")
            md.append(f"  - hypothesis: {m['hypothesis']}")
    md.append("")
    md.append("## Edge cases that surprised the router")
    md.append("")
    if surprises:
        for s in surprises:
            md.append(f"- {s}")
    else:
        md.append("_None — adversarials all routed correctly._")
    md.append("")
    md.append("## Native hardcoded-planner reachability")
    md.append("")
    for nr in native_results:
        path = "hardcoded" if nr.get("hardcoded") else "LLM-fallback"
        md.append(f"- [{path}] `{nr['prompt'][:80]}`")
        if nr.get("n_ops"):
            md.append(f"  - {nr['n_ops']} ops, first: `{nr.get('first')}`")
    md.append("")
    md.append("## Recommended marker-list additions")
    md.append("")
    md.extend(_recommendations(mismatches))
    md.append("")
    md.append("## Raw rows")
    md.append("")
    md.append("```json")
    md.append(json.dumps(rows, indent=2))
    md.append("```")
    out_path.write_text("\n".join(md), encoding="utf-8")

    # 6b. 5-line summary
    print("\n" + "=" * 76)
    print(f"SUMMARY:")
    print(f"  total: {total_ok}/{len(rows)}  ({accuracy*100:.0f}%)")
    print(f"  per-bucket: " + ", ".join(f"{b}={ok}/{n}"
                                          for b, (ok, n) in per_bucket.items()))
    print(f"  mismatches: {len(mismatches)}  | system branch: "
          f"{'OK' if system_branch_ok else 'BROKEN'}")
    print(f"  hardcoded-native hits: "
          f"{sum(1 for r in native_results if r.get('hardcoded'))}/"
          f"{len(native_results)}")
    print(f"  report: {out_path.relative_to(REPO_ROOT)}")
    return 0 if accuracy == 1.0 else 1


def _diagnose(prompt: str, expected: str, actual: str) -> str:
    p = prompt.lower()
    has_mcad = any(m in p for m in _MCAD_MARKERS)
    has_ecad = any(m in p for m in _ECAD_MARKERS)
    if expected == "system" and actual != "system":
        if has_mcad and not has_ecad:
            return ("ECAD marker missed — prompt has MCAD but no token "
                    "in _ECAD_MARKERS. Check for arduino/breakout/header.")
        if has_ecad and not has_mcad:
            return ("MCAD marker missed — ECAD detected but no MCAD "
                    "token. Add part-of-speech in _MCAD_MARKERS.")
        return "neither marker family triggered — words too generic"
    if expected == "native" and actual == "system":
        return ("false-positive ECAD marker fired on a mech-only part "
                "(e.g. 'PCB' inside 'PCB drill bit holder')")
    if expected == "native" and actual != "native":
        return ("non-system mode took priority — check sheetmetal/dwg/"
                "asm keyword precedence")
    return f"unexpected drift {expected}->{actual}"


def _recommendations(mismatches: list[dict]) -> list[str]:
    if not mismatches:
        return ["_None — router is well-tuned for this corpus._"]
    out: list[str] = []
    out.append("Suggested additions based on misses:")
    out.append("")
    for m in mismatches:
        if m["expected"] == "system" and m["actual"] != "system":
            p = m["prompt"].lower()
            tokens = [t for t in p.split() if len(t) > 3]
            cand_ecad = [t for t in tokens
                          if any(k in t for k in
                                  ("arduino", "breakout", "driver",
                                    "header", "imu", "bms"))]
            cand_mcad = [t for t in tokens
                          if any(k in t for k in
                                  ("housing", "joint", "pack", "chassis"))]
            if cand_ecad:
                out.append(f"- Add to _ECAD_MARKERS: {sorted(set(cand_ecad))}")
            if cand_mcad:
                out.append(f"- Add to _MCAD_MARKERS: {sorted(set(cand_mcad))}")
        if m["expected"] == "native" and m["actual"] == "system":
            out.append(f"- Tighten _ECAD_MARKERS: '{m['prompt'][:50]}' "
                       f"contains a false-positive ECAD token")
    if len(out) == 2:
        out.append("- (No structural marker fixes needed; misses likely "
                   "from priority ordering in _auto_detect_mode)")
    return out


if __name__ == "__main__":
    sys.exit(main())
