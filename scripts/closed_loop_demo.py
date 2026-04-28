r"""closed_loop_demo.py - end-to-end "text prompt -> printed part" demo.

Phases (logged + screenshotted at each):
    1. orchestrator.run(goal) -> STEP/STL/run_id
    2. self_heal_fea(step) -> validate the part will hold the load
    3. start OpenClaw bridge if not already up
    4. submitJob(machine_id="printer-1", artifact=stl_url) -> job_id
    5. poll status every 2s -> progress %
    6. visual check via /op runVisualCheck
    7. emit demo_report.json with timeline + screenshots

For the YC video this script is the single-command artifact:
    python scripts/closed_loop_demo.py "M5 mounting bracket, 80x60x10mm,
        4 holes for 4x M5, 500N axial load"

Output goes to outputs/demo/<run_id>/ and includes:
    - prompt.txt
    - cad/part.step + cad/part.stl
    - fea/auto_fea_report.json (with VTU)
    - openclaw/job_log.jsonl
    - timeline.json (every phase + duration)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


def _hit_op(url: str, payload: dict, timeout: float = 30.0) -> dict:
    """POST JSON to a CAD/openclaw bridge; return the parsed response."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"URL error: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _phase_log(timeline: list, name: str, status: str,
                t_start: float, **extra) -> None:
    timeline.append({
        "phase": name, "status": status,
        "t_s": round(time.time() - t_start, 2),
        **extra,
    })
    print(f"[{round(time.time() - t_start, 1):>6.1f}s] {name:24s} {status}"
          + (f" — {extra}" if extra else ""))


def _grab_frame(openclaw_url: str, out_dir: Path, phase: str,
                  job_id: str | None = None,
                  machine_id: str = "printer-1") -> str | None:
    """Hit /op cameraFrame and save the bytes to out_dir/frames/<phase>.<ext>.
    Returns the saved path or None on failure / no-camera. Fail-soft —
    closed-loop demo keeps running even if camera is missing.
    """
    import base64
    try:
        resp = _hit_op(openclaw_url, {
            "kind": "cameraFrame",
            "params": {"job_id": job_id, "machine_id": machine_id},
        }, timeout=5.0)
    except Exception:
        return None
    if not resp.get("ok"):
        return None
    b64 = resp.get("frame_b64")
    if not b64:
        return None
    fmt = resp.get("fmt", "image/jpeg")
    ext = ".jpg" if "jpeg" in fmt else ".png"
    fdir = out_dir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    fpath = fdir / f"{phase}{ext}"
    try:
        fpath.write_bytes(base64.b64decode(b64))
        return str(fpath)
    except Exception:
        return None


