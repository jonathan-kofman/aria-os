"""
Full pipeline test: turbopump housing through the agent pipeline
with proper routing, engineering prompts, and visual verification.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


def main():
    from aria_os.orchestrator import run

    goal = (
        "turbopump housing for a 5kN thrust LOX/kerosene rocket engine. "
        "Must include: volute scroll casing (snail shell spiral collecting fluid from impeller), "
        "axial inlet pipe 25mm ID, tangential discharge outlet 20mm ID, "
        "15mm shaft bore with bearing pocket and seal groove, "
        "6-bolt flange on open face (M6 on 90mm PCD), "
        "8mm wall thickness, overall ~100x100x80mm, 6061-T6 aluminum, "
        "designed for 5-axis CNC manufacturing"
    )

    result = run(
        goal,
        agent_mode=True,
        max_agent_iterations=10,
    )

    print("\nDone.")


if __name__ == "__main__":
    main()
