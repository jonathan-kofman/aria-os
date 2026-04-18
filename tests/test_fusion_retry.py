"""
Test: Fusion generation with retry loop + visual verification.
Submits one job at a time, waits for completion, verifies, retries with feedback.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

from aria_os.llm_client import call_llm
from aria_os.generators.fusion_generator import _FUSION_LLM_SYSTEM
from aria_os.visual_verifier import verify_visual

JOBS_DIR = Path("outputs/cad/fusion_jobs")
MAX_RETRIES = 3
POLL_TIMEOUT = 180  # seconds per attempt

GOAL = (
    "Turbopump housing: 120mm OD cylinder, 180mm tall, 8mm wall, "
    "bolt flange at bottom 160mm OD with 6 M8 holes, "
    "bearing bore 50mm on top, 30mm outlet pipe on top, "
    "4 internal ribs connecting bore to wall"
)
PARAMS = {"od_mm": 120, "height_mm": 180, "wall_mm": 8, "n_bolts": 6, "bore_mm": 50}
PART_ID = "turbopump_retry"
STEP_PATH = str(Path("outputs/cad/step/turbopump_retry.step").resolve()).replace("\\", "/")
STL_PATH = str(Path("outputs/cad/stl/turbopump_retry.stl").resolve()).replace("\\", "/")


def clean_code(response: str) -> str:
    code = response.strip()
    if "```" in code:
        match = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.DOTALL)
        if match:
            code = match.group(1).strip()
    elif not code.startswith(("import ", "#")):
        for i, line in enumerate(code.split("\n")):
            if line.strip().startswith(("import ", "#")):
                code = "\n".join(code.split("\n")[i:])
                break
    code = re.sub(r".*designType.*\n", "", code)
    code = re.sub(r".*doc\.close.*\n", "", code)
    code = re.sub(r".*documents\.add.*\n", "", code)
    return code


def submit_and_wait(code: str, attempt: int) -> dict:
    """Submit job to Fusion bridge, wait for result. Returns result dict."""
    # Clean any stale files
    for f in JOBS_DIR.glob("*.json"):
        f.unlink()

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = {
        "part_id": PART_ID,
        "goal": GOAL,
        "script": code,
        "step_path": STEP_PATH,
        "stl_path": STL_PATH,
    }
    job_path = JOBS_DIR / f"{PART_ID}.json"
    job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")
    print(f"  Job submitted, waiting for Fusion (up to {POLL_TIMEOUT}s)...")

    done_f = JOBS_DIR / f"_done_{PART_ID}.json"
    err_f = JOBS_DIR / f"_err_{PART_ID}.json"

    t0 = time.time()
    while time.time() - t0 < POLL_TIMEOUT:
        if done_f.exists():
            result = json.loads(done_f.read_text(encoding="utf-8"))
            return {"status": "done", **result}
        if err_f.exists():
            result = json.loads(err_f.read_text(encoding="utf-8"))
            return result
        time.sleep(3)

    return {"status": "timeout"}


def main():
    prev_error = None

    for attempt in range(MAX_RETRIES):
        print(f"\n{'='*50}")
        print(f"Attempt {attempt + 1}/{MAX_RETRIES}")
        print(f"{'='*50}")

        # Generate script
        if prev_error:
            prompt = (
                f"Previous Fusion script failed:\n{prev_error[:500]}\n\n"
                f"Fix the script for: {GOAL}\n"
                f"IMPORTANT FIXES:\n"
                f"- For internal ribs: create each rib as a NewBody, then use CombineFeatures with JoinFeatureOperation\n"
                f"- For bolt holes: use circular pattern with correct count\n"
                f"- For outlet pipe: extrude a circle UP from the top face as NewBody, then Join\n"
                f"- Bearing bore: cut INTO the top face, not through the whole body\n"
                f"All CM. No designType/close/create doc."
            )
        else:
            prompt = (
                f"{GOAL}\n\n"
                f"Build order:\n"
                f"1. Main cylinder: sketch 6cm radius, extrude 18cm, shell -0.8cm\n"
                f"2. Bottom flange: sketch 8cm radius on XY plane, extrude 1.5cm as NewBody, Join\n"
                f"3. Bolt holes: sketch 0.4cm circles on flange top, 6x circular pattern, Cut\n"
                f"4. Bearing bore: sketch 2.5cm circle on top face, cut 2.5cm deep\n"
                f"5. Outlet pipe: sketch 1.5cm circle on top face, extrude up 4cm as NewBody, Join\n"
                f"6. 4 ribs: create thin box sketches on XZ and YZ planes inside cylinder, "
                f"   extrude as NewBody, then Join to main body\n"
                f"All CM. No designType/close/create doc."
            )

        print("  Generating script...")
        response = call_llm(prompt, system=_FUSION_LLM_SYSTEM)
        if not response:
            print("  LLM returned empty")
            prev_error = "LLM returned empty response"
            continue

        code = clean_code(response)
        try:
            compile(code, "<test>", "exec")
            print(f"  Script OK: {len(code.splitlines())} lines")
        except SyntaxError as e:
            print(f"  Syntax error: {e}")
            prev_error = f"Syntax error: {e}"
            continue

        # Submit to Fusion and wait
        result = submit_and_wait(code, attempt)

        if result["status"] == "done":
            step_size = result.get("step_size", 0) / 1024
            stl_size = result.get("stl_size", 0) / 1024
            print(f"  Fusion completed: STEP {step_size:.0f}KB, STL {stl_size:.0f}KB")

            # Visual verify
            stl = result.get("stl_path", STL_PATH)
            if Path(stl).exists() and Path(stl).stat().st_size > 100:
                print("  Running visual verification...")
                vis = verify_visual(
                    result.get("step_path", STEP_PATH), stl, GOAL, PARAMS
                )
                conf = vis.get("confidence", 0)
                verified = vis.get("verified")
                checks = [c for c in vis.get("checks", []) if isinstance(c, dict)]
                passed = sum(1 for c in checks if c.get("found"))
                issues = vis.get("issues", [])

                print(f"  Visual: {passed}/{len(checks)} checks, {conf:.0%} confidence")
                for c in checks:
                    s = "OK" if c.get("found") else "XX"
                    print(f"    [{s}] {c.get('feature', '?')[:70]}")
                for iss in issues[:3]:
                    print(f"    [!!] {iss}")

                if verified and conf >= 0.90:
                    print(f"\n{'='*50}")
                    print("PASSED — all visual checks met with >=90% confidence")
                    print(f"{'='*50}")
                    return

                # Build feedback for retry
                failed = [c for c in checks if not c.get("found")]
                prev_error = (
                    f"Visual verification failed ({conf:.0%}):\n"
                    + "\n".join(f"- MISSING: {c.get('feature','?')}: {c.get('notes','')[:100]}" for c in failed)
                    + "\n" + "\n".join(f"- {iss}" for iss in issues[:3])
                )
            else:
                print("  No STL to verify")
                prev_error = "STL file empty or missing"

        elif result["status"] == "error":
            prev_error = result.get("error", "unknown")[:500]
            print(f"  Fusion error: {prev_error[:150]}")

        elif result["status"] == "timeout":
            print("  Fusion timed out")
            prev_error = "Fusion bridge timed out — script may be too complex"

    print(f"\n{'='*50}")
    print(f"All {MAX_RETRIES} attempts exhausted. Best result saved.")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
