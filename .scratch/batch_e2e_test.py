"""
E2E batch validation test — runs diverse goals through the full pipeline
and reports pass/fail, template used, and visual verify result.
Sequential (shared output path).
"""
import subprocess, sys, time, re, json
from pathlib import Path

REPO = Path(__file__).parent.parent
PYTHON = sys.executable

GOALS = [
    # (goal, expected_template_hint)
    ("centrifugal fan impeller 150mm OD 30mm bore 6 backward-curved blades aluminium",  "impeller"),
    ("heat sink 60mm wide 40mm deep 12 aluminium fins",                                  "heat_sink"),
    ("gt2 timing pulley 20 teeth 5mm bore 7mm belt width",                              "timing_pulley"),
    ("pcb enclosure 100x80x40mm abs plastic with 4 mounting bosses",                    "pcb_enclosure"),
    ("involute spur gear 40 teeth 2mm module 20mm height steel",                        "involute_gear"),
    ("deep groove ball bearing 6205 25mm bore 52mm OD 15mm wide",                       "ball_bearing"),
]

def run_goal(goal: str) -> dict:
    t0 = time.time()
    result = subprocess.run(
        [PYTHON, "run_aria_os.py", goal],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
    )
    elapsed = time.time() - t0
    out = result.stdout + result.stderr

    step = Path(REPO / "outputs/cad/step/agent.step")
    stl  = Path(REPO / "outputs/cad/stl/agent.stl")
    has_step = step.is_file() and step.stat().st_size > 1000
    has_stl  = stl.is_file()  and stl.stat().st_size  > 1000

    # Extract template used
    tmpl_match = re.search(r"\[DesignerAgent\].*?template[:\s]+(\w+)", out)
    tmpl = tmpl_match.group(1) if tmpl_match else "?"

    # Extract visual verify result
    vv_match = re.search(r"(PASS|FAIL)\b.*?confidence[:\s]*([\d.]+)", out, re.IGNORECASE)
    if not vv_match:
        vv_match = re.search(r"overall.*?(PASS|FAIL)", out, re.IGNORECASE)
    vv = vv_match.group(1).upper() if vv_match else "?"
    conf = vv_match.group(2) if (vv_match and vv_match.lastindex >= 2) else "?"

    # Iteration count
    iters = len(re.findall(r"Iteration \d+", out))

    return {
        "step": has_step,
        "stl":  has_stl,
        "template": tmpl,
        "vv": vv,
        "conf": conf,
        "iters": iters,
        "elapsed_s": round(elapsed),
        "rc": result.returncode,
        "tail": out[-600:],
    }

print("=" * 70)
print("E2E BATCH VALIDATION")
print("=" * 70)

results = []
for goal, hint in GOALS:
    print(f"\n>>> {goal[:60]}")
    r = run_goal(goal)
    ok_file = "STEP+STL" if (r["step"] and r["stl"]) else ("STEP" if r["step"] else "NO-FILE")
    status = "PASS" if (r["step"] and r["vv"] == "PASS") else ("FILE-OK" if r["step"] else "FAIL")
    print(f"    status={status}  file={ok_file}  tmpl={r['template']}  "
          f"vv={r['vv']}({r['conf']})  iters={r['iters']}  {r['elapsed_s']}s")
    if status != "PASS":
        print(f"    TAIL: {r['tail'][-200:]}")
    r["goal"] = goal
    r["hint"] = hint
    results.append(r)

passed = sum(1 for r in results if r["step"] and r["vv"] == "PASS")
files  = sum(1 for r in results if r["step"])
print(f"\n{'='*70}")
print(f"RESULT: {passed}/{len(results)} full pass  |  {files}/{len(results)} produced files")
print(f"{'='*70}")

# Save results
out_path = REPO / ".scratch/batch_e2e_results.json"
out_path.write_text(json.dumps(results, indent=2))
print(f"Results saved to {out_path}")
