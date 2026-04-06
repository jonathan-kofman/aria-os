"""
quote_agent.py — Instant quoting agent for the ARIA-OS pipeline.

Takes a STEP file + material + process and produces a structured cost estimate.
Uses deterministic parametric models for the base quote, then calls an Ollama
LLM (llama3.1:8b) to review for cost risks and optimization suggestions.

Usage:
    from aria_os.agents.quote_agent import QuoteAgent
    agent = QuoteAgent()
    quote = agent.quote("outputs/cad/step/part.step", material="aluminium_6061",
                        process="cnc", quantity=1)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .base_agent import BaseAgent, _call_ollama, is_ollama_available
from .design_state import DesignState
from .ollama_config import OLLAMA_HOST
from .quote_tools import (
    FINISHING_RATES,
    MACHINE_RATES,
    SETUP_COSTS,
    estimate_lead_time,
    estimate_machining_time,
    estimate_print_time_hr,
    extract_geometry_for_quote,
    get_material_rate,
)

# ─── Process aliases ─────────────────────────────────────────────────────────

_PROCESS_ALIASES: dict[str, str] = {
    "cnc":          "cnc_3axis",
    "cnc3":         "cnc_3axis",
    "cnc_3":        "cnc_3axis",
    "3axis":        "cnc_3axis",
    "cnc4":         "cnc_4axis",
    "cnc_4":        "cnc_4axis",
    "4axis":        "cnc_4axis",
    "cnc5":         "cnc_5axis",
    "cnc_5":        "cnc_5axis",
    "5axis":        "cnc_5axis",
    "turning":      "cnc_turning",
    "lathe":        "cnc_turning",
    "3d_print":     "fdm",
    "3dprint":      "fdm",
    "3d print":     "fdm",
    "fdm":          "fdm",
    "sla":          "sla",
    "sls":          "sls",
    "sheet_metal":  "sheet_metal",
    "sheet metal":  "sheet_metal",
    "sheetmetal":   "sheet_metal",
    "injection_mold": "injection_mold",
    "injection mold": "injection_mold",
    "injection":    "injection_mold",
}

_SYSTEM_PROMPT = """\
You are a manufacturing cost estimation expert. You review parametric cost
estimates for CNC machining, 3D printing, sheet metal, and injection molding.

When given a geometry summary and cost breakdown, you:
1. Identify cost risks the parametric model may have missed (tight tolerances,
   thin walls, deep pockets, special tooling, heat treatment, surface finish).
2. Suggest concrete optimizations with estimated dollar savings.
3. Assess confidence: "high" if the part is straightforward, "medium" if there
   are unknowns, "low" if the geometry is unusual or the process is a poor fit.

