"""
Smoke test for the entire component catalog. Generates a STEP for every
registered component and reports failures. Marked slow — opt-in via:

    pytest tests/test_catalog_smoke.py -v
    pytest tests/test_catalog_smoke.py -v -m smoke

This catches latent bugs in catalog entries (closure-scoping issues, bad
defaults, edge-case dimensions) that the targeted tests miss.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from aria_os.components import catalog


def _generate_one(designation: str, tmpdir: Path) -> tuple[bool, str, int]:
    """Try to generate a STEP for one component AND validate its geometry.

    Returns (ok, error_msg, file_size). A part is "ok" only when:
      1. STEP file generates without raising
      2. STEP file size is non-trivial (>100 bytes)
      3. Geometry passes the spec-derived contract — single solid, watertight,
         expected hole count (genus), expected radial features (FFT lobes),
         bbox within ±20% of the catalog spec dims

    Visual verification (LLM-based) is intentionally NOT run here — it costs
    API budget and is non-deterministic. The contract validator covers what
    visual would catch deterministically (missing holes, wrong blade count).
    """
    try:
        out = tmpdir / f"{designation.replace('/', '_').replace(' ', '_')}.step"
        path = catalog.generate(designation, str(out))
        size = Path(path).stat().st_size
        if size < 100:
            return False, f"STEP file too small ({size} bytes)", size

        # Contract validation — the gate that catches silent geometry bugs
        from aria_os.validation import Contract, validate_part
        from aria_os.components import catalog as _cat
        import cadquery as cq

        spec_obj = next((s for s in _cat.list_all() if s.designation == designation), None)
        if spec_obj is None:
            return True, "", size  # no catalog spec to validate against
        spec_dict = _spec_to_dict(spec_obj)
        contract = Contract.from_spec(spec_dict, getattr(spec_obj, "description", ""))
        if contract.is_empty():
            return True, "", size  # nothing checkable in the catalog spec

        shape = cq.importers.importStep(str(path))
        result = validate_part(shape, contract)
        if not result.passed:
            return False, "contract: " + "; ".join(result.failures[:2]), size
        return True, "", size
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}", 0


def _spec_to_dict(spec_obj) -> dict:
    """Translate a catalog ComponentSpec into the dict form Contract.from_spec
    expects. Catalog stores most dims in spec.dimensions (a dict), some on
    top-level attrs, plus a `params` dict on a few entries. Pull from all three.
    """
    d: dict = {}
    # Top-level attrs
    for src, dst in (
        ("od_mm", "od_mm"), ("outer_dia_mm", "od_mm"), ("diameter_mm", "diameter_mm"),
        ("bore_mm", "bore_mm"), ("id_mm", "id_mm"),
        ("width_mm", "width_mm"), ("height_mm", "height_mm"),
        ("depth_mm", "depth_mm"), ("length_mm", "length_mm"),
        ("thickness_mm", "thickness_mm"),
        ("n_bolts", "n_bolts"), ("n_blades", "n_blades"),
        ("n_fins", "n_fins"), ("n_spokes", "n_spokes"), ("n_teeth", "n_teeth"),
        ("part_type", "part_type"), ("subcategory", "part_type"),
    ):
        v = getattr(spec_obj, src, None)
        if v is not None and not callable(v):
            d.setdefault(dst, v)
    # Dimensions dict (canonical location for catalog parts)
    dims = getattr(spec_obj, "dimensions", None) or {}
    _dim_aliases = {
        "outer_diameter_mm": "od_mm", "outer_dia_mm": "od_mm",
        "diameter_mm": "od_mm",
        "id_mm": "bore_mm", "inner_dia_mm": "bore_mm",
        "body_dia_mm": "od_mm",        # for cylindrical bodies, body_dia is the OD
        "flange_dia_mm": None,         # don't mistake flange OD for part OD
        "total_length_mm": "length_mm",
        "fin_height_mm": None,         # informational, not a bbox dim
    }
    for k, v in dims.items():
        if v is None or callable(v):
            continue
        target = _dim_aliases.get(k, k)
        if target is None:
            continue
        d.setdefault(target, v)
    # `params` dict if present (some specs use this instead of dimensions)
    params = getattr(spec_obj, "params", None) or {}
    for k, v in params.items():
        d.setdefault(k, v)
    # Mating features carry bolt-circle counts and PCDs that the dims dict
    # does not. Pull n_bolts and bolt_dia_mm from any "bolt_circle" entries.
    mating = getattr(spec_obj, "mating_features", None) or []
    for mf in mating:
        mf_type = getattr(mf, "type", None) or (mf.get("type") if isinstance(mf, dict) else None)
        mf_params = getattr(mf, "params", None) or (mf.get("params") if isinstance(mf, dict) else {}) or {}
        if mf_type == "bolt_circle":
            n = mf_params.get("n_bolts")
            if n is not None:
                d["n_bolts"] = (d.get("n_bolts") or 0) + int(n)
            pcd = mf_params.get("pcd_mm")
            if pcd is not None:
                d.setdefault("bolt_circle_r_mm", float(pcd) / 2.0)
            bd = mf_params.get("bolt_dia_mm")
            if bd is not None:
                d.setdefault("bolt_dia_mm", float(bd))
    return d


@pytest.mark.slow
def test_all_components_generate_step(tmp_path):
    """
    Generate a STEP for every component in the catalog. Report which fail.

    Parallelized across CPU cores via joblib. ~5-8× faster than the original
    serial loop on an 8-core machine. Falls back to serial if joblib not
    installed.
    """
    candidates = [s for s in catalog.list_all() if s.generate_fn is not None]
    failures: list[tuple[str, str]] = []
    successes = 0
    total_size = 0

    try:
        from joblib import Parallel, delayed
        # MUST use processes, not threads — cadquery's expression grammar
        # (built via pyparsing) is not thread-safe and corrupts under
        # concurrent access ("missing positional argument: 'res'" errors).
        # Process isolation gives clean state per worker.
        results = Parallel(n_jobs=-1, backend="loky", verbose=0)(
            delayed(_generate_one)(s.designation, tmp_path) for s in candidates
        )
        for spec, (ok, err, size) in zip(candidates, results):
            if ok:
                successes += 1
                total_size += size
            else:
                failures.append((spec.designation, err))
    except ImportError:
        # Serial fallback when joblib is unavailable
        for spec in candidates:
            ok, err, size = _generate_one(spec.designation, tmp_path)
            if ok:
                successes += 1
                total_size += size
            else:
                failures.append((spec.designation, err))

    print()
    print(f"Catalog smoke: {successes} ok, {len(failures)} failed")
    if failures:
        print("Failing components (first 20):")
        for desig, err in failures[:20]:
            print(f"  {desig}: {err}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

    # Zero tolerance for failures — the contract validator is the gate.
    # If a part fails, fix the part (or relax its specific contract).
    # The previous 5% threshold was the false-PASS pattern in disguise.
    n_with_generator = successes + len(failures)
    if n_with_generator == 0:
        pytest.skip("No components with generators")
    assert len(failures) == 0, (
        f"{len(failures)}/{n_with_generator} catalog parts failed contract validation. "
        f"First failures: " + "; ".join(f"{d}: {e[:80]}" for d, e in failures[:5])
    )


def test_one_per_subcategory_generates(tmp_path):
    """
    Faster smoke test: generate one component per subcategory. Catches
    common-case generator bugs without spending 30s on the full catalog.
    """
    seen_subcategories: set[str] = set()
    samples: list = []
    for spec in catalog.list_all():
        key = (spec.category, spec.subcategory)
        if key in seen_subcategories or spec.generate_fn is None:
            continue
        seen_subcategories.add(key)
        samples.append(spec)

    failures = []
    for spec in samples:
        ok, err, size = _generate_one(spec.designation, tmp_path)
        if not ok:
            failures.append((spec.designation, spec.subcategory, err))

    if failures:
        for desig, subcat, err in failures:
            print(f"  [{subcat}] {desig}: {err}")
        pytest.fail(f"{len(failures)}/{len(samples)} subcategory representatives failed")
