"""
cem_aria.py — ARIA CEM shim

Thin re-export layer so the CEM registry can import "cem_aria" without
shadowing the aria_cem/ package that may exist in the repo root.

All public names from aria_cem.py are re-exported here.
"""
from aria_cem import ARIAInputs, ARIAModule  # noqa: F401


def compute_for_goal(goal: str, params: dict | None = None) -> dict:
    """
    Entry point used by the CEM pipeline orchestrator.

    Accepts an optional params dict that can override ARIAInputs defaults.
    Returns a flat dict of geometry scalars suitable for plan["params"] injection.
    """
    inp_kwargs: dict = {}
    if params:
        for field_name in ARIAInputs.__dataclass_fields__:
            if field_name in params and params[field_name] is not None:
                try:
                    inp_kwargs[field_name] = float(params[field_name])
                except (TypeError, ValueError):
                    pass

    inp = ARIAInputs(**inp_kwargs)
    module = ARIAModule(inputs=inp)
    summary = module.compute()

    return {"part_family": "aria", **summary}
