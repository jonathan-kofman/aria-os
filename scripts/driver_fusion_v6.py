"""Turbopump v6: 5 attempts with accumulated feedback learning."""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OPENBLAS_NUM_THREADS"] = "1"

from aria_os.llm_client import call_llm
from aria_os.visual_verifier import verify_visual

JOBS_DIR = Path("outputs/cad/fusion_jobs")
PART_ID = "turbopump_v6"
MAX_ATTEMPTS = 5

SYSTEM = """You write Fusion 360 API scripts. Pre-defined: app, ui, design, rootComp, adsk, math.
Design is Parametric. All dimensions in CM. Do NOT set designType/close/create doc.

ARCHITECTURE: Create every feature as NewBodyFeatureOperation on construction planes.
Combine all bodies at the end. Never sketch on faces. Never reference bRepBodies by index during build.

COORDINATE SYSTEM: Fusion uses cm. XY = top view, Z = up. Origin is at (0,0,0).

Construction planes:
  rootComp.xYConstructionPlane  (horizontal)
  rootComp.xZConstructionPlane  (vertical front)
  rootComp.yZConstructionPlane  (vertical side)
  Offset: pi = rootComp.constructionPlanes.createInput()
          pi.setByOffset(rootComp.xYConstructionPlane, adsk.core.ValueInput.createByReal(Z_CM))
          plane = rootComp.constructionPlanes.add(pi)

COMBINE PATTERN (at end, after ALL bodies created):
  main = rootComp.bRepBodies.item(0)
  while rootComp.bRepBodies.count > 1:
      tool = adsk.core.ObjectCollection.create()
      tool.add(rootComp.bRepBodies.item(1))
      ci = rootComp.features.combineFeatures.createInput(main, tool)
      ci.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
      rootComp.features.combineFeatures.add(ci)

SHELL (after combine): rootComp.features.shellFeatures (select all faces, then exclude top)

CUTS (after shell): Sketch on XY, CutFeatureOperation, setAllExtent for through-holes.
Circular pattern: ObjectCollection with cut feature, circularPatternFeatures around zConstructionAxis.

Output ONLY Python code starting with 'import math'. No markdown."""

GOAL = """Turbopump housing. ALL coordinates in CM. FOLLOW THIS EXACT ORDER:

STEP 1 — Main cylinder + shell (do this FIRST while body is simple):
  Sketch circle r=6.0 on XY, extrude 18.0cm (NewBody)
  Shell with -0.8cm wall (this is easy on a simple cylinder)

STEP 2 — Bottom flange (NewBody, then Join):
  Sketch circle r=8.0 on XY, extrude 1.5cm (NewBody)
  Combine: join flange to main body

STEP 3 — Outlet pipe (NewBody, then Join):
  Offset XY plane at Z=18.0
  Sketch circle r=1.5 on offset plane, extrude 4.0cm up (NewBody)
  Combine: join pipe to main body

STEP 4 — 4 Ribs (each as NewBody, then Join one at a time):
  Rib A: XZ plane, rect (2.5, 1.5) to (5.2, 16.5), symmetric extrude ±0.15cm (NewBody), Join
  Rib B: YZ plane, rect (2.5, 1.5) to (5.2, 16.5), symmetric extrude ±0.15cm (NewBody), Join
  Rib C: XZ plane, rect (-5.2, 1.5) to (-2.5, 16.5), symmetric extrude ±0.15cm (NewBody), Join
  Rib D: YZ plane, rect (-5.2, 1.5) to (-2.5, 16.5), symmetric extrude ±0.15cm (NewBody), Join

STEP 5 — Cuts (after everything is joined):
  Bearing bore: circle r=2.5 at origin on offset XY at Z=18, cut 2.5cm deep
  Bolt holes: circle r=0.4 at (7.0, 0, 0) on XY, cut through all, circular pattern x6 around Z

KEY: Shell the cylinder BEFORE adding ribs. Join each rib individually right after creating it."""


def clean(response):
    code = response.strip()
    if "```" in code:
        m = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.DOTALL)
        if m:
            code = m.group(1).strip()
    elif not code.startswith(("import ", "#")):
        for i, line in enumerate(code.split("\n")):
            if line.strip().startswith(("import ", "#")):
                code = "\n".join(code.split("\n")[i:])
                break
    code = re.sub(r".*designType.*\n", "", code)
    code = re.sub(r".*doc\.close.*\n", "", code)
    code = re.sub(r".*documents\.add.*\n", "", code)
    return code


