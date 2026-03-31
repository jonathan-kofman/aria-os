"""
ecad_variant_runner.py — Circuit variant tester for generated KiCad boards.

Runs multiple board variants through generation + ERC + DRC, then compares
power draw and BOM cost across variants.

Usage:
    from aria_os.ecad_variant_runner import run_variant_study, print_variant_table
    results = run_variant_study("ARIA ESP32 board 80x60mm", variants, repo_root=Path("."))
    print_variant_table(results)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ─── Power-draw lookup (mA per component type) ───────────────────────────────

_POWER_DRAW_MA: dict[str, float] = {
    "esp32":    240,
    "stm32f4":  100,
    "stm32f1":   50,
    "stm32":     50,
    "hx711":      1.5,
    "ams1117":    5,
    "ldo":        5,
    "vesc":     500,
    "led":       20,
    "buzzer":    30,
    "usb":        0,
    "barrel":     0,
    "jst":        0,
    "capacitor":  0,
    "resistor":   0,
}

# ─── Cost lookup (USD per component) ─────────────────────────────────────────

_COMPONENT_COST_USD: dict[str, float] = {
    "esp32":    2.50,
    "stm32f4":  4.20,
    "stm32f1":  1.80,
    "stm32":    1.80,
    "hx711":    1.10,
    "ams1117":  0.15,
    "ldo":      0.25,
    "vesc":    45.00,
    "led":      0.05,
    "buzzer":   0.50,
    "usb":      0.35,
    "barrel":   0.40,
    "jst":      0.15,
    "capacitor": 0.05,
    "resistor":  0.02,
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _match_component(value_lower: str, table: dict[str, Any]) -> str | None:
    """
    Return the first table key that appears as a substring in value_lower.
    Keys are checked in descending length order so more-specific entries
    (e.g. "stm32f4") beat shorter prefixes (e.g. "stm32").
    """
    for key in sorted(table.keys(), key=len, reverse=True):
        if key in value_lower:
            return key
    return None


def _compute_power(components: list[dict]) -> float:
    """Sum mA draw across all BOM components."""
    total = 0.0
    for comp in components:
        val = str(comp.get("value", "")).lower()
        ref = str(comp.get("ref", ""))
        key = _match_component(val, _POWER_DRAW_MA)
        if key is not None:
            total += _POWER_DRAW_MA[key]
        elif ref and ref[0].upper() in ("U", "I"):  # IC / active component
            total += 10.0
    return total


def _compute_cost(components: list[dict]) -> float:
    """Sum USD cost across all BOM components."""
    total = 0.0
    for comp in components:
        val = str(comp.get("value", "")).lower()
        key = _match_component(val, _COMPONENT_COST_USD)
        if key is not None:
            total += _COMPONENT_COST_USD[key]
        else:
            total += 0.50  # unrecognised component default
    return total


def _build_variant_description(base_description: str, variant: dict) -> str:
    """
    Append each variant key/value pair (except "name") to the base description
    as "with {value} {key}".
    """
    parts = [base_description.rstrip()]
    for k, v in variant.items():
        if k == "name":
            continue
        parts.append(f"with {v} {k}")
    return ", ".join(parts)


def _slug(text: str) -> str:
    """Filesystem-safe slug from a description string."""
    txt = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return txt[:48]


# ─── Public API ──────────────────────────────────────────────────────────────

def run_variant_study(
    base_description: str,
    variants: list[dict],
    repo_root: Path,
) -> list[dict]:
    """
    Run each variant through ECAD generation + ERC/DRC, then compute power
    draw and BOM cost.

    Parameters
    ----------
    base_description : str
        Natural-language board description shared by all variants.
    variants : list[dict]
        Each dict must contain at least ``"name"``.  All other keys are appended
        to the base description as ``"with {value} {key}"``.
    repo_root : Path
        Repository root (passed to ecad_generator.generate_ecad via ROOT).

    Returns
    -------
    list[dict]
        One result dict per variant.  On error the dict contains an ``"error"``
        key and both pass flags are ``False``.
    """
    from .ecad_generator import (
        parse_board_dimensions,
        parse_components,
        place_components,
        build_bom,
        extract_firmware_pins,
    )
    from .ecad_validator import run_full_check

    total = len(variants)
    results: list[dict] = []

    fw_pins = extract_firmware_pins(repo_root)

    for i, variant in enumerate(variants):
        name = variant.get("name", f"variant_{i+1}")
        print(f"[VARIANT] Running variant {i+1}/{total}: {name}...")

        try:
            variant_desc = _build_variant_description(base_description, variant)

            # ── Generate (parse + place; no file I/O needed for comparison) ──
            board_w, board_h = parse_board_dimensions(variant_desc)
            components = parse_components(variant_desc)
            place_components(components, board_w, board_h)
            bom = build_bom(components)
            comp_dicts = bom["components"]

            # ── ERC + DRC ──────────────────────────────────────────────────
            check_result = run_full_check(
                variant_desc, comp_dicts, fw_pins, board_w, board_h
            )

            erc_pass = check_result["erc"]["passed"]
            drc_pass = check_result["drc"]["passed"]
            errors   = list(check_result.get("errors",   []))
            warnings = list(check_result.get("warnings", []))

            # ── Metrics ────────────────────────────────────────────────────
            power_ma  = _compute_power(comp_dicts)
            cost_usd  = _compute_cost(comp_dicts)

            results.append({
                "variant_name":    name,
                "description":     variant_desc,
                "erc_pass":        erc_pass,
                "drc_pass":        drc_pass,
                "errors":          errors,
                "warnings":        warnings,
                "power_draw_ma":   power_ma,
                "bom_cost_usd":    cost_usd,
                "component_count": len(comp_dicts),
            })

        except Exception as exc:  # noqa: BLE001
            results.append({
                "variant_name":    name,
                "description":     _build_variant_description(base_description, variant),
                "error":           str(exc),
                "erc_pass":        False,
                "drc_pass":        False,
                "errors":          [str(exc)],
                "warnings":        [],
                "power_draw_ma":   0.0,
                "bom_cost_usd":    0.0,
                "component_count": 0,
            })

    return results


def print_variant_table(results: list[dict]) -> None:
    """
    Print an ASCII comparison table for variant study results.

    Columns: Variant | ERC | DRC | Power (mA) | Cost ($) | Errors | Warnings
    """
    if not results:
        print("[VARIANT] No results to display.")
        return

    # Column widths
    col_variant = max(len("Variant"), max(len(r["variant_name"]) for r in results))
    col_erc     = 6
    col_drc     = 6
    col_power   = 12
    col_cost    = 10
    col_errors  = 8
    col_warn    = 10

    def _sep() -> str:
        return (
            "+-" + "-" * col_variant + "-+-"
            + "-" * col_erc + "-+-"
            + "-" * col_drc + "-+-"
            + "-" * col_power + "-+-"
            + "-" * col_cost + "-+-"
            + "-" * col_errors + "-+-"
            + "-" * col_warn + "-+"
        )

    header = (
        f"| {'Variant':<{col_variant}} "
        f"| {'ERC':^{col_erc}} "
        f"| {'DRC':^{col_drc}} "
        f"| {'Power (mA)':^{col_power}} "
        f"| {'Cost ($)':^{col_cost}} "
        f"| {'Errors':^{col_errors}} "
        f"| {'Warnings':^{col_warn}} |"
    )

    sep = _sep()
    print(sep)
    print(header)
    print(sep)

    valid_results = []

    for r in results:
        erc_sym  = "OK  " if r.get("erc_pass") else "FAIL"
        drc_sym  = "OK  " if r.get("drc_pass") else "FAIL"
        err_count  = len(r.get("errors",   []))
        warn_count = len(r.get("warnings", []))
        power_str  = f"{r.get('power_draw_ma', 0.0):.0f}"
        cost_str   = f"{r.get('bom_cost_usd',  0.0):.2f}"

        print(
            f"| {r['variant_name']:<{col_variant}} "
            f"| {erc_sym:^{col_erc}} "
            f"| {drc_sym:^{col_drc}} "
            f"| {power_str:^{col_power}} "
            f"| {cost_str:^{col_cost}} "
            f"| {err_count:^{col_errors}} "
            f"| {warn_count:^{col_warn}} |"
        )

        if "error" not in r:
            valid_results.append(r)

    print(sep)

    if valid_results:
        best_cost  = min(valid_results, key=lambda x: x["bom_cost_usd"])
        best_power = min(valid_results, key=lambda x: x["power_draw_ma"])
        print(f"\nBest by cost:  {best_cost['variant_name']} (${best_cost['bom_cost_usd']:.2f})")
        print(f"Best by power: {best_power['variant_name']} ({best_power['power_draw_ma']:.0f}mA)")
    else:
        print("\n(All variants encountered errors — no summary available.)")


def save_variant_study(
    results: list[dict],
    board_slug: str,
    repo_root: Path,
) -> Path:
    """
    Persist variant study results as JSON.

    Returns the path to the written file:
        repo_root/outputs/ecad/<board_slug>/variant_study.json
    """
    out_dir = repo_root / "outputs" / "ecad" / board_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "variant_study.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"[VARIANT] Study saved to {out_path}")
    return out_path
