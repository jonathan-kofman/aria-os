"""
cem_core.py - CEM Platform Core Abstraction Layer
Domain-agnostic base classes shared across all CEM modules.
RP (rocket propulsion), HX (heat exchanger), EM (electric motor) all inherit from here.

Architecture mirrors Noyron: physics encodes geometry, not vice versa.
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod


# ─────────────────────────────────────────────────────────────────────────────
# BASE MATERIAL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Material:
    """Generic material properties. Extend for specific materials."""
    name: str
    density_kg_m3: float        = 8000.0
    yield_strength_MPa: float   = 500.0
    ultimate_strength_MPa: float= 700.0
    youngs_modulus_GPa: float   = 200.0
    poisson_ratio: float        = 0.30
    thermal_conductivity_W_mK: float = 15.0
    specific_heat_J_kgK: float  = 500.0
    max_use_temp_K: float       = 1000.0
    # Temperature-dependent (override with arrays if needed)
    yield_at_temp_MPa: Optional[float] = None  # yield at operating temp

    def allowable_stress_MPa(self, safety_factor: float = 2.0) -> float:
        """Allowable stress = yield (at temp if available) / SF."""
        yield_s = self.yield_at_temp_MPa or self.yield_strength_MPa
        return yield_s / safety_factor


# Pre-defined materials from Seraphim data
MATERIAL_X1_420i = Material(
    name="X1 420i Metal (60% 420SS + 40% 90/10 Bronze)",
    density_kg_m3=7860, yield_strength_MPa=620, ultimate_strength_MPa=800,
    thermal_conductivity_W_mK=22.6, specific_heat_J_kgK=478, max_use_temp_K=737)

MATERIAL_6061_AL = Material(
    name="6061 Aluminum T6",
    density_kg_m3=2700, yield_strength_MPa=276, ultimate_strength_MPa=310,
    youngs_modulus_GPa=69, thermal_conductivity_W_mK=167,
    specific_heat_J_kgK=896, max_use_temp_K=473)

# NOTE: yield_strength_MPa=700 is the elevated-temperature (700 degC) yield used for
# hot-section CEM analysis. Room-temp yield is 1100 MPa per context/aria_materials.md.
# If you need room-temp properties, override yield_strength_MPa=1100.
MATERIAL_INCONEL718 = Material(
    name="Inconel 718 (700C)",
    density_kg_m3=8220, yield_strength_MPa=700, ultimate_strength_MPa=900,
    youngs_modulus_GPa=165, thermal_conductivity_W_mK=14,
    max_use_temp_K=1200, yield_at_temp_MPa=700)

MATERIAL_COPPER_C18150 = Material(
    name="Copper C18150 (regen inner wall)",
    density_kg_m3=8900, yield_strength_MPa=380, ultimate_strength_MPa=420,
    youngs_modulus_GPa=128, thermal_conductivity_W_mK=320,
    max_use_temp_K=800)


# ─────────────────────────────────────────────────────────────────────────────
# BASE FLUID
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Fluid:
    """Generic fluid properties."""
    name: str
    density_kg_m3: float    = 1000.0
    viscosity_Pa_s: float   = 1e-3
    specific_heat_J_kgK: float = 4180.0
    thermal_conductivity_W_mK: float = 0.6
    boiling_point_K: float  = 373.0
    phase: str              = "liquid"   # liquid, gas, supercritical

    @property
    def Prandtl(self) -> float:
        return self.viscosity_Pa_s * self.specific_heat_J_kgK / self.thermal_conductivity_W_mK


# Pre-defined fluids from Seraphim data
FLUID_KEROSENE = Fluid(
    name="Kerosene (Seraphim)", density_kg_m3=820, viscosity_Pa_s=1.64e-3,
    specific_heat_J_kgK=2010, thermal_conductivity_W_mK=0.14, boiling_point_K=450)

FLUID_LOX = Fluid(
    name="LOX", density_kg_m3=1141, viscosity_Pa_s=1.96e-4,
    specific_heat_J_kgK=1700, thermal_conductivity_W_mK=0.152,
    boiling_point_K=90.2, phase="liquid")

FLUID_IPA = Fluid(
    name="IPA (LOX simulant)", density_kg_m3=786, viscosity_Pa_s=2.4e-3,
    specific_heat_J_kgK=2570, thermal_conductivity_W_mK=0.14, boiling_point_K=355.4)

FLUID_WATER = Fluid(
    name="Water (Kero simulant)", density_kg_m3=998, viscosity_Pa_s=1.0e-3,
    specific_heat_J_kgK=4182, thermal_conductivity_W_mK=0.598, boiling_point_K=373.15)


# ─────────────────────────────────────────────────────────────────────────────
# BASE LOADS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PressureLoad:
    """Internal pressure load on a vessel or channel."""
    pressure_Pa: float
    safety_factor: float = 2.0
    load_type: str = "internal"   # internal or external

    def required_wall_thickness(self, radius_m: float,
                                 material: Material) -> float:
        """Thin-wall hoop stress: t = P*r*SF / sigma_allow"""
        sigma = material.allowable_stress_MPa(self.safety_factor) * 1e6
        return self.pressure_Pa * radius_m * self.safety_factor / sigma


@dataclass
class ThermalLoad:
    """Heat flux load on a surface."""
    q_flux_W_m2: float
    T_fluid_K: float = 300.0      # coolant/ambient temperature
    T_wall_limit_K: float = 900.0 # max allowable wall temperature

    @property
    def q_flux_MW(self) -> float:
        return self.q_flux_W_m2 / 1e6

    def required_h_conv(self) -> float:
        """Required convective coefficient: h = q / (T_wall - T_fluid)"""
        dT = self.T_wall_limit_K - self.T_fluid_K
        return self.q_flux_W_m2 / max(dT, 1.0)


@dataclass
class MechanicalLoad:
    """Force/moment loads."""
    force_N: float = 0.0
    moment_Nm: float = 0.0
    safety_factor: float = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# BASE CHANNEL (for regen cooling, HX tubes, etc.)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Channel:
    """Generic flow channel — used by RP cooling, HX, etc."""
    width_mm: float
    height_mm: float
    length_mm: float
    fluid: Optional[Fluid] = None
    mass_flow_kg_s: float = 0.0

    @property
    def area_mm2(self) -> float:
        return self.width_mm * self.height_mm

    @property
    def hydraulic_diameter_mm(self) -> float:
        return 2 * self.width_mm * self.height_mm / (self.width_mm + self.height_mm)

    @property
    def area_m2(self) -> float:
        return self.area_mm2 * 1e-6

    @property
    def hydraulic_diameter_m(self) -> float:
        return self.hydraulic_diameter_mm / 1000.0

    def velocity_m_s(self) -> float:
        if not self.fluid or self.mass_flow_kg_s == 0:
            return 0.0
        return self.mass_flow_kg_s / (self.fluid.density_kg_m3 * self.area_m2)

    def reynolds(self) -> float:
        if not self.fluid: return 0.0
        v = self.velocity_m_s()
        return self.fluid.density_kg_m3 * v * self.hydraulic_diameter_m / self.fluid.viscosity_Pa_s

    def dittus_boelter_h(self) -> float:
        """Forced convection h via Dittus-Boelter correlation."""
        if not self.fluid: return 0.0
        Re = self.reynolds()
        Pr = self.fluid.Prandtl
        k  = self.fluid.thermal_conductivity_W_mK
        if Re < 2300:
            return 3.66 * k / self.hydraulic_diameter_m  # laminar
        return 0.023 * Re**0.8 * Pr**0.4 * k / self.hydraulic_diameter_m

    def pressure_drop_Pa(self) -> float:
        """Darcy-Weisbach pressure drop."""
        if not self.fluid: return 0.0
        Re = self.reynolds()
        v  = self.velocity_m_s()
        f  = 64.0/Re if Re < 2300 else 0.316*Re**(-0.25)
        L  = self.length_mm / 1000.0
        Dh = self.hydraulic_diameter_m
        return f * (L / Dh) * 0.5 * self.fluid.density_kg_m3 * v**2


# ─────────────────────────────────────────────────────────────────────────────
# BASE CEM MODULE
# ─────────────────────────────────────────────────────────────────────────────

class CEMModule(ABC):
    """
    Abstract base class for all CEM domain modules.
    RP (rocket propulsion), HX (heat exchanger), etc. all inherit from this.

    Every module must implement:
      compute()  — derive geometry from physics
      validate() — check physical plausibility
      export()   — write output files
    """

    def __init__(self, name: str):
        self.name     = name
        self.warnings = []
        self.passed   = []
        self._computed = False

    @abstractmethod
    def compute(self) -> Any:
        """Derive all geometry from input parameters. Returns geometry object."""
        pass

    @abstractmethod
    def validate(self) -> bool:
        """Run all physics/manufacturing sanity checks. Returns True if all pass."""
        pass

    @abstractmethod
    def export(self, out_dir: str) -> List[str]:
        """Export all outputs to out_dir. Returns list of file paths created."""
        pass

    def warn(self, msg: str):
        self.warnings.append(f"WARNING: {msg}")

    def ok(self, msg: str):
        self.passed.append(f"OK: {msg}")

    def print_validation(self):
        print(f"\n{'='*55}\n  {self.name} - Validation\n{'='*55}")
        for p in self.passed:   print(f"  {p}")
        for w in self.warnings: print(f"  {w}")
        status = "ALL CHECKS PASSED" if not self.warnings else f"{len(self.warnings)} WARNINGS"
        print(f"  Status: {status}\n{'='*55}")

    def physics_check(self, condition: bool, ok_msg: str, warn_msg: str):
        if condition: self.ok(ok_msg)
        else: self.warn(warn_msg)


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class CEMPlatform:
    """
    Top-level platform registry.
    Mirrors Noyron architecture: one platform, multiple domain modules.

    Usage:
        platform = CEMPlatform()
        platform.register("RP", RocketPropulsionModule(...))
        platform.register("HX", HeatExchangerModule(...))  # future
        results = platform.run_all()
    """

    def __init__(self, name: str = "CEM Platform"):
        self.name    = name
        self.modules: Dict[str, CEMModule] = {}
        self.version = "0.3.0"

    def register(self, key: str, module: CEMModule):
        self.modules[key] = module
        print(f"  [{self.name}] Registered module: {key} ({module.name})")

    def run(self, key: str, out_dir: str = "output") -> Any:
        if key not in self.modules:
            raise KeyError(f"Module {key} not registered")
        mod = self.modules[key]
        print(f"\n  [{self.name}] Running module: {key}...")
        geom = mod.compute()
        mod.validate()
        mod.print_validation()
        files = mod.export(out_dir)
        print(f"  [{self.name}] {key} complete. Outputs: {files}")
        return geom

    def run_all(self, out_dir: str = "output") -> Dict[str, Any]:
        results = {}
        for key in self.modules:
            results[key] = self.run(key, out_dir)
        return results

    def summary(self):
        print(f"\n{'='*55}\n  {self.name} v{self.version}\n{'='*55}")
        print(f"  Registered modules: {list(self.modules.keys())}")
        total_warn = sum(len(m.warnings) for m in self.modules.values())
        total_ok   = sum(len(m.passed)   for m in self.modules.values())
        print(f"  Checks: {total_ok} passed, {total_warn} warnings")
        print(f"{'='*55}")


# ─────────────────────────────────────────────────────────────────────────────
# SHARED PHYSICS UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def hoop_stress_Pa(pressure_Pa: float, radius_m: float, thickness_m: float) -> float:
    """Thin-wall hoop stress [Pa]."""
    return pressure_Pa * radius_m / thickness_m

def min_wall_thickness(pressure_Pa: float, radius_m: float,
                        material: Material, safety_factor: float = 2.0) -> float:
    """Minimum wall thickness from hoop stress [m]."""
    sigma = material.allowable_stress_MPa(safety_factor) * 1e6
    return pressure_Pa * radius_m / sigma

def dittus_boelter(Re: float, Pr: float, k: float, Dh: float,
                    heating: bool = True) -> float:
    """Dittus-Boelter convection coefficient [W/m2K]."""
    n = 0.4 if heating else 0.3
    if Re < 2300:
        return 3.66 * k / Dh
    return 0.023 * Re**0.8 * Pr**n * k / Dh

def reynolds(rho: float, v: float, L: float, mu: float) -> float:
    """Reynolds number."""
    return rho * v * L / mu

def isentropic_area_ratio(M: float, gamma: float) -> float:
    """Isentropic area ratio A/A* at Mach M."""
    g = gamma
    return (1/M) * ((2/(g+1)) * (1 + (g-1)/2 * M**2))**((g+1)/(2*(g-1)))

def prandtl_meyer(M: float, gamma: float) -> float:
    """Prandtl-Meyer function nu(M) [radians]."""
    g = gamma
    return (np.sqrt((g+1)/(g-1)) *
            np.arctan(np.sqrt((g-1)/(g+1)*(M**2-1))) -
            np.arctan(np.sqrt(M**2-1)))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — demonstrate platform architecture
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  CEM Platform Core — Architecture Demo")
    print("="*55)

    # Show material library
    print("\nMaterial Library (from Seraphim data):")
    for m in [MATERIAL_X1_420i, MATERIAL_6061_AL, MATERIAL_INCONEL718, MATERIAL_COPPER_C18150]:
        print(f"  {m.name[:40]:<40} "
              f"sigma_y={m.yield_strength_MPa:.0f}MPa  "
              f"k={m.thermal_conductivity_W_mK:.0f}W/mK  "
              f"T_max={m.max_use_temp_K:.0f}K")

    # Show fluid library
    print("\nFluid Library (from Seraphim data):")
    for f in [FLUID_KEROSENE, FLUID_LOX, FLUID_IPA, FLUID_WATER]:
        print(f"  {f.name:<35} rho={f.density_kg_m3:.0f}kg/m3  "
              f"mu={f.viscosity_Pa_s:.2e}Pa.s  Pr={f.Prandtl:.2f}")

    # Demonstrate channel physics
    print("\nChannel Physics Demo (regen cooling at throat):")
    ch = Channel(width_mm=2.43, height_mm=1.88, length_mm=333.1,
                 fluid=FLUID_KEROSENE, mass_flow_kg_s=0.296)
    print(f"  Velocity:   {ch.velocity_m_s():.1f} m/s")
    print(f"  Reynolds:   {ch.reynolds():.0f}")
    print(f"  h_conv:     {ch.dittus_boelter_h():.0f} W/m2K")
    print(f"  dP:         {ch.pressure_drop_Pa()/1e5:.3f} bar")

    # Pressure vessel demo
    print("\nPressure Vessel Demo (chamber wall at Pc=34.5bar):")
    pl = PressureLoad(pressure_Pa=34.474e5, safety_factor=2.0)
    t  = pl.required_wall_thickness(0.02247, MATERIAL_X1_420i)
    print(f"  Required wall thickness: {t*1000:.3f} mm")
    print(f"  Hoop stress:             {hoop_stress_Pa(34.474e5, 0.02247, t)/1e6:.1f} MPa")

    # Platform registry demo
    print("\nPlatform Registry:")
    platform = CEMPlatform("Seraphim CEM")
    print(f"  Version: {platform.version}")
    print(f"  Ready for module registration:")
    print(f"    platform.register('RP', RocketPropulsionModule(...))")
    print(f"    platform.register('HX', HeatExchangerModule(...))   # future")
    print(f"    platform.run_all()")
