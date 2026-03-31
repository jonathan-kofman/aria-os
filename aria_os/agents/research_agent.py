"""ResearchAgent — searches the web for reference specs, dimensions, and design context."""
from __future__ import annotations

import json
import re
from typing import Any

from .design_state import DesignState


class ResearchAgent:
    """
    Gathers real-world product specs and design references from the web
    before the DesignerAgent generates geometry.

    Does NOT use Ollama — uses WebSearch + WebFetch tools directly,
    then summarizes findings into structured context for the designer.
    """

    def __init__(self):
        self.name = "ResearchAgent"

    def research(self, state: DesignState) -> None:
        """
        Search for reference information about the product/part.
        Populates state.plan["research_context"] with findings.
        """
        goal = state.goal
        if not goal:
            return

        print(f"  [{self.name}] Researching: {goal}")

        findings: list[str] = []

        # 1. Search for product dimensions and specs
        specs = self._search_specs(goal)
        if specs:
            findings.append(f"## Reference Specifications\n{specs}")

        # 2. Search for design features and construction details
        design_info = self._search_design(goal)
        if design_info:
            findings.append(f"## Design Features\n{design_info}")

        # 3. Search for CAD/3D model references
        cad_refs = self._search_cad_references(goal)
        if cad_refs:
            findings.append(f"## CAD Reference Notes\n{cad_refs}")

        if findings:
            context = "\n\n".join(findings)
            state.plan.setdefault("research_context", context)

            # Extract any dimensions found and inject into spec (without overriding user values)
            extracted_dims = self._extract_dims_from_research(context)
            for k, v in extracted_dims.items():
                if k not in state.spec or state.spec[k] is None:
                    state.spec[k] = v

            print(f"  [{self.name}] Found {len(findings)} reference sections, "
                  f"{len(extracted_dims)} new dimensions")
        else:
            print(f"  [{self.name}] No reference information found")

    def _search_specs(self, goal: str) -> str:
        """Search for product specifications and dimensions."""
        try:
            from aria_os.agents.search_chain import web_search
            # Build a targeted spec search query
            query = f"{goal} dimensions specifications mm measurements"
            results = web_search(query)
            if results:
                return results[:3000]
        except Exception as exc:
            print(f"  [{self.name}] Spec search failed: {exc}")
        return ""

    def _search_design(self, goal: str) -> str:
        """Search for design features, construction, and teardown info."""
        try:
            from aria_os.agents.search_chain import web_search
            # Search for teardown / design analysis
            query = f"{goal} design features construction teardown engineering"
            results = web_search(query)
            if results:
                return results[:2000]
        except Exception as exc:
            print(f"  [{self.name}] Design search failed: {exc}")
        return ""

    def _search_cad_references(self, goal: str) -> str:
        """Search for existing CAD models or 3D printing references."""
        try:
            from aria_os.agents.search_chain import web_search
            query = f"{goal} 3D model CAD dimensions cross section"
            results = web_search(query)
            if results:
                return results[:1500]
        except Exception as exc:
            print(f"  [{self.name}] CAD ref search failed: {exc}")
        return ""

    def _extract_dims_from_research(self, text: str) -> dict[str, float]:
        """Extract dimensional values from research text."""
        dims: dict[str, float] = {}
        lower = text.lower()

        # Common dimension patterns in research results
        patterns = [
            (r"(?:width|wide)\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm", "width_mm"),
            (r"(?:height|tall|long)\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm", "height_mm"),
            (r"(?:depth|thick|deep)\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm", "depth_mm"),
            (r"(?:wall|shell)\s*(?:thickness)?\s*[=:]\s*(\d+(?:\.\d+)?)\s*mm", "wall_mm"),
            (r"(\d+(?:\.\d+)?)\s*mm\s*(?:x|×)\s*(\d+(?:\.\d+)?)\s*mm\s*(?:x|×)\s*(\d+(?:\.\d+)?)\s*mm", None),
            (r"(?:weight|mass)\s*[=:]\s*(\d+(?:\.\d+)?)\s*g", "mass_g"),
            (r"(\d+(?:\.\d+)?)\s*(?:mm|millimeter)\s*(?:wide|width)", "width_mm"),
            (r"(\d+(?:\.\d+)?)\s*(?:mm|millimeter)\s*(?:tall|height|long|length)", "height_mm"),
            (r"(\d+(?:\.\d+)?)\s*(?:mm|millimeter)\s*(?:deep|thick|depth)", "depth_mm"),
        ]

        for pattern, key in patterns:
            if key is None:
                # WxHxD pattern
                match = re.search(pattern, lower)
                if match and "width_mm" not in dims:
                    dims["width_mm"] = float(match.group(1))
                    dims["height_mm"] = float(match.group(2))
                    dims["depth_mm"] = float(match.group(3))
            else:
                match = re.search(pattern, lower)
                if match and key not in dims:
                    dims[key] = float(match.group(1))

        return dims
