"""
Tolerance stack-up analysis — worst-case and statistical (RSS) methods.

Pure-math module with no CAD dependency. Takes a list of dimensional
contributors and returns combined stack results.

Dimension format (per contributor):
    {"nominal": float, "plus": float, "minus": float}

- ``nominal`` is the target dimension (mm, inches — units pass through).
- ``plus`` is the positive tolerance (unsigned; always added).
- ``minus`` is the negative tolerance (unsigned; always subtracted).

Worst-case combines tolerances linearly — the absolute envelope a stack
will ever occupy. Statistical (Root-Sum-Square) combines them assuming
each contributor is an independent normal distribution with the stated
range equal to ±3σ. ``statistical()`` returns the 3σ (99.73%) half-width
of the stack around the nominal sum.

Typical usage::

    from aria_os.tolerance_stackup import worst_case, statistical
    dims = [
        {"nominal": 10.0, "plus": 0.05, "minus": 0.05},
        {"nominal": 20.0, "plus": 0.10, "minus": 0.02},
        {"nominal":  5.0, "plus": 0.01, "minus": 0.01},
    ]
    worst_case(dims)
    statistical(dims)
"""

from __future__ import annotations

import math
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_dims(dimensions: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    """Validate and normalise the input list.

    - Accepts mixed int/float.
    - Accepts signed ``minus`` — strips the sign, we only care about magnitude.
    - Raises ``ValueError`` for missing keys or non-finite numbers.
    """
    out: list[dict[str, float]] = []
    for i, d in enumerate(dimensions):
        if not isinstance(d, dict):
            raise ValueError(f"dimension #{i} is not a dict: {d!r}")
        try:
            nominal = float(d["nominal"])
            plus = float(d["plus"])
            minus = float(d["minus"])
        except KeyError as e:
            raise ValueError(
                f"dimension #{i} missing required key: {e.args[0]!r}"
            ) from None
        except (TypeError, ValueError):
            raise ValueError(f"dimension #{i} has non-numeric tolerance: {d!r}")

        for name, val in (("nominal", nominal), ("plus", plus), ("minus", minus)):
            if not math.isfinite(val):
                raise ValueError(f"dimension #{i} {name} is not finite: {val}")

        # Strip sign on the magnitudes — users sometimes pass minus=-0.05.
        out.append({"nominal": nominal, "plus": abs(plus), "minus": abs(minus)})
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def worst_case(dimensions: list[dict[str, Any]]) -> dict[str, float]:
    """Worst-case (arithmetic) tolerance stack.

    Sums all nominals, then adds the total plus tolerances to produce
    ``worst_case_max`` and subtracts the total minus tolerances to produce
    ``worst_case_min``. This is the guaranteed envelope regardless of
    individual part variation.

    Returns a dict with keys:
        nominal_total, worst_case_max, worst_case_min, total_plus,
        total_minus, total_range.
    """
    dims = _coerce_dims(dimensions)
    nominal_total = sum(d["nominal"] for d in dims)
    total_plus = sum(d["plus"] for d in dims)
    total_minus = sum(d["minus"] for d in dims)
    wc_max = nominal_total + total_plus
    wc_min = nominal_total - total_minus
    return {
        "nominal_total": round(nominal_total, 6),
        "worst_case_max": round(wc_max, 6),
        "worst_case_min": round(wc_min, 6),
        "total_plus": round(total_plus, 6),
        "total_minus": round(total_minus, 6),
        "total_range": round(wc_max - wc_min, 6),
        "method": "worst_case",
        "n_contributors": len(dims),
    }


def statistical(dimensions: list[dict[str, Any]]) -> dict[str, float]:
    """Statistical (RSS) tolerance stack assuming each contributor is ±3σ.

    For each contributor we treat the stated tolerance range as 6σ. We
    take the half-range (``(plus + minus) / 2``) as the individual 3σ
    half-width, convert to 1σ, then RSS them and scale back to 3σ.

    Returns dict with:
        nominal_total, statistical_sigma_3, stat_max, stat_min, total_range.

    ``stat_max`` / ``stat_min`` are ``nominal_total ± statistical_sigma_3``
    (centred on the biased nominal if plus != minus).
    """
    dims = _coerce_dims(dimensions)
    nominal_total = sum(d["nominal"] for d in dims)

    # Biased centre: shift each contributor by (plus - minus) / 2 so the
    # statistical distribution is around the midpoint of its tolerance band,
    # not the nominal. Mirrors the convention used in GD&T textbooks.
    centre_shift = sum((d["plus"] - d["minus"]) / 2.0 for d in dims)
    stat_centre = nominal_total + centre_shift

    # Each contributor's 3σ half-width = (plus + minus) / 2 (tolerance half-range).
    # 1σ half-width = half / 3. Variance = (half / 3)**2.
    variance = sum(((d["plus"] + d["minus"]) / 2.0 / 3.0) ** 2 for d in dims)
    sigma = math.sqrt(variance)
    sigma_3 = 3.0 * sigma

    return {
        "nominal_total": round(nominal_total, 6),
        "stat_centre": round(stat_centre, 6),
        "statistical_sigma_1": round(sigma, 6),
        "statistical_sigma_3": round(sigma_3, 6),
        "stat_max": round(stat_centre + sigma_3, 6),
        "stat_min": round(stat_centre - sigma_3, 6),
        "total_range": round(2 * sigma_3, 6),
        "method": "statistical_rss_3sigma",
        "n_contributors": len(dims),
    }


def compare(dimensions: list[dict[str, Any]]) -> dict[str, Any]:
    """Run both methods and return a side-by-side comparison dict.

    Useful for reports: shows how much tolerance can be recovered by
    moving from worst-case to a statistical assumption.
    """
    wc = worst_case(dimensions)
    st = statistical(dimensions)
    range_saved = wc["total_range"] - st["total_range"]
    pct_saved = (range_saved / wc["total_range"] * 100.0) if wc["total_range"] > 0 else 0.0
    return {
        "worst_case": wc,
        "statistical": st,
        "range_saved": round(range_saved, 6),
        "percent_saved": round(pct_saved, 2),
    }