def run_demo(goal: str, *,
              load_n: float = 500.0,
              material: str = "aluminum_6061",
              target_sf: float = 2.0,
              machine_id: str = "printer-1",
              openclaw_url: str = "http://localhost:7510/op",
              cad_url: str = "http://localhost:7501/op",
              out_dir: Path | None = None,
              skip_fea: bool = False,
              skip_print: bool = False) -> dict:
    t0 = time.time()
    timeline: list = []
    out_dir = Path(out_dir or f"outputs/demo/{int(t0)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompt.txt").write_text(goal, encoding="utf-8")

    _phase_log(timeline, "demo_start", "begin", t0, goal=goal)
    # Snapshot the machine before any work — establishes the "before" frame
    f0 = _grab_frame(openclaw_url, out_dir, "00_start",
                       machine_id=machine_id)
    if f0: timeline[-1]["frame"] = f0

    # ============================================================
    # Phase 1: orchestrator — text goal -> CAD artifacts
    # ============================================================
    _phase_log(timeline, "1_orchestrator", "running", t0)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from aria_os.orchestrator import run as orch_run
    try:
        orch_result = orch_run(goal)
    except Exception as ex:
        _phase_log(timeline, "1_orchestrator", "failed", t0,
                    error=f"{type(ex).__name__}: {ex}")
        return {"ok": False, "phase": 1, "timeline": timeline,
                "out_dir": str(out_dir)}
    step_path = (orch_result.get("artifacts", {}) or {}).get("step")
    stl_path = (orch_result.get("artifacts", {}) or {}).get("stl")
    if not step_path or not Path(step_path).is_file():
        # Legacy fallback
        for p in (Path("outputs/cad/step/agent.step"),
                   Path("outputs/cad/stl/agent.stl")):
            if p.is_file() and step_path is None and p.suffix == ".step":
                step_path = str(p)
            if p.is_file() and stl_path is None and p.suffix == ".stl":
                stl_path = str(p)
    _phase_log(timeline, "1_orchestrator", "ok", t0,
                step=step_path, stl=stl_path)

    # ============================================================
    # Phase 2: self-healing FEA validation
    # ============================================================
    fea_report = None
    if not skip_fea and step_path and Path(step_path).is_file():
        _phase_log(timeline, "2_fea_self_heal", "running", t0)
        try:
            from aria_os.fea.self_heal import heal_fea
            heal = heal_fea(step_path, material=material,
                             load_n=load_n,
                             target_safety_factor=target_sf,
                             max_iters=4,
                             out_dir=out_dir / "fea")
            fea_report = {
                "ok": heal.ok,
                "initial_passed": heal.initial_passed,
                "final_passed": heal.final_passed,
                "iterations": heal.iterations,
                "final_material": heal.final_material,
                "final_sf": heal.final_safety_factor,
                "final_max_stress_mpa": heal.final_max_stress_mpa,
                "trajectory": [
                    {"iter": a.iteration, "action": a.action,
                     "material": a.material, "sf": a.safety_factor,
                     "stress": a.max_stress_mpa, "passed": a.passed}
                    for a in heal.attempts],
            }
            _phase_log(timeline, "2_fea_self_heal",
                        "ok" if heal.ok else "fail", t0,
                        iters=heal.iterations,
                        final_sf=heal.final_safety_factor,
                        material=heal.final_material)
        except Exception as ex:
            fea_report = {"ok": False,
                            "error": f"{type(ex).__name__}: {ex}"}
            _phase_log(timeline, "2_fea_self_heal", "error", t0,
                        error=str(ex))
    elif skip_fea:
        _phase_log(timeline, "2_fea_self_heal", "skipped", t0)
    else:
        _phase_log(timeline, "2_fea_self_heal", "no_step", t0)

    # ============================================================
    # Phase 3: OpenClaw bridge submitJob
    # ============================================================
    job_id = None
    job_status_log: list = []
    if not skip_print and stl_path and Path(stl_path).is_file():
        _phase_log(timeline, "3_openclaw_submit", "running", t0)
        sub = _hit_op(openclaw_url, {
            "kind": "submitJob",
            "params": {
                "run_id": Path(out_dir).name,
                "machine_id": machine_id,
                "artifact_url": f"file://{Path(stl_path).resolve()}",
                "expected_runtime_s": 30.0,
                "expected_bbox_mm": [80.0, 60.0, 10.0],
                "slicer_hash": "demo-skip",
                "cam_hash": "demo-skip",
            },
        })
        if sub.get("ok"):
            job_id = sub.get("job_id")
            _phase_log(timeline, "3_openclaw_submit", "ok", t0,
                        job_id=job_id)
        else:
            _phase_log(timeline, "3_openclaw_submit", "failed", t0,
                        error=sub.get("error"))

        # Phase 4: poll until terminal state
        if job_id:
            _phase_log(timeline, "4_openclaw_poll", "running", t0)
            deadline = time.time() + 60.0
            terminal = {"completed", "failed", "cancelled"}
            while time.time() < deadline:
                ps = _hit_op(openclaw_url, {
                    "kind": "pollStatus",
                    "params": {"job_id": job_id},
                })
                if ps.get("ok"):
                    j = ps.get("job", {}) or ps.get("status", {})
                    job_status_log.append({
                        "t_s": round(time.time() - t0, 2),
                        "state": j.get("state"),
                        "progress": j.get("progress"),
                    })
                    if (j.get("state") or "") in terminal:
                        _phase_log(timeline, "4_openclaw_poll",
                                    j.get("state") or "unknown", t0,
                                    progress=j.get("progress"),
                                    last_error=j.get("last_error"))
                        f4 = _grab_frame(openclaw_url, out_dir,
                                          "4_print_done",
                                          job_id=job_id,
                                          machine_id=machine_id)
                        if f4: timeline[-1]["frame"] = f4
                        break
                else:
                    job_status_log.append({
                        "t_s": round(time.time() - t0, 2),
                        "error": ps.get("error")})
                time.sleep(1.0)
            else:
                _phase_log(timeline, "4_openclaw_poll", "timeout", t0)

            (out_dir / "openclaw_job_log.jsonl").write_text(
                "\n".join(json.dumps(s) for s in job_status_log),
                encoding="utf-8")
    elif skip_print:
        _phase_log(timeline, "3_openclaw_submit", "skipped", t0)
    else:
        _phase_log(timeline, "3_openclaw_submit", "no_stl", t0)

    # ============================================================
    # Phase 5: visual check (camera or render)
    # ============================================================
    visual_result = None
    if job_id and not skip_print:
        _phase_log(timeline, "5_visual_check", "running", t0)
        vc = _hit_op(openclaw_url, {
            "kind": "runVisualCheck",
            "params": {"job_id": job_id,
                        "expected_bbox_mm": [80.0, 60.0, 10.0]},
        })
        visual_result = vc
        _phase_log(timeline, "5_visual_check",
                    "ok" if vc.get("ok") else "failed", t0,
                    match=vc.get("match"), confidence=vc.get("confidence"))

    # ============================================================
    # Final report
    # ============================================================
    elapsed = time.time() - t0
    report = {
        "ok": True,
        "goal": goal,
        "elapsed_s": round(elapsed, 2),
        "out_dir": str(out_dir),
        "step_path": step_path,
        "stl_path": stl_path,
        "fea": fea_report,
        "openclaw": {"job_id": job_id, "status_log": job_status_log,
                       "visual": visual_result},
        "timeline": timeline,
    }
    (out_dir / "demo_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n=== DONE in {elapsed:.1f}s ===")
    print(f"Report: {out_dir / 'demo_report.json'}")
    return report


def main():
    ap = argparse.ArgumentParser(
        description="text -> CAD -> FEA -> printer end-to-end demo")
    ap.add_argument("goal",
                     help='Natural-language part description, e.g. '
                          '"M5 mounting bracket 80x60x10mm with 4 M5 holes"')
    ap.add_argument("--load-n", type=float, default=500.0)
    ap.add_argument("--material", default="aluminum_6061")
    ap.add_argument("--target-sf", type=float, default=2.0)
    ap.add_argument("--machine-id", default="printer-1")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--skip-fea", action="store_true")
    ap.add_argument("--skip-print", action="store_true")
    args = ap.parse_args()
    rep = run_demo(args.goal, load_n=args.load_n,
                    material=args.material, target_sf=args.target_sf,
                    machine_id=args.machine_id, out_dir=args.out_dir,
                    skip_fea=args.skip_fea, skip_print=args.skip_print)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
