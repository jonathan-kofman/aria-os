"""
battery_run.py — drive a battery of SW + KiCad prompts through the live
dashboard, export STL / KiCad outputs, run visual-verify, and write a
single markdown report.

Run from repo root:
    python scripts/battery_run.py
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DASH = "http://127.0.0.1:8000"
# SW addin listener registers to http://localhost:7501/ (not +/all-hosts).
# Windows HTTP.sys rejects 127.0.0.1 requests with 400 Bad Hostname at the
# kernel layer before our handler runs — must use "localhost".
SW_LSN = "http://localhost:7501"
EXPORT_DIR = Path.home() / "AppData/Local/Temp/aria-exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

SW_PROMPTS = [
    ("flange_200",  "Flange 200mm OD 100mm bore 12mm thick, 8 M8 bolt holes on 160mm PCD, mild steel"),
    ("shaft_step",  "Stepped shaft 150mm long, 20mm dia ends, 30mm dia centre, 5mm wide DIN6885 keyway, steel"),
    ("heat_sink",   "Heat sink 80mm wide 60mm deep 10mm base, 8 fins 3mm thick 30mm tall, aluminium"),
    ("lbrkt_100",   "L-bracket 100x80x50mm 6mm wall, 6 M6 mounting holes, 304 stainless, indoor mount"),
    ("gear_24",     "Spur gear 24 teeth 50mm OD 15mm thick 10mm bore module 2, steel"),
]

KICAD_PROMPTS = [
    ("esp32_brk",   "ESP32-WROOM breakout 50x40mm 2-layer, USB-C power and data, 2 LEDs, 1 reset button"),
    ("buck_24_5",   "24V to 5V 3A buck regulator 40x30mm, screw terminal input, JST output"),
    ("amp_class_d", "Class-D audio amplifier 50W TDA7498 80x60mm, RCA input, screw terminal output"),
    ("motor_drv",   "Dual H-bridge motor driver DRV8871 12V 5A per channel 60x50mm, terminal blocks"),
    ("rfid_oled",   "RFID RC522 + ESP8266 + OLED 128x64 70x50mm, 3 push buttons, USB-C"),
]


def post(path: str, body: dict, *, timeout: int = 180) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{DASH}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def saveas(path: str) -> tuple[int, dict]:
    data = json.dumps({"kind": "saveAs",
                          "params": {"path": path, "format": "stl"}}).encode("utf-8")
    req = urllib.request.Request(
        f"{SW_LSN}/op", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def sw_new_doc() -> None:
    """Force the SW addin to open a fresh part doc before each build —
    pile-up of stale docs causes saveAs to grab the wrong handle."""
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{SW_LSN}/new_doc", data=b"{}",
                                       headers={"Content-Type": "application/json"},
                                       method="POST"),
            timeout=20,
        )
    except Exception:
        pass


def run_sw_prompt(slug: str, goal: str) -> dict:
    t0 = time.time()
    res: dict = {"slug": slug, "goal": goal, "kind": "sw"}

    # 1. Clarify gate
    s, c = post("/api/clarify", {"goal": goal}, timeout=60)
    res["clarify_ok"] = bool(c.get("enough_info"))
    res["clarify_reason"] = c.get("skipped_reason") or c.get("part_family") or "?"
    if not res["clarify_ok"]:
        res["wall_s"] = round(time.time() - t0, 1)
        res["status"] = "clarify_blocked"
        res["clarifications"] = [q.get("text") if isinstance(q, dict) else str(q)
                                    for q in (c.get("clarifications") or [])][:3]
        return res

    # 2. Build (open a fresh SW doc first — addin's old _model handle
    #    can otherwise persist across runs and break saveAs at the end)
    sw_new_doc()
    s, b = post("/api/cad/text-to-part",
                {"goal": goal, "cad": "solidworks", "quality_tier": "balanced"},
                timeout=300)
    res["status_code"] = s
    res["planned"] = b.get("n_ops_planned")
    res["succeeded"] = b.get("n_ops_succeeded")
    res["failed_at"] = b.get("failed_at")
    res["build_ok"] = bool(b.get("ok"))

    if not res["build_ok"]:
        res["wall_s"] = round(time.time() - t0, 1)
        res["status"] = "build_failed"
        return res

    # 3. Save STL — let the SW op chain settle after the heavy
    #    /api/cad/text-to-part response closes; the addin's HTTP
    #    listener is single-threaded so back-to-back POSTs from the
    #    same urllib pool can race against the rebuild we just did.
    #    Retry once with a longer wait if the first attempt fails.
    stl_path = str(EXPORT_DIR / f"battery_{slug}.stl")
    Path(stl_path).unlink(missing_ok=True)
    time.sleep(0.5)
    s, sav = saveas(stl_path)
    sw_result = sav.get("result") or {}
    if not sw_result.get("ok"):
        time.sleep(2.0)
        s, sav = saveas(stl_path)
        sw_result = sav.get("result") or {}
    if not sw_result.get("ok"):
        res["status"] = "saveas_failed"
        res["saveas_error"] = (sw_result.get("error")
                                  or sav.get("error")
                                  or "unknown")[:140]
        res["wall_s"] = round(time.time() - t0, 1)
        return res
    res["stl_bytes"] = Path(stl_path).stat().st_size if Path(stl_path).is_file() else 0

    # 4. Visual verify
    s, ve = post("/api/native_eval",
                  {"goal": goal, "stl_path": stl_path,
                   "iteration": 1, "max_iterations": 1},
                  timeout=240)
    rr = ve.get("result") or {}
    res["verdict"] = ve.get("verdict")
    res["confidence"] = rr.get("confidence")
    checks = rr.get("checks") or []
    res["passed_checks"] = sum(1 for c in checks if c.get("found") is True)
    res["total_checks"] = len(checks)
    res["bbox_check"] = next((c for c in checks
                                  if "width" in (c.get("feature") or "").lower()), {}).get("notes", "")
    res["status"] = "ok"
    res["wall_s"] = round(time.time() - t0, 1)
    return res


def run_kicad_prompt(slug: str, goal: str) -> dict:
    t0 = time.time()
    res: dict = {"slug": slug, "goal": goal, "kind": "kicad"}
    s, c = post("/api/clarify", {"goal": goal}, timeout=60)
    res["clarify_ok"] = bool(c.get("enough_info"))
    res["clarify_reason"] = c.get("skipped_reason") or c.get("part_family") or "?"
    if not res["clarify_ok"]:
        res["wall_s"] = round(time.time() - t0, 1)
        res["status"] = "clarify_blocked"
        res["clarifications"] = [q.get("text") if isinstance(q, dict) else str(q)
                                    for q in (c.get("clarifications") or [])][:3]
        return res

    s, b = post("/api/ecad/text-to-board", {"goal": goal}, timeout=300)
    res["status_code"] = s
    res["board_name"] = b.get("board_name")
    res["board_dims"] = (f"{b.get('board_w_mm')}x{b.get('board_h_mm')}"
                            if b.get("board_w_mm") else None)
    res["n_components"] = b.get("n_components")
    res["n_layers"] = b.get("n_layers")
    ge = b.get("gerber_export") or {}
    res["gerbers"] = bool(ge.get("available"))
    res["gerber_dir"] = (ge.get("gerber_dir") or "").split("\\")[-1][:40]
    res["build_ok"] = bool(b.get("ok"))
    res["error"] = (b.get("error") or "")[:150] if not res["build_ok"] else ""
    res["status"] = "ok" if res["build_ok"] else "build_failed"
    res["wall_s"] = round(time.time() - t0, 1)
    return res


def main() -> None:
    out: list[dict] = []
    for slug, goal in SW_PROMPTS:
        print(f"--- SW: {slug} ---", flush=True)
        out.append(run_sw_prompt(slug, goal))
        print(f"     -> {out[-1].get('status')} ({out[-1].get('wall_s')}s)", flush=True)
    for slug, goal in KICAD_PROMPTS:
        print(f"--- KiCad: {slug} ---", flush=True)
        out.append(run_kicad_prompt(slug, goal))
        print(f"     -> {out[-1].get('status')} ({out[-1].get('wall_s')}s)", flush=True)

    md = ["# Battery results\n",
            "## SW (port 7501)\n",
            "| slug | clarify | planned | succeeded | verdict | bbox | wall(s) |",
            "|------|---------|---------|-----------|---------|------|---------|"]
    for r in out:
        if r["kind"] != "sw":
            continue
        md.append(
            f"| {r['slug']} | {r.get('clarify_ok')} | {r.get('planned','-')} | "
            f"{r.get('succeeded','-')} | {r.get('verdict','-')} | "
            f"{(r.get('bbox_check') or '')[:50]} | {r.get('wall_s')} |"
        )
    md += ["\n## KiCad (port 7505)\n",
              "| slug | clarify | build_ok | dims | layers | comps | gerbers | wall(s) |",
              "|------|---------|----------|------|--------|-------|---------|---------|"]
    for r in out:
        if r["kind"] != "kicad":
            continue
        md.append(
            f"| {r['slug']} | {r.get('clarify_ok')} | {r.get('build_ok')} | "
            f"{r.get('board_dims') or '-'} | {r.get('n_layers') or '-'} | "
            f"{r.get('n_components') or '-'} | {r.get('gerbers')} | {r.get('wall_s')} |"
        )

    report = Path("outputs") / "battery_report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nWrote {report}")
    print("\n".join(md))


if __name__ == "__main__":
    main()
