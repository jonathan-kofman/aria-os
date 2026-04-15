# ARIA-OS: Autonomous CAD generation for ARIA auto-belay
# Lazy imports — avoid crashing the whole package if a top-level orchestrator
# dependency (e.g. grasshopper_generator, blender_generator) is unavailable.

def run(*args, **kwargs):
    from aria_os.orchestrator import run as _run
    return _run(*args, **kwargs)


def run_image_fast(*args, **kwargs):
    from aria_os.orchestrator import run_image_fast as _run_image_fast
    return _run_image_fast(*args, **kwargs)


__all__ = ["run", "run_image_fast"]
