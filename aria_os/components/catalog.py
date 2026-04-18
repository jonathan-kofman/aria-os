"""
Component catalog — central registry for standard parts.

Each component is a `ComponentSpec` that knows:
- How to generate its CadQuery geometry (parametric function)
- What mating features it exposes (shaft axis, bolt holes, top face, etc.)
- Its mass, material, cost, and BOM metadata
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Mating features — named geometric references on a component
# ---------------------------------------------------------------------------

@dataclass
class MatingFeature:
    """
    A named reference on a component used by the mating_solver.

    type:
      - "axis"      — a line in 3D (e.g. shaft rotation axis). params: {origin, direction}
      - "face"      — a planar face (e.g. top of flange). params: {origin, normal}
      - "hole"      — a through hole. params: {origin, axis, diameter_mm}
      - "bolt_circle" — ring of bolt holes. params: {center, axis, pcd_mm, n_bolts, bolt_dia_mm}
      - "point"     — a 3D point (e.g. end of shaft). params: {origin}
    """
    name: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Component spec
# ---------------------------------------------------------------------------

@dataclass
class ComponentSpec:
    """
    A standard part — either a purchased item (bolt, bearing, motor) or a
    parametric component template that can be generated on demand.
    """
    designation: str                         # e.g. "M6x20_12.9", "6205-2RS", "NEMA23-19mm"
    category: str                            # "fastener" | "bearing" | "motor" | "coupling" | "hardware"
    subcategory: str = ""                    # "bolt" | "nut" | "deep_groove" | "stepper" | etc.
    description: str = ""

    # Parametric generation
    generate_fn: Callable[[str], str] | None = None
    # Signature: generate_fn(output_step_path) -> step_path_actual. Returns where the file was written.
    # Optional params — most components are fully specified by their designation.

    # BOM / manufacturing metadata
    purchased: bool = True                   # True for off-the-shelf; False for make-to-order
    material: str = ""                       # "steel_12.9", "6061_aluminum", "sintered_steel", etc.
    mass_g: float = 0.0
    cost_usd: float = 0.0
    supplier: str = ""                       # optional — e.g. "McMaster 91290A*"
    supplier_pn: str = ""                    # optional — supplier part number

    # Export control classification. Most hardware is "EAR99" (no license needed for
    # most destinations). ITAR-controlled components (missile hardware, military
    # electronics, etc.) should be tagged with their USML category like "ITAR-IV"
    # so BOM/MillForge handoff can refuse to route them to non-US instances.
    # Values: "EAR99", "EAR-<ECCN>", "ITAR-<USML category>", "controlled-other"
    export_control: str = "EAR99"

    # Load ratings (for bearings, fasteners, linear rails) — optional
    dynamic_load_n: float | None = None      # C (dynamic load capacity), N
    static_load_n: float | None = None       # C0 (static load capacity), N
    max_rpm: float | None = None             # for rotating elements
    max_torque_nm: float | None = None       # for shafts, couplings, fasteners

    # Fidelity flags — be honest about what's accurate vs approximate
    # geometry_fidelity:
    #   "high"        — STEP within ±0.5mm of real part, OK for fit checks
    #   "medium"      — accurate envelope, simplified internals (catalog default)
    #   "placeholder" — visual stand-in only (e.g. flat-plate propellers, slot-grooved beam coupling)
    geometry_fidelity: str = "medium"
    # data_fidelity for mass / cost / load ratings:
    #   "manufacturer" — values from actual datasheet
    #   "estimated"    — calculated/inferred (e.g. derived BLDC torque from KV)
    data_fidelity: str = "manufacturer"
    fidelity_notes: str = ""

    # Mating features — what this component exposes to the mating_solver
    mating_features: list[MatingFeature] = field(default_factory=list)

    # Dimensions — free-form dict, category-specific
    dimensions: dict[str, float] = field(default_factory=dict)

    def get_feature(self, name: str) -> MatingFeature | None:
        """Look up a named mating feature."""
        for f in self.mating_features:
            if f.name == name:
                return f
        return None

    def to_bom_row(self, quantity: int = 1) -> dict[str, Any]:
        """Bill-of-materials row for this component in an assembly."""
        return {
            "designation": self.designation,
            "category": self.category,
            "subcategory": self.subcategory,
            "description": self.description,
            "quantity": quantity,
            "purchased": self.purchased,
            "material": self.material,
            "mass_g": self.mass_g,
            "unit_cost_usd": self.cost_usd,
            "total_cost_usd": self.cost_usd * quantity,
            "supplier": self.supplier,
            "supplier_pn": self.supplier_pn,
            "export_control": self.export_control,
        }

    @property
    def is_itar(self) -> bool:
        """True if this component is ITAR-controlled (USML Categories I-XXI)."""
        return self.export_control.upper().startswith("ITAR")

    @property
    def is_export_controlled(self) -> bool:
        """True if the component requires any export license check (not EAR99)."""
        return self.export_control.upper() not in ("EAR99", "")


# ---------------------------------------------------------------------------
# Catalog singleton
# ---------------------------------------------------------------------------

class ComponentCatalog:
    """Registry of all standard parts available in ARIA-OS."""

    def __init__(self) -> None:
        self._components: dict[str, ComponentSpec] = {}

    def register(self, spec: ComponentSpec) -> None:
        """Register a component. Overwrites if designation already present."""
        self._components[spec.designation] = spec

    def get(self, designation: str) -> ComponentSpec | None:
        """Fetch a component by exact designation."""
        return self._components.get(designation)

    def list_all(self) -> list[ComponentSpec]:
        """All registered components."""
        return list(self._components.values())

    def list_category(self, category: str) -> list[ComponentSpec]:
        """Components in a category (fastener, bearing, motor, coupling, hardware)."""
        return [c for c in self._components.values() if c.category == category]

    def list_subcategory(self, subcategory: str) -> list[ComponentSpec]:
        """Components with a specific subcategory (bolt, nut, deep_groove, ...)."""
        return [c for c in self._components.values() if c.subcategory == subcategory]

    def search(self, query: str) -> list[ComponentSpec]:
        """Fuzzy search on designation + description. Returns sorted by relevance."""
        q = query.lower()
        matches: list[tuple[int, ComponentSpec]] = []
        for c in self._components.values():
            score = 0
            if q in c.designation.lower():
                score += 10
            if q in c.description.lower():
                score += 3
            if q in c.subcategory:
                score += 2
            if q in c.category:
                score += 1
            if score > 0:
                matches.append((score, c))
        return [c for _, c in sorted(matches, key=lambda x: -x[0])]

    def generate(self, designation: str, output_path: str | Path) -> str:
        """
        Generate the STEP file for a component.

        Returns the actual output path (generate_fn may rename).
        Raises KeyError if designation not found, ValueError if no generator.
        """
        spec = self.get(designation)
        if spec is None:
            raise KeyError(f"Component '{designation}' not in catalog")
        if spec.generate_fn is None:
            raise ValueError(f"Component '{designation}' has no generator (purchased-only stub)")
        out = str(output_path)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        return spec.generate_fn(out)

    def __contains__(self, designation: str) -> bool:
        return designation in self._components

    def __len__(self) -> int:
        return len(self._components)


# Global singleton — components register themselves at import
catalog = ComponentCatalog()


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def register_component(spec: ComponentSpec) -> None:
    catalog.register(spec)


def get_component(designation: str) -> ComponentSpec | None:
    return catalog.get(designation)


def list_components(category: str | None = None) -> list[ComponentSpec]:
    return catalog.list_category(category) if category else catalog.list_all()
