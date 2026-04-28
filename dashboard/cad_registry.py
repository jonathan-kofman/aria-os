"""cad_registry.py — Centralized CAD bridge URL registry.

Decouples aria_server.py from specific CAD URL configurations. The dashboard
can import this module instead of hardcoding _CAD_BASE_URL internally.

This allows adding new CAD bridges (like AutoCAD on port 7503) without modifying
aria_server.py directly — just update the dicts here.
"""


def get_cad_base_urls() -> dict:
    """Return the base URLs for all MCAD bridges.

    Returns:
        dict: {cad_name: http_url, ...}
            cad_name can be "solidworks", "sw", "rhino", "fusion360", "autocad", etc.
    """
    return {
        "solidworks": "http://localhost:7501",
        "sw":         "http://localhost:7501",
        "rhino":      "http://localhost:7502",
        "autocad":    "http://localhost:7503",
        "acad":       "http://localhost:7503",
        "fusion360":  "http://localhost:7504",  # in-process in ARIA stack
        "fusion":     "http://localhost:7504",
        "onshape":    "http://localhost:7506",  # REST bridge to cad.onshape.com
    }


def get_ecad_base_urls() -> dict:
    """Return the base URLs for all ECAD bridges.

    Returns:
        dict: {cad_name: http_url, ...}
    """
    return {
        "kicad": "http://localhost:7505",
    }


def get_all_cad_urls() -> dict:
    """Return combined MCAD + ECAD URLs."""
    urls = {}
    urls.update(get_cad_base_urls())
    urls.update(get_ecad_base_urls())
    return urls