Respond in this exact format (no markdown, no extra text):
CONFIDENCE: high|medium|low
RISKS:
- <risk 1>
- <risk 2>
OPTIMIZATIONS:
- <optimization 1 ($X savings)>
- <optimization 2 ($X savings)>
"""


class QuoteAgent:
    """
    Instant quoting agent.

    Deterministic parametric cost model + optional LLM review for edge cases.
    """

    def __init__(self, model: str = "llama3.1:8b"):
        self.model = model

    # ─── Public API ───────────────────────────────────────────────────────

    def quote(
        self,
        step_path: str,
        material: str = "aluminium_6061",
        process: str = "cnc",
        quantity: int = 1,
        finish: str = "as_machined",
        axes: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate an instant quote for a part.

        Parameters
        ----------
        step_path : Path to STEP file.
        material  : Material key (e.g. "aluminium_6061", "steel_4140").
        process   : Process key or alias (e.g. "cnc", "fdm", "sheet_metal").
        quantity  : Number of units.
        finish    : Surface finish (e.g. "as_machined", "anodized", "polished").
        axes      : Override axis count for CNC ("3axis", "4axis", "5axis").
                    Auto-detected from complexity if None.

        Returns structured quote dict.
        """
        # Resolve process alias
        proc = _PROCESS_ALIASES.get(process.lower().replace("-", "_"), process.lower())

        # Auto-detect CNC axis count from geometry complexity
        if proc.startswith("cnc") and proc == "cnc_3axis" and axes is None:
            # Will be updated after geometry extraction
            pass

        # Extract geometry
        geom = extract_geometry_for_quote(step_path)
        bbox = geom["bbox_mm"]
        vol = geom["volume_cm3"]
        sa = geom["surface_area_cm2"]
        complexity = geom["complexity"]
        face_count = geom["face_count"]

        # Auto-select CNC axes from complexity if not specified
        if axes is not None:
            proc = f"cnc_{axes}" if not axes.startswith("cnc_") else axes
        elif proc.startswith("cnc_") and proc != "cnc_turning":
            if complexity == "high":
                proc = "cnc_5axis"
            elif complexity == "medium" and face_count > 30:
                proc = "cnc_4axis"
            # else stays as cnc_3axis

        # Compute stock volume
        stock_vol = (bbox[0] * bbox[1] * bbox[2]) / 1000.0  # mm3 -> cm3
        stock_vol = max(stock_vol, vol * 1.1)  # at least 10% bigger than part
        removal_pct = (1.0 - vol / stock_vol) * 100.0 if stock_vol > 0 else 0.0

        # Get material data
        mat_data = get_material_rate(material)

        # Route to the right cost model
        if proc.startswith("cnc_") or proc == "cnc_turning":
            breakdown = self._cost_cnc(
                proc, vol, stock_vol, sa, mat_data, finish, complexity,
            )
        elif proc in ("fdm", "sla", "sls"):
            breakdown = self._cost_3d_print(proc, vol, sa, mat_data, finish)
        elif proc == "sheet_metal":
            breakdown = self._cost_sheet_metal(bbox, sa, mat_data, finish)
        elif proc == "injection_mold":
            breakdown = self._cost_injection_mold(vol, complexity, quantity, mat_data)
        else:
            breakdown = self._cost_cnc(
                "cnc_3axis", vol, stock_vol, sa, mat_data, finish, complexity,
            )

        # Quantity discount
        qty_discount = 1.0
        if quantity >= 100:
            qty_discount = 0.75
        elif quantity >= 50:
            qty_discount = 0.80
        elif quantity >= 25:
            qty_discount = 0.85
        elif quantity >= 10:
            qty_discount = 0.90
        elif quantity >= 5:
            qty_discount = 0.95

        # Apply margin (20%)
        subtotal = sum(breakdown.values())
        margin = subtotal * 0.20
        unit_cost = (subtotal + margin) * qty_discount

        # Lead time
        lead_days = estimate_lead_time(proc, complexity, quantity)

        # Build quote
        quote_result: dict[str, Any] = {
            "process": proc,
            "material": material,
            "quantity": quantity,
            "unit_cost_usd": round(unit_cost, 2),
            "breakdown": {k: round(v, 2) for k, v in breakdown.items()},
            "lead_time_days": lead_days,
            "geometry": {
                "bbox_mm": [round(b, 2) for b in bbox],
                "volume_cm3": round(vol, 4),
                "stock_volume_cm3": round(stock_vol, 4),
                "material_removal_pct": round(removal_pct, 1),
                "surface_area_cm2": round(sa, 4),
                "face_count": face_count,
            },
            "optimizations": [],
            "confidence": "medium",
        }
        quote_result["breakdown"]["margin_usd"] = round(margin, 2)

        # LLM review for cost risks and optimizations
        llm_review = self._llm_review(quote_result, geom)
        if llm_review:
            quote_result["optimizations"] = llm_review.get("optimizations", [])
            quote_result["confidence"] = llm_review.get("confidence", "medium")
            if llm_review.get("risks"):
                quote_result["risks"] = llm_review["risks"]

        return quote_result

    # ─── Cost models ──────────────────────────────────────────────────────

    def _cost_cnc(
        self,
        process: str,
        part_vol: float,
        stock_vol: float,
        surface_area: float,
        mat_data: dict,
        finish: str,
        complexity: str,
    ) -> dict[str, float]:
        """CNC machining cost breakdown."""
        material_cost = stock_vol * mat_data["rate_per_cm3"]
        setup_cost = SETUP_COSTS.get(process, 75.0)

        # Machining time
        axes_key = process.replace("cnc_", "") if process.startswith("cnc_") else "3axis"
        machining_min = estimate_machining_time(
            part_vol,
            material="steel",  # use category for rate lookup
            axes=axes_key,
            stock_volume_cm3=stock_vol,
        )
        # Adjust for machinability
        machining_min /= max(mat_data.get("machinability", 1.0), 0.1)

        machine_rate = MACHINE_RATES.get(process, 1.50)
        machining_cost = machining_min * machine_rate

        # Finishing
        finish_rate = FINISHING_RATES.get(finish, FINISHING_RATES["as_machined"])
        finishing_cost = surface_area * finish_rate

        return {
            "material_usd": material_cost,
            "machining_usd": machining_cost,
            "setup_usd": setup_cost,
            "finishing_usd": finishing_cost,
        }

    def _cost_3d_print(
        self,
        process: str,
        part_vol: float,
        surface_area: float,
        mat_data: dict,
        finish: str,
    ) -> dict[str, float]:
        """3D printing cost breakdown."""
        # Material cost (filament/resin)
        filament_rates = {"fdm": 0.025, "sla": 0.10, "sls": 0.06}
        material_cost = part_vol * filament_rates.get(process, 0.025)

        # Support factor based on complexity
        support_factor = 0.2
        print_time = estimate_print_time_hr(part_vol, process, support_factor)
        machine_rate_hr = MACHINE_RATES.get(process, 0.083) * 60  # $/hr
        machine_cost = print_time * machine_rate_hr

        # Post-processing
        post_cost = 5.0  # base post-processing (support removal, cleanup)
        if finish != "none" and finish != "as_machined":
            post_cost += surface_area * FINISHING_RATES.get(finish, 0.01)

        setup_cost = SETUP_COSTS.get(process, 5.0)

        return {
            "material_usd": material_cost,
            "machining_usd": machine_cost,  # "machining" = print time cost
            "setup_usd": setup_cost,
            "finishing_usd": post_cost,
        }

    def _cost_sheet_metal(
        self,
        bbox: list[float],
        surface_area: float,
        mat_data: dict,
        finish: str,
    ) -> dict[str, float]:
        """Sheet metal cost breakdown."""
        # Assume thickness from smallest bbox dimension
        dims = sorted(bbox)
        thickness_mm = dims[0]
        sheet_area_cm2 = (dims[1] * dims[2]) / 100.0  # mm2 -> cm2

        material_cost = sheet_area_cm2 * (thickness_mm / 10.0) * mat_data["rate_per_cm3"]

        # Laser cutting: perimeter estimate
        perimeter_mm = 2.0 * (dims[1] + dims[2])
        cutting_cost = perimeter_mm * 0.01  # $0.01/mm

        # Bending: estimate 2-4 bends for typical sheet metal part
        n_bends = 3
        bending_cost = n_bends * 5.0

        # Finishing
        finish_rate = FINISHING_RATES.get(finish, 0.005)
        finishing_cost = surface_area * finish_rate

        setup_cost = SETUP_COSTS.get("sheet_metal", 40.0)

        return {
            "material_usd": material_cost,
            "machining_usd": cutting_cost + bending_cost,
            "setup_usd": setup_cost,
            "finishing_usd": finishing_cost,
        }

    def _cost_injection_mold(
        self,
        part_vol: float,
        complexity: str,
        quantity: int,
        mat_data: dict,
    ) -> dict[str, float]:
        """Injection molding cost breakdown (tooling + per-part)."""
        # Mold tooling cost based on complexity
        mold_costs = {"low": 3000.0, "medium": 8000.0, "high": 20000.0}
        tooling_cost = mold_costs.get(complexity, 8000.0)

        # Per-part material (injection grade polymer is cheap)
        per_part_material = part_vol * 0.015  # $/cm3 for injection pellets
        # Per-part cycle cost (~$0.50-2.00 per shot)
        cycle_cost = 0.50 if complexity == "low" else (1.00 if complexity == "medium" else 2.00)

        # Amortize tooling across quantity
        tooling_per_part = tooling_cost / max(quantity, 1)

        return {
            "material_usd": per_part_material,
            "machining_usd": cycle_cost,
            "setup_usd": tooling_per_part,
            "finishing_usd": 0.25,  # minimal post-processing per part
        }

    # ─── LLM review ──────────────────────────────────────────────────────

    def _llm_review(self, quote: dict, geom: dict) -> dict[str, Any] | None:
        """
        Send quote summary to Ollama LLM for cost risk review and optimizations.
        Returns parsed suggestions or None if LLM unavailable.
        """
        if not is_ollama_available():
            # Try cloud fallback
            return self._cloud_review(quote, geom)

        prompt = self._build_review_prompt(quote, geom)
        response = _call_ollama(prompt, _SYSTEM_PROMPT, self.model)
        if not response:
            return self._cloud_review(quote, geom)

        return self._parse_review_response(response)

    def _cloud_review(self, quote: dict, geom: dict) -> dict[str, Any] | None:
        """Fallback to cloud LLM if Ollama unavailable."""
        try:
            from ..llm_client import call_llm
            prompt = self._build_review_prompt(quote, geom)
            response = call_llm(prompt, system=_SYSTEM_PROMPT)
            if response:
                return self._parse_review_response(response)
        except Exception:
            pass
        return None

    def _build_review_prompt(self, quote: dict, geom: dict) -> str:
        """Build the prompt for LLM cost review."""
        bbox = quote["geometry"]["bbox_mm"]
        return (
            f"Part quote review:\n"
            f"Process: {quote['process']}\n"
            f"Material: {quote['material']}\n"
            f"Bounding box: {bbox[0]:.1f} x {bbox[1]:.1f} x {bbox[2]:.1f} mm\n"
            f"Part volume: {quote['geometry']['volume_cm3']:.2f} cm3\n"
            f"Stock volume: {quote['geometry']['stock_volume_cm3']:.2f} cm3\n"
            f"Material removal: {quote['geometry']['material_removal_pct']:.0f}%\n"
            f"Surface area: {quote['geometry']['surface_area_cm2']:.2f} cm2\n"
            f"Face count: {geom.get('face_count', 'unknown')}\n"
            f"Complexity: {geom.get('complexity', 'medium')}\n\n"
            f"Cost breakdown:\n"
            f"  Material:   ${quote['breakdown'].get('material_usd', 0):.2f}\n"
            f"  Machining:  ${quote['breakdown'].get('machining_usd', 0):.2f}\n"
            f"  Setup:      ${quote['breakdown'].get('setup_usd', 0):.2f}\n"
            f"  Finishing:  ${quote['breakdown'].get('finishing_usd', 0):.2f}\n"
            f"  Margin:     ${quote['breakdown'].get('margin_usd', 0):.2f}\n"
            f"  Unit cost:  ${quote['unit_cost_usd']:.2f}\n"
            f"  Quantity:   {quote['quantity']}\n\n"
            f"Review this quote. Identify cost risks and suggest optimizations."
        )

    def _parse_review_response(self, response: str) -> dict[str, Any]:
        """Parse structured LLM response into dict."""
        result: dict[str, Any] = {
            "confidence": "medium",
            "risks": [],
            "optimizations": [],
        }

        lines = response.strip().splitlines()
        section = None

        for line in lines:
            stripped = line.strip()

            if stripped.upper().startswith("CONFIDENCE:"):
                conf = stripped.split(":", 1)[1].strip().lower()
                if conf in ("high", "medium", "low"):
                    result["confidence"] = conf

            elif stripped.upper().startswith("RISKS:"):
                section = "risks"

            elif stripped.upper().startswith("OPTIMIZATIONS:") or stripped.upper().startswith("OPTIMIZATION:"):
                section = "optimizations"

            elif stripped.startswith("- ") and section:
                text = stripped[2:].strip()
                if text and section in result:
                    result[section].append(text)

        return result

    # ─── Pretty print ─────────────────────────────────────────────────────

    @staticmethod
    def print_quote(quote: dict) -> None:
        """Print a formatted quote summary to stdout."""
        proc_display = quote["process"].replace("_", " ").upper()
        mat_display = quote["material"].replace("_", " ").title()
        bbox = quote["geometry"]["bbox_mm"]
        removal = quote["geometry"]["material_removal_pct"]
        bd = quote["breakdown"]

        print(f"  [QUOTE] {proc_display} -- {mat_display}")
        print(f"  [QUOTE] Qty {quote['quantity']} unit cost: ${quote['unit_cost_usd']:.2f}")
        print(f"  [QUOTE]   Material:   ${bd.get('material_usd', 0):.2f}"
              f" (stock {bbox[0]:.0f}x{bbox[1]:.0f}x{bbox[2]:.0f}mm)")

        # Machining line with context
        mach_cost = bd.get("machining_usd", 0)
        if quote["process"].startswith("cnc") or quote["process"] == "cnc_turning":
            rate = MACHINE_RATES.get(quote["process"], 1.50)
            time_min = mach_cost / rate if rate > 0 else 0
            print(f"  [QUOTE]   Machining:  ${mach_cost:.2f}"
                  f" ({time_min:.1f} min @ ${rate:.2f}/min, {removal:.0f}% removal)")
        elif quote["process"] in ("fdm", "sla", "sls"):
            print(f"  [QUOTE]   Print cost: ${mach_cost:.2f}")
        else:
            print(f"  [QUOTE]   Processing: ${mach_cost:.2f}")

        print(f"  [QUOTE]   Setup:      ${bd.get('setup_usd', 0):.2f}")
        print(f"  [QUOTE]   Finishing:  ${bd.get('finishing_usd', 0):.2f}")
        print(f"  [QUOTE]   Margin:     ${bd.get('margin_usd', 0):.2f} (20%)")

        # Lead time
        lt = quote["lead_time_days"]
        if lt <= 3:
            print(f"  [QUOTE] Lead time: {lt} business days")
        else:
            # Show as range
            print(f"  [QUOTE] Lead time: {lt}-{lt + 2} business days")

        # Confidence
        conf = quote.get("confidence", "medium")
        print(f"  [QUOTE] Confidence: {conf}")

        # Optimizations
        opts = quote.get("optimizations", [])
        if opts:
            print("  [QUOTE] Optimizations:")
            for opt in opts:
                print(f"  [QUOTE]   - {opt}")

        # Risks
        risks = quote.get("risks", [])
        if risks:
            print("  [QUOTE] Risks:")
            for risk in risks:
                print(f"  [QUOTE]   - {risk}")


def run_quote_cli(
    step_path: str,
    material: str = "aluminium_6061",
    process: str = "cnc",
    quantity: int = 1,
    finish: str = "as_machined",
) -> dict[str, Any]:
    """
    CLI entry point for --quote flag.

    Runs the quoting agent and prints formatted output.
    Returns the quote dict.
    """
    path = Path(step_path)
    if not path.exists():
        print(f"  [QUOTE] ERROR: STEP file not found: {step_path}")
        return {}

    agent = QuoteAgent()
    quote = agent.quote(
        step_path=str(path),
        material=material,
        process=process,
        quantity=quantity,
        finish=finish,
    )

    print()
    QuoteAgent.print_quote(quote)
    print()

    return quote