def submit_wait(code):
    for f in JOBS_DIR.glob("*.json"):
        f.unlink()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    step = str(Path(f"outputs/cad/step/{PART_ID}.step").resolve()).replace("\\", "/")
    stl = str(Path(f"outputs/cad/stl/{PART_ID}.stl").resolve()).replace("\\", "/")
    job = {"part_id": PART_ID, "goal": GOAL, "script": code, "step_path": step, "stl_path": stl}
    (JOBS_DIR / f"{PART_ID}.json").write_text(json.dumps(job, indent=2))

    done_f = JOBS_DIR / f"_done_{PART_ID}.json"
    err_f = JOBS_DIR / f"_err_{PART_ID}.json"
    t0 = time.time()
    while time.time() - t0 < 180:
        if done_f.exists():
            return {**json.loads(done_f.read_text()), "status": "done"}
        if err_f.exists():
            return json.loads(err_f.read_text())
        time.sleep(3)
    return {"status": "timeout"}


def main():
    all_feedback = []

    for attempt in range(MAX_ATTEMPTS):
        print(f"\n{'='*50}")
        print(f"Attempt {attempt+1}/{MAX_ATTEMPTS}")
        print(f"{'='*50}")

        prompt = GOAL
        if all_feedback:
            prompt += "\n\nPREVIOUS FAILURES — LEARN FROM THESE:\n"
            for i, fb in enumerate(all_feedback):
                prompt += f"\nAttempt {i+1}: {fb}\n"

        print("  Generating...")
        response = call_llm(prompt, system=SYSTEM)
        if not response:
            all_feedback.append("LLM empty response")
            continue

        code = clean(response)
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            all_feedback.append(f"Syntax: {e}")
            print(f"  Syntax error: {e}")
            continue

        print(f"  Script: {len(code.splitlines())} lines, submitting...")
        result = submit_wait(code)

        if result.get("status") == "error":
            err = result.get("error", "?")[:300]
            all_feedback.append(f"Fusion error: {err}")
            print(f"  Error: {err[:120]}")
            continue

        if result.get("status") == "timeout":
            all_feedback.append("Timeout 180s")
            print("  Timeout")
            continue

        step_kb = result.get("step_size", 0) / 1024
        stl_kb = result.get("stl_size", 0) / 1024
        print(f"  Done: STEP {step_kb:.0f}KB, STL {stl_kb:.0f}KB")

        stl = result.get("stl_path", "")
        if not Path(stl).exists() or Path(stl).stat().st_size < 100:
            all_feedback.append("STL empty")
            continue

        import trimesh
        m = trimesh.load(stl)
        d = m.bounds[1] - m.bounds[0]
        print(f"  Dims: {d[0]:.1f}x{d[1]:.1f}x{d[2]:.1f}mm, Faces: {len(m.faces)}")

        # Dimension sanity
        dim_fb = []
        if abs(max(d[0], d[1]) - 160) > 20:
            dim_fb.append(f"XY should be ~160mm, got {max(d[0],d[1]):.0f}")
        if d[2] < 170 or d[2] > 250:
            dim_fb.append(f"Z should be ~195-220mm, got {d[2]:.0f}")

        print("  Verifying...")
        vis = verify_visual(
            result.get("step_path", ""), stl,
            "Turbopump housing: 120mm OD, 180mm tall, 8mm wall, flange 160mm with 6 bolt holes, bore 50mm top, outlet pipe top, 4 internal ribs",
            {"od_mm": 120, "height_mm": 180, "wall_mm": 8, "n_bolts": 6, "bore_mm": 50},
        )
        checks = [c for c in vis.get("checks", []) if isinstance(c, dict)]
        passed = sum(1 for c in checks if c.get("found"))
        conf = vis.get("confidence", 0)
        issues = vis.get("issues", [])

        print(f"  Visual: {passed}/{len(checks)}, {conf:.0%}")
        for c in checks:
            s = "OK" if c.get("found") else "XX"
            print(f"    [{s}] {c.get('feature','?')[:60]}")

        if conf >= 0.90 and passed == len(checks):
            print(f"\n{'='*50}")
            print(f"PASSED — {conf:.0%}, {passed}/{len(checks)}")
            print(f"{'='*50}")
            return

        # Accumulate specific feedback
        failed = [f"{c.get('feature','?')}: {c.get('notes','')[:60]}" for c in checks if not c.get("found")]
        fb = f"Visual {passed}/{len(checks)} ({conf:.0%}). Dims: {d[0]:.0f}x{d[1]:.0f}x{d[2]:.0f}mm."
        if dim_fb:
            fb += " DIM: " + "; ".join(dim_fb)
        if failed:
            fb += " MISSING: " + "; ".join(failed[:3])
        if issues:
            fb += " ISSUES: " + "; ".join(issues[:2])
        all_feedback.append(fb)

    print(f"\n{'='*50}")
    print(f"Exhausted {MAX_ATTEMPTS} attempts. Feedback history:")
    for i, fb in enumerate(all_feedback):
        print(f"  {i+1}: {fb[:120]}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
