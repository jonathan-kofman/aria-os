"""Visually verify the Phase E pipeline end-to-end through SW + KiCad.

For each Phase E prompt:
  1. Pre-flight the SW addin (auto-redeploy stale DLL)
  2. beginPlan → fresh part doc
  3. LLM-plan ops → dispatch each op to SW addin live
  4. saveAs to .step
  5. Render the .step to PNG via cadquery + trimesh + matplotlib

Output goes to outputs/phase_e_visual/<name>.png. The driver returns
a JSON summary of which prompts produced a usable bundle and which
hit the planner / op / save / render boundary.

KiCad is exercised only when a prompt has electronics content;
Phase E is all-mechanical so KiCad gets no real workout here. Add
ECAD prompts to PROMPTS_ECAD if you want that surface tested too.

Run:
    python scripts/phase_e_visual_verify.py [--only rocket_nozzle ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

PROMPTS: dict[str, str] = {
    "rocket_nozzle":
        "bell-shaped rocket nozzle, throat 25mm, exit 80mm, length 120mm, "
        "6061 aluminium, M6 mounting flange",
    "motorcycle_frame":
        "motorcycle subframe bracket, steel, 200mm long, 40mm wide, "
        "10mm thick, M10 mount holes",
    "satellite_chassis":
        "1U cubesat chassis frame, 100x100x100mm, 6061 aluminium, "
        "4 lateral panels with M3 holes",
    "camera_gimbal":
        "camera gimbal yoke ring, 50mm OD, 40mm ID, 10mm thick, "
        "carbon fibre, M3 servo mount holes",
    "telescope_mount":
        "telescope mount fork bracket, 150mm tall, 100mm wide, "
        "8mm thick aluminium, 1/4-20 tripod thread",
}

PORT = 7501
BASE = f"http://localhost:{PORT}"
OUT_DIR = REPO_ROOT / "outputs" / "phase_e_visual"


def _post(path: str, body: dict, timeout: float = 300.0) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _render_step_to_png(step_path: Path, out_png: Path,
                          title: str = "") -> bool:
    """Render a STEP file to a 3D PNG. Returns True on success."""
    try:
        import cadquery as cq
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import tempfile
        import trimesh
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    except ImportError as exc:
        print(f"  [render] missing dep: {exc}")
        return False

    try:
        shp = cq.importers.importStep(str(step_path))
    except Exception as exc:
        print(f"  [render] importStep failed: {exc}")
        return False

    try:
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as t:
            stl_path = t.name
        cq.exporters.export(shp, stl_path, "STL")
        m = trimesh.load(stl_path, force="mesh")
    except Exception as exc:
        print(f"  [render] STL conversion failed: {exc}")
        return False

    try:
        fig = plt.figure(figsize=(8, 8))
        ax = fig.add_subplot(111, projection="3d")
        verts = m.vertices
        if len(m.faces) == 0:
            print("  [render] empty mesh")
            return False
        ax.plot_trisurf(verts[:, 0], verts[:, 1], verts[:, 2],
                          triangles=m.faces, alpha=0.7,
                          edgecolor="steelblue", linewidth=0.05)
        ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)")
        ax.set_zlabel("Z (mm)")
        if title:
            ax.set_title(title, fontsize=10)
        ax.view_init(elev=22, azim=42)
        plt.tight_layout()
        plt.savefig(str(out_png), dpi=120, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception as exc:
        print(f"  [render] matplotlib failed: {exc}")
        try: plt.close(fig)
        except Exception: pass
        return False


def run_prompt(name: str, goal: str, quality_tier: str = "fast"
                ) -> dict:
    out: dict = {"name": name, "goal": goal,
                  "n_ops_planned": 0, "n_ops_succeeded": 0,
                  "step_path": None, "png_path": None,
                  "first_op_failed": None, "errors": []}
    t0 = time.time()
    try:
        # 1. Plan via LLM (fast tier — Phase E is a smoke test, not premium).
        try:
            from aria_os.native_planner.llm_planner import plan_from_llm
        except Exception as exc:
            out["errors"].append(f"planner import: {exc}")
            out["duration_s"] = round(time.time() - t0, 1)
            return out
        try:
            ops = plan_from_llm(goal, {}, quality=quality_tier,
                                 repo_root=REPO_ROOT)
        except Exception as exc:
            out["errors"].append(f"plan_from_llm: {exc}")
            out["duration_s"] = round(time.time() - t0, 1)
            return out
        out["n_ops_planned"] = len(ops or [])
        if not ops:
            out["errors"].append("planner returned no ops")
            out["duration_s"] = round(time.time() - t0, 1)
            return out

        # 2. Fresh part doc.
        try:
            _post("/op", {"kind": "beginPlan", "params": {}}, timeout=30.0)
        except Exception as exc:
            out["errors"].append(f"beginPlan: {exc}")

        # 3. Dispatch each op.
        n_ok = 0
        for i, op in enumerate(ops):
            payload = {"kind": op.get("kind"),
                        "params": op.get("params", {})}
            try:
                r = _post("/op", payload, timeout=120.0)
            except Exception as exc:
                if out["first_op_failed"] is None:
                    out["first_op_failed"] = i
                out["errors"].append(
                    f"op[{i}] {payload['kind']}: {type(exc).__name__}: {exc}")
                continue
            ok = (r.get("result") or {}).get("ok", r.get("ok"))
            if ok:
                n_ok += 1
            else:
                if out["first_op_failed"] is None:
                    out["first_op_failed"] = i
                err = (r.get("result") or {}).get("error") or r.get("error")
                out["errors"].append(f"op[{i}] {payload['kind']}: {err}")
        out["n_ops_succeeded"] = n_ok

        # 4. Export STEP. Even if some ops failed, the partial geometry
        #    that DID land is still useful for visual diagnosis.
        step_path = OUT_DIR / f"{name}.step"
        try:
            save = _post("/op", {
                "kind": "saveAs",
                "params": {"path": str(step_path).replace("\\", "/")},
            }, timeout=120.0)
            if (save.get("result") or {}).get("ok") \
                    and step_path.is_file() and step_path.stat().st_size > 0:
                out["step_path"] = str(step_path)
                out["step_size"] = step_path.stat().st_size
        except Exception as exc:
            out["errors"].append(f"saveAs: {exc}")

        # 5. Render PNG.
        if out["step_path"]:
            png_path = OUT_DIR / f"{name}.png"
            if _render_step_to_png(step_path, png_path,
                                     title=f"{name}: {goal[:50]}"):
                out["png_path"] = str(png_path)

    except Exception as exc:
        out["errors"].append(
            f"unhandled: {type(exc).__name__}: {exc}\n"
            + traceback.format_exc(limit=3))
    out["duration_s"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="+", default=None,
                      help=f"subset of prompts (have: {list(PROMPTS)})")
    ap.add_argument("--quality", default="fast",
                      help="planner LLM tier (fast|balanced|premium)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Pre-flight ensures SW + addin are ready, redeploys stale DLL.
    try:
        from sw_preflight import ensure_ready  # type: ignore
        ensure_ready(PORT)
    except SystemExit as exc:
        print(f"[preflight failed] {exc}")
        return 2
    except Exception as exc:
        print(f"[preflight unavailable: {exc}] continuing with raw probe")

    # 2. Iterate prompts.
    selected = (PROMPTS if not args.only
                else {k: v for k, v in PROMPTS.items() if k in args.only})
    summary: list[dict] = []
    for name, goal in selected.items():
        print(f"\n=== {name} ============================================")
        print(f"goal: {goal}")
        rec = run_prompt(name, goal, quality_tier=args.quality)
        summary.append(rec)
        print(f"  planned={rec['n_ops_planned']}  ok={rec['n_ops_succeeded']}  "
              f"step={'Y' if rec.get('step_path') else 'N'}  "
              f"png={'Y' if rec.get('png_path') else 'N'}  "
              f"({rec['duration_s']}s)")
        if rec.get("errors"):
            for e in rec["errors"][:3]: print(f"  ! {e}")

    out_json = OUT_DIR / "summary.json"
    out_json.write_text(json.dumps(summary, indent=2, default=str), "utf-8")
    print(f"\n[done] summary at {out_json}")
    n_pass = sum(1 for r in summary if r.get("png_path"))
    print(f"[done] {n_pass}/{len(summary)} prompts produced a renderable STEP")
    return 0 if n_pass == len(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
