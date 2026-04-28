"""
aria_server.py
FastAPI backend for the ARIA-OS agentic UI.

Endpoints:
  POST /api/generate          — run ARIA pipeline with a goal string
  GET  /api/log/stream        — SSE stream of pipeline events
  GET  /api/parts             — list generated parts from learning_log.json
  GET  /api/parts/{id}/stl    — serve a part's STL file
  GET  /api/sessions          — list session logs from sessions/
  GET  /api/cem               — latest CEM check results from learning_log
"""
import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from aria_os import event_bus

REPO_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="ARIA-OS Server", version="1.0.0")

# CORS — Vite dev (5173), legacy Next (3000), Streamlit (8501), and any
# CAD plugin WebView origin (fusion://, file://, etc). Tighten via the
# ARIA_CORS_ORIGINS env var in prod.
import os as _os
_cors_raw = _os.environ.get(
    "ARIA_CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:8501,null").strip()
_origins = ["*"] if _cors_raw == "*" else [
    o.strip() for o in _cors_raw.split(",") if o.strip()
]
# Fusion WebView2 and some sandboxed iframes send `Origin: null`.
# Onshape-embedded panels load over cloudflare/ngrok tunnels.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=(
        r"^(null"
        r"|https?://(localhost|127\.0\.0\.1)(:\d+)?"
        r"|https://[a-z0-9-]+\.trycloudflare\.com"
        r"|https://[a-z0-9-]+\.ngrok-free\.app"
        r"|https://[a-z0-9-]+\.ngrok\.io"
        r"|https://cad\.onshape\.com)$"
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class ClarifyRequest(BaseModel):
    goal: str
    quality_tier: str = "fast"  # cheap — just a classification LLM call


@app.post("/api/clarify")
async def clarify_endpoint(req: ClarifyRequest):
    """Pre-planner pass: ask the LLM what production-critical fields
    are missing from the user's prompt. Panel renders the result as an
    inline form; user fills it; panel resubmits to /api/generate with
    the enriched spec.

    Fast path: if the prompt matches a hardcoded planner, skip clarify
    entirely — those planners have engineering defaults for every field
    and the clarify LLM just invents irrelevant questions."""
    from aria_os.spec_extractor import extract_spec
    spec = extract_spec(req.goal) or {}
    # Hardcoded-planner fast path (flange / impeller / bracket) — the
    # planner has sensible defaults and doesn't need clarifications.
    try:
        from aria_os.native_planner.dispatcher import is_supported
        if is_supported(req.goal):
            return {
                "enough_info": True,
                "part_family": "hardcoded",
                "summary": req.goal[:120],
                "clarifications": [],
                "regex_spec": spec,
                "skipped_reason": "hardcoded planner matches — defaults OK",
            }
    except Exception:
        pass
    from aria_os.agents.clarify_agent import clarify
    result = clarify(req.goal, spec,
                      quality=req.quality_tier,
                      repo_root=REPO_ROOT)
    result["regex_spec"] = spec
    return result


class GenerateRequest(BaseModel):
    goal: str
    max_attempts: int = 3
    # New in 2026-04-21 ChatPanel: route-mode + LLM-tier from the composer.
    # Backward-compat: old clients that don't send these keep the default
    # mechanical/balanced path.
    mode: str = "mechanical"           # mechanical | electrical | assembly
    quality_tier: str = "balanced"     # fast | balanced | premium
    attachments: list[dict] | None = None
    # Live Fusion/Rhino/Onshape state, optional. Populated by the panel
    # before submit via bridge.getSelection / getFeatureTree /
    # getUserParameters. Enables selection-aware and delta prompts.
    host_context: dict | None = None


# --------------------------------------------------------------------------- #
# /api/cad/synthesize-args — LLM-in-the-loop fallback for native CAD ops
# whose static fallback chain has exhausted. Each plugin (SW addin, Rhino,
# Fusion, Onshape, KiCad) calls this AFTER its own recipe-cache + 11-combo
# fallback grid has all returned null. The LLM proposes the next arg combo
# to try based on the failure context. Wins get persisted by the plugin
# back into its local recipe cache.
#
# Strict per-CAD scope — the LLM is told "this is a SolidWorks COM call,
# do not suggest switching CADs." Same for Rhino/Fusion/Onshape/KiCad.
# Each CAD remains its own self-contained autonomous generator.
# --------------------------------------------------------------------------- #

class SynthesizeArgsRequest(BaseModel):
    cad: str                          # "solidworks" | "rhino" | "fusion" | "onshape" | "kicad"
    op: str                           # e.g. "cut_extrude_blind"
    method: str                       # native API method name, e.g. "FeatureCut4"
    signature: str | None = None      # human-readable arg list of the method
    prior_attempts: list[dict] = []   # arg combos already tried
    failure_msgs: list[str] = []      # what each prior attempt produced/threw
    context: dict | None = None       # body bbox, sketch info, plane normal, etc.


@app.post("/api/cad/synthesize-args")
def synthesize_args(req: SynthesizeArgsRequest):
    """Ask an LLM for the next-best native-API arg combo to try.

    Plugins call this when their static fallback chain has exhausted.
    Response: { "args": {...} } with one new arg combo, OR
              { "args": null, "reason": "..." } if no good idea.

    The plugin then runs its native-API call with these args, persists
    a win to its local recipe cache, and never asks again for the same
    intent until the cached recipe stops working.
    """
    from aria_os.llm_client import call_llm

    cad = (req.cad or "").lower().strip()
    if cad not in {"solidworks", "rhino", "fusion", "onshape", "kicad"}:
        raise HTTPException(400, f"unknown cad: {cad!r}")

    cad_label = {
        "solidworks": "SolidWorks COM API",
        "rhino":      "RhinoCommon (RhinoCommon.dll)",
        "fusion":     "Autodesk Fusion 360 (adsk.fusion / adsk.core)",
        "onshape":    "Onshape REST API (BTMFeature/FeatureScript)",
        "kicad":      "KiCad pcbnew Python bindings",
    }[cad]

    system = (
        f"You suggest one next-best argument combination for a {cad_label} "
        f"native call that has been failing. You MUST stay within {cad_label} — "
        f"never suggest switching to a different CAD. Respond ONLY with a "
        f"single JSON object containing the next arg combo to try (matching "
        f"the same shape as the prior_attempts entries), with no surrounding "
        f"prose, code fences, or commentary. If you cannot suggest anything "
        f"useful, return {{\"args\": null, \"reason\": \"<why>\"}}."
    )

    prompt_lines = [
        f"Native method: {req.method}",
        f"Intent / op key: {req.op}",
    ]
    if req.signature:
        prompt_lines.append(f"Method signature: {req.signature}")
    if req.context:
        prompt_lines.append(
            f"Context: {json.dumps(req.context, default=str)[:1500]}")
    if req.prior_attempts:
        prompt_lines.append("Already tried (each failed):")
        for i, (args, msg) in enumerate(zip(
                req.prior_attempts,
                (req.failure_msgs + [""] * len(req.prior_attempts))[
                    :len(req.prior_attempts)]), start=1):
            prompt_lines.append(
                f"  {i}. args={json.dumps(args)} -> {msg or '(returned null)'}")
    prompt_lines.append(
        "Suggest the next single arg combo to try. JSON object only.")
    prompt = "\n".join(prompt_lines)

    raw = call_llm(prompt, system, repo_root=REPO_ROOT, quality="fast")
    if raw is None:
        return {"args": None, "reason": "no LLM backend available"}

    raw = raw.strip()
    # Strip ```json fences if a model added them despite instructions
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        last_fence = raw.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            raw = raw[first_nl + 1:last_fence].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "args": None,
            "reason": f"LLM reply was not JSON: {exc.msg}",
            "raw_preview": raw[:300],
        }
    if isinstance(parsed, dict) and "args" in parsed:
        return parsed
    # Plain arg-combo response — wrap it
    return {"args": parsed if isinstance(parsed, dict) else None,
            "reason": "" if isinstance(parsed, dict) else "non-object reply"}


# --------------------------------------------------------------------------- #
# /api/cad/text-to-part — end-to-end autonomous flow.
#
# Takes (goal, cad). Calls the LLM planner to turn goal into a list of
# native ops. Dispatches each op to the target CAD's HTTP listener
# (SW: 7501, Rhino: 7502). Returns per-op success + final screenshot
# path. The user's actual end-goal: type a prompt, get a working part
# inside the chosen CAD, no GUI clicks.
# --------------------------------------------------------------------------- #

class TextToPartRequest(BaseModel):
    goal: str
    cad: str = "solidworks"          # solidworks | rhino
    quality_tier: str = "balanced"


# Per-CAD HTTP listener URL. Each plugin's AriaHttpListener binds to
# its own well-known port. Imports from dashboard.cad_registry so
# adding a new bridge means one file (the registry) — no edits here.
try:
    from dashboard.cad_registry import get_cad_base_urls as _get_cad_base_urls
    _CAD_BASE_URL = _get_cad_base_urls()
except Exception:
    _CAD_BASE_URL = {
        "solidworks": "http://localhost:7501",
        "sw":         "http://localhost:7501",
        "rhino":      "http://localhost:7502",
        "autocad":    "http://localhost:7503",
    }


def _run_text_to_part_inproc(goal: str, cad: str = "solidworks",
                                quality_tier: str = "fast") -> dict:
    """Body of /api/cad/text-to-part — extracted so callers can run it
    in-process instead of via HTTP self-call.

    The HTTP recursion path deadlocks under the default single-worker
    uvicorn (the inner request waits for the outer worker that's blocked
    waiting for the inner). /api/system/full-build calls this directly.
    """
    import httpx as _httpx

    cad = (cad or "solidworks").lower().strip()
    base = _CAD_BASE_URL.get(cad)
    if not base:
        return {"ok": False, "error": f"unknown cad: {cad!r}"}

    # Confirm the listener is up — fail fast with a useful error rather
    # than dispatching ops into a void.
    try:
        with _httpx.Client(timeout=5.0) as c:
            status = c.get(f"{base}/status").json()
            if not status.get("ok"):
                return {"ok": False,
                          "error": f"{cad} listener not ready: {status}"}
            # Fresh document before each generation — beginPlan only clears
            # the in-memory registry, not the actual Rhino/SW doc objects.
            # Without /new_doc the scene piles up across runs.
            try: c.post(f"{base}/new_doc", json={}, timeout=15.0)
            except Exception: pass
    except Exception as exc:
        return {"ok": False,
                  "error": f"{cad} listener at {base} unreachable: "
                            f"{type(exc).__name__}: {exc}"}

    # Plan via the dispatcher: hardcoded planners (L-bracket, flange,
    # impeller, shaft, etc.) take priority — they emit deterministic,
    # validated ops. The LLM is only used for parts the catalogue
    # doesn't cover. Going straight to plan_from_llm here was bypassing
    # plan_simple_bracket and emitting circularPattern for rectangular
    # bolt grids, which doesn't render right.
    from aria_os.native_planner.dispatcher import make_plan
    from aria_os.spec_extractor import extract_spec
    spec = extract_spec(goal) or {}
    try:
        ops = make_plan(goal, spec, quality=quality_tier,
                         repo_root=REPO_ROOT, allow_llm=True)
    except Exception as exc:
        return {"ok": False,
                  "error": f"planner failed: {type(exc).__name__}: {exc}"}
    if not ops:
        return {"ok": False, "error": "planner returned no ops",
                 "cad": cad, "goal": goal}

    # Dispatch each op — keep going past individual failures.
    results = []
    first_failed_at = None
    with _httpx.Client(timeout=120.0) as c:
        for i, op in enumerate(ops):
            payload = {"kind": op.get("kind"), "params": op.get("params", {})}
            try:
                r = c.post(f"{base}/op", json=payload)
                r_json = r.json()
                ok = (r_json.get("result") or {}).get("ok",
                                                       r_json.get("ok"))
                results.append({
                    "i": i, "kind": payload["kind"],
                    "label": op.get("label"),
                    "ok": ok,
                    "result": r_json.get("result"),
                    "error": r_json.get("error"),
                })
                if not ok and first_failed_at is None:
                    first_failed_at = i
            except Exception as exc:
                results.append({
                    "i": i, "kind": payload["kind"], "ok": False,
                    "error": f"transport: {type(exc).__name__}: {exc}",
                })
                if first_failed_at is None:
                    first_failed_at = i

    n_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": first_failed_at is None,
        "cad": cad, "goal": goal,
        "n_ops_planned":    len(ops),
        "n_ops_dispatched": len(results),
        "n_ops_succeeded":  n_ok,
        "failed_at":        first_failed_at,
        "results":          results,
    }


@app.post("/api/cad/text-to-part")
def text_to_part(req: TextToPartRequest):
    """End-to-end: goal text → planner ops → target CAD via HTTP."""
    result = _run_text_to_part_inproc(req.goal, req.cad, req.quality_tier)
    if "error" in result and not result.get("ok") and "listener" in result.get("error", ""):
        # Listener-level errors deserve 502 status
        raise HTTPException(502, result["error"])
    return result


# --------------------------------------------------------------------------- #
# /api/ecad/text-to-board — end-to-end autonomous flow for ECAD.
#
# Mirrors /api/cad/text-to-part. Takes (goal). Uses the existing
# aria_os.ecad.ecad_generator pipeline as the "planner" (it already
# produces a high-quality BOM with real footprints + netlists from a
# natural-language goal). Translates each BOM component into a
# placeComponent op, dispatches to the KiCad listener on port 7505,
# adds GND zones, saves, and exports gerbers.
#
# Per the user's MCAD↔ECAD allowance: this endpoint can also accept
# an optional `mcad_constraints` dict (board outline / mounting holes
# from the matching enclosure) so a future MCAD pass can drive PCB
# perimeter from the SW/Rhino enclosure. No SW↔ECAD coupling beyond
# this — both remain otherwise self-contained.
# --------------------------------------------------------------------------- #

class TextToBoardRequest(BaseModel):
    goal: str
    quality_tier: str = "balanced"
    add_ground_zone: bool = True
    export_gerbers: bool = True
    mcad_constraints: dict | None = None


_ECAD_BASE_URL = {
    "kicad": "http://localhost:7505",
}


@app.post("/api/ecad/text-to-board")
def text_to_board(req: TextToBoardRequest):
    """End-to-end: goal text → BOM → KiCad listener → .kicad_pcb + Gerbers."""
    import httpx as _httpx

    base = _ECAD_BASE_URL["kicad"]

    # Confirm the listener is up — fail fast with a useful error.
    try:
        with _httpx.Client(timeout=5.0) as c:
            status = c.get(f"{base}/status").json()
            if not status.get("ok"):
                raise HTTPException(502,
                    f"kicad listener not ready: {status}")
    except Exception as exc:
        raise HTTPException(502,
            f"kicad listener at {base} unreachable: "
            f"{type(exc).__name__}: {exc}")

    # Use the existing ECAD pipeline as the planner. parse_components
    # + LLM enrichment + net assignment + place_components is a much
    # higher-quality "planner" than asking the LLM to emit raw ops.
    try:
        from aria_os.ecad.ecad_generator import (
            parse_components,
            _llm_enrich_components,
            _compute_mcu_peripheral_nets,
            _assign_component_nets,
            place_components,
            parse_board_dimensions,
            _slug,
        )
    except Exception as exc:
        raise HTTPException(500,
            f"ecad_generator import failed: {type(exc).__name__}: {exc}")

    board_name = _slug(req.goal)
    try:
        # MCAD constraints can override the parsed board dims (the
        # enclosure sets the hard outline — the PCB shrinks to fit).
        if req.mcad_constraints:
            board_w = float(req.mcad_constraints.get("board_w_mm",
                            req.mcad_constraints.get("width_mm", 0)))
            board_h = float(req.mcad_constraints.get("board_h_mm",
                            req.mcad_constraints.get("height_mm", 0)))
            if not (board_w and board_h):
                board_w, board_h = parse_board_dimensions(req.goal)
        else:
            board_w, board_h = parse_board_dimensions(req.goal)

        components = parse_components(req.goal)
        components = _llm_enrich_components(req.goal, components, REPO_ROOT)
        _compute_mcu_peripheral_nets(components, req.goal)
        _assign_component_nets(components, req.goal)
        place_components(components, board_w, board_h)
    except Exception as exc:
        raise HTTPException(500,
            f"ecad planner failed: {type(exc).__name__}: {exc}")
    if not components:
        return {"ok": False, "error": "no components parsed from goal",
                 "goal": req.goal}

    # Dispatch via the KiCad listener.
    results = []
    failed_at = None
    layers = 4 if any("4-layer" in (c.description or "").lower()
                       or "4 layer" in (c.description or "").lower()
                       for c in components) else 2
    n_layers = 4 if "4-layer" in req.goal.lower() or "4 layer" in req.goal.lower() else 2

    with _httpx.Client(timeout=120.0) as c:
        # 1. newBoard
        r = c.post(f"{base}/new_board", json={
            "name":        board_name,
            "board_w_mm":  board_w,
            "board_h_mm":  board_h,
            "n_layers":    n_layers,
        })
        results.append({"i": 0, "kind": "newBoard", "ok": r.json().get("ok"),
                         "result": r.json()})

        # 2. placeComponent for each parsed component
        for i, comp in enumerate(components, start=1):
            payload = {"kind": "placeComponent", "params": {
                "ref":          comp.ref,
                "value":        comp.value,
                "footprint":    comp.footprint,
                "x_mm":         comp.x_mm,
                "y_mm":         comp.y_mm,
                "width_mm":     comp.width_mm,
                "height_mm":    comp.height_mm,
                "rotation_deg": 0.0,
                "nets":         list(comp.nets) if comp.nets
                                else (list(set(comp.net_map.values()))
                                      if comp.net_map else []),
                "net_map":      dict(comp.net_map) if comp.net_map else {},
                "description":  comp.description,
            }}
            try:
                r = c.post(f"{base}/op", json=payload)
                rj = r.json()
                results.append({
                    "i": i, "kind": "placeComponent",
                    "ref": comp.ref, "value": comp.value,
                    "ok": (rj.get("result") or {}).get("ok", rj.get("ok")),
                    "result": rj.get("result"),
                })
                if not results[-1]["ok"]:
                    failed_at = i
                    break
            except Exception as exc:
                results.append({"i": i, "kind": "placeComponent",
                                 "ref": comp.ref, "ok": False,
                                 "error": f"transport: "
                                          f"{type(exc).__name__}: {exc}"})
                failed_at = i
                break

        # 3. addZone (GND copper pour) — usually wanted on B.Cu
        if req.add_ground_zone and failed_at is None:
            r = c.post(f"{base}/op", json={"kind": "addZone", "params": {
                "net_name": "GND", "layer": "B.Cu",
            }})
            rj = r.json()
            results.append({"i": len(results), "kind": "addZone",
                             "ok": (rj.get("result") or {}).get("ok",
                                                                  rj.get("ok")),
                             "result": rj.get("result")})

        # 4. save_pcb
        if failed_at is None:
            r = c.post(f"{base}/save_pcb", json={})
            save_result = r.json()
            results.append({"i": len(results), "kind": "save_pcb",
                             "ok": save_result.get("ok"),
                             "result": save_result})

        # 5. export_gerbers (optional)
        gerber_result = None
        if req.export_gerbers and failed_at is None:
            r = c.post(f"{base}/export_gerbers", json={})
            gerber_result = r.json()
            results.append({"i": len(results), "kind": "export_gerbers",
                             "ok": bool(gerber_result.get("available") and
                                         not gerber_result.get("error")),
                             "result": gerber_result})

    n_ok = sum(1 for r in results if r["ok"])
    return {
        "ok":               failed_at is None,
        "cad":              "kicad",
        "goal":             req.goal,
        "board_name":       board_name,
        "board_w_mm":       board_w,
        "board_h_mm":       board_h,
        "n_layers":         n_layers,
        "n_components":     len(components),
        "n_ops_dispatched": len(results),
        "n_ops_succeeded":  n_ok,
        "failed_at":        failed_at,
        "gerber_export":    gerber_result,
        "results":          results,
    }


# --------------------------------------------------------------------------- #
# /api/system/board-and-enclosure — MCAD↔ECAD round-trip.
#
# The user's only allowed cross-CAD coupling: MCAD and ECAD work together
# so a goal like "ESP32 dev board in a 90x70x25mm enclosure with USB-C
# cutout and 4 M3 mounting bosses" produces both a real .kicad_pcb AND a
# coordinated enclosure, with the cutout positioned where the actual
# USB-C connector ended up on the PCB.
#
# Flow:
#   1. ECAD pipeline (parse + place components) → list of Components.
#   2. Extract pcb_constraints: board bbox, connector cutouts (USB-C / headers
#      / barrel jacks), tallest component z, mount-hole positions.
#   3. Save .kicad_pcb via the KiCad listener.
#   4. Synthesize an MCAD goal that bakes those constraints in
#      (cavity dims, side-wall cutouts, mount-boss positions).
#   5. Dispatch to the chosen MCAD listener (default rhino — running);
#      sw is supported when the SW addin is connected.
#   6. Return both results + the constraints that bridged them.
# --------------------------------------------------------------------------- #

# Heuristic component-height lookup table. We never ask KiCad for actual
# 3D model heights — that would require parsing every WRL/STEP from the
# library. Instead, classify by footprint substring and use a known good
# enclosure-design height. Always rounds UP because the cost of an
# enclosure that's 1mm too tall is zero; 1mm too short is fatal.
_COMPONENT_HEIGHT_MM_LOOKUP: list[tuple[tuple[str, ...], float]] = [
    # Through-hole + tall first (substring "wins" by ordering)
    (("BarrelJack", "DC_Jack"),                              11.0),
    (("RJ45",),                                              16.0),
    (("PinHeader_2x", "PinHeader_1x"),                        9.0),
    (("Switch_TH", "Tactile_TH"),                            10.0),
    (("Crystal_HC", "Quartz_HC"),                             5.0),
    (("USB_C_Receptacle", "USB_C_Plug",
      "USB_C_Receptacle_GCT", "USB-C"),                       3.5),
    (("USB_B_", "USB_Mini",),                                 8.0),
    (("ESP32", "ESP32-S3", "ESP32-C3",
      "WROOM", "WROVER"),                                     3.5),
    (("STM32", "QFN", "QFP", "SOP", "TSSOP"),                 1.5),
    (("MicroSD",),                                            2.0),
    (("Module_BNO055", "IMU"),                                2.0),
    (("LED_THT",),                                            8.0),
    # Surface-mount discretes
    (("R_0402", "C_0402", "L_0402"),                          0.5),
    (("R_0603", "C_0603", "L_0603"),                          0.55),
    (("R_0805", "C_0805", "L_0805"),                          0.6),
    (("R_1206", "C_1206", "L_1206"),                          0.7),
    (("LED_0402", "LED_0603", "LED_0805"),                    0.6),
    (("SOT-23", "SOT-223", "SOT-89"),                         1.5),
    (("DPAK", "TO-252"),                                      2.4),
    (("D2PAK", "TO-263"),                                     4.4),
    # Generic fallback
    (("Generic",),                                            2.0),
]

_DEFAULT_COMPONENT_HEIGHT_MM = 2.0


def _component_height_mm(comp) -> float:
    """Best-effort z-height for a placed component. See lookup table."""
    fp = (comp.footprint or "").lower()
    val = (comp.value or "").lower()
    haystack = f"{fp} {val}"
    for substrings, h in _COMPONENT_HEIGHT_MM_LOOKUP:
        if any(s.lower() in haystack for s in substrings):
            return h
    return _DEFAULT_COMPONENT_HEIGHT_MM


def _classify_connector(comp) -> str | None:
    """Return a connector category if `comp` is a connector, else None."""
    fp = (comp.footprint or "").lower()
    val = (comp.value or "").lower()
    ref = (comp.ref or "").upper()
    if "usb_c" in fp or "usb-c" in fp or "type-c" in fp \
            or "usb_c" in val or "usb-c" in val:
        return "usb_c"
    if "usb_micro" in fp or "usb_mini" in fp or "usb_b" in fp:
        return "usb_other"
    if "barreljack" in fp or "dc_jack" in fp:
        return "barrel_jack"
    if "rj45" in fp:
        return "rj45"
    if "pinheader" in fp or "screwterm" in fp:
        return "header"
    if ref.startswith("J") and ("usb" in val or "conn" in val
                                  or "header" in val or "jack" in val):
        return "header"
    return None


def _nearest_edge(x: float, y: float, w: float, h: float,
                   board_w: float, board_h: float) -> str:
    """Which board edge is the (x,y,w,h) bbox nearest to? top|bottom|left|right."""
    cx = x + w / 2
    cy = y + h / 2
    dist_left = cx
    dist_right = board_w - cx
    dist_bottom = cy
    dist_top = board_h - cy
    by_dist = sorted([("left", dist_left), ("right", dist_right),
                       ("bottom", dist_bottom), ("top", dist_top)],
                      key=lambda kv: kv[1])
    return by_dist[0][0]


def _extract_pcb_constraints(components, board_w: float,
                              board_h: float) -> dict:
    """Walk placed components → MCAD-relevant geometry constraints.

    Returns a dict the MCAD planner can ingest verbatim into an
    enclosure goal string.
    """
    tallest = 0.0
    connectors = []
    headers = []
    mount_holes = []
    for c in components:
        h = _component_height_mm(c)
        if h > tallest:
            tallest = h
        cat = _classify_connector(c)
        cx, cy = c.x_mm + c.width_mm / 2, c.y_mm + c.height_mm / 2
        if cat in ("usb_c", "usb_other", "barrel_jack", "rj45"):
            edge = _nearest_edge(c.x_mm, c.y_mm, c.width_mm, c.height_mm,
                                  board_w, board_h)
            connectors.append({
                "ref":      c.ref,
                "type":     cat,
                "value":    c.value,
                "x_mm":     round(c.x_mm, 2),
                "y_mm":     round(c.y_mm, 2),
                "w_mm":     round(c.width_mm, 2),
                "h_mm":     round(c.height_mm, 2),
                "z_mm":     h,
                "edge":     edge,
            })
        elif cat == "header":
            headers.append({
                "ref":      c.ref,
                "value":    c.value,
                "x_mm":     round(cx, 2),
                "y_mm":     round(cy, 2),
                "z_mm":     h,
            })
        if (c.ref or "").upper().startswith("H") \
                or "mountinghole" in (c.footprint or "").lower():
            mount_holes.append({
                "x_mm":          round(cx, 2),
                "y_mm":          round(cy, 2),
                "diameter_mm":   3.2,  # M3 clearance default
            })
    # If no explicit mount holes, place 4 at the corners with 3mm inset.
    if not mount_holes:
        inset = 3.5
        mount_holes = [
            {"x_mm": inset,                "y_mm": inset,
             "diameter_mm": 3.2, "auto_corner": True},
            {"x_mm": board_w - inset,      "y_mm": inset,
             "diameter_mm": 3.2, "auto_corner": True},
            {"x_mm": board_w - inset,      "y_mm": board_h - inset,
             "diameter_mm": 3.2, "auto_corner": True},
            {"x_mm": inset,                "y_mm": board_h - inset,
             "diameter_mm": 3.2, "auto_corner": True},
        ]
    return {
        "board_w_mm":               round(board_w, 2),
        "board_h_mm":               round(board_h, 2),
        "tallest_component_z_mm":   round(tallest, 2),
        "connectors":               connectors,
        "headers":                  headers,
        "mount_holes":              mount_holes,
    }


def _enclosure_goal_from_constraints(c: dict, *,
                                       wall_mm: float = 3.0,
                                       cavity_clearance_mm: float = 1.0
                                       ) -> str:
    """Build a deterministic MCAD goal string from PCB constraints.

    The MCAD planner reads natural language. We compose a precise
    description so the planner emits the right ops (no LLM creativity
    on dimensions — those are fixed by the PCB).
    """
    bw = c["board_w_mm"]
    bh = c["board_h_mm"]
    bz = max(c["tallest_component_z_mm"] + cavity_clearance_mm, 8.0)
    outer_w = bw + 2 * wall_mm + 2 * cavity_clearance_mm
    outer_h = bh + 2 * wall_mm + 2 * cavity_clearance_mm
    outer_z = bz + 2 * wall_mm
    parts = [
        f"Rectangular enclosure {outer_w:.1f}x{outer_h:.1f}x{outer_z:.1f}mm "
        f"with {wall_mm}mm walls. ",
        f"Internal cavity {bw + 2 * cavity_clearance_mm:.1f}x"
        f"{bh + 2 * cavity_clearance_mm:.1f}x{bz:.1f}mm to fit a PCB. ",
    ]
    for conn in c.get("connectors", []):
        # Cutout dim defaults: USB-C ~9x4mm, barrel jack 11x11mm,
        # USB-other ~12x7mm. RJ45 ~16x14mm.
        cw, ch = {
            "usb_c":       (9.0, 4.0),
            "usb_other":   (12.0, 7.0),
            "barrel_jack": (11.0, 11.0),
            "rj45":        (16.0, 14.0),
        }.get(conn["type"], (10.0, 5.0))
        parts.append(
            f"Cutout {cw:.1f}x{ch:.1f}mm on the {conn['edge']} wall "
            f"for the {conn['type']} connector at ({conn['x_mm']},"
            f"{conn['y_mm']}) on the PCB. ")
    if c.get("mount_holes"):
        n = len(c["mount_holes"])
        parts.append(
            f"Add {n} M3 mounting bosses on the cavity floor at the PCB "
            f"corner positions, 5mm tall, with M3 clearance holes. ")
    return "".join(parts).strip()


def _build_enclosure_ops_deterministic(c: dict, *,
                                         wall_mm: float = 3.0,
                                         cavity_clearance_mm: float = 1.0
                                         ) -> list[dict]:
    """Build a guaranteed-working ops list straight from the constraints.

    Used when the LLM planner fails to parse / returns nothing. We KNOW
    the board dims, cutouts, and mount holes — so synthesising the ops
    deterministically is both faster and more reliable than asking an
    LLM to re-derive the geometry.

    Output ops use the canonical Rhino/SW kinds: beginPlan, newSketch,
    sketchRect, sketchCircle, extrude. shell op is OPTIONAL — if the
    target bridge doesn't have it, the cavity is created via a second
    smaller box subtracted from the outer box (boolean cut).
    """
    bw = c["board_w_mm"]
    bh = c["board_h_mm"]
    bz = max(c["tallest_component_z_mm"] + cavity_clearance_mm, 8.0)
    outer_w = bw + 2 * wall_mm + 2 * cavity_clearance_mm
    outer_h = bh + 2 * wall_mm + 2 * cavity_clearance_mm
    outer_z = bz + 2 * wall_mm
    cavity_w = bw + 2 * cavity_clearance_mm
    cavity_h = bh + 2 * cavity_clearance_mm
    cavity_z = bz

    # All sketch primitives use the canonical schema in
    # aria_os/native_planner/llm_planner.py: cx/cy/w/h for rects,
    # cx/cy/r for circles. Origin is the sketch plane center.
    ops: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Begin enclosure plan"},
        # Outer box
        {"kind": "newSketch", "params": {
            "plane": "XY", "alias": "sk_outer",
            "name": "Outer box base",
        }, "label": "Outer base sketch"},
        {"kind": "sketchRect", "params": {
            "sketch": "sk_outer", "cx": 0, "cy": 0,
            "w": outer_w, "h": outer_h,
        }, "label": f"{outer_w:.1f}x{outer_h:.1f} outer rect"},
        {"kind": "extrude", "params": {
            "sketch": "sk_outer", "distance": outer_z,
            "operation": "new", "alias": "outer_box",
        }, "label": f"Extrude outer box {outer_z:.1f}mm"},
        # Inner cavity (subtract). Offset the sketch plane up by wall_mm
        # so the cavity floor sits at +Z = wall_mm; extrude `cavity_z`
        # upward leaves +Z = wall_mm + cavity_z = outer_z - wall_mm
        # (i.e. the lid wall stays intact).
        {"kind": "newSketch", "params": {
            "plane": "XY", "alias": "sk_cavity",
            "name": "Cavity base",
            "offset": wall_mm,
        }, "label": "Cavity base sketch"},
        {"kind": "sketchRect", "params": {
            "sketch": "sk_cavity", "cx": 0, "cy": 0,
            "w": cavity_w, "h": cavity_h,
        }, "label": f"{cavity_w:.1f}x{cavity_h:.1f} cavity rect"},
        {"kind": "extrude", "params": {
            "sketch": "sk_cavity", "distance": cavity_z,
            "operation": "cut", "target": "outer_box",
            "alias": "cavity_cut",
        }, "label": f"Cut cavity {cavity_z:.1f}mm deep"},
    ]

    # Connector cutouts on side walls. Sketch planes (YZ for left/right,
    # XZ for top/bottom) are centered on the world origin; sketch
    # coords (cx,cy) are the in-plane offsets where the cutout center
    # should land. We project the connector's PCB position onto the
    # appropriate side wall.
    pcb_origin_x = -bw / 2 - cavity_clearance_mm  # cavity is centered on outer
    pcb_origin_y = -bh / 2 - cavity_clearance_mm
    pcb_z_top = wall_mm + (cavity_clearance_mm)  # PCB top surface above floor
    for i, conn in enumerate(c.get("connectors", [])):
        ctype = conn["type"]
        cw, ch = {
            "usb_c":       (9.0, 4.0),
            "usb_other":   (12.0, 7.0),
            "barrel_jack": (11.0, 11.0),
            "rj45":        (16.0, 14.0),
        }.get(ctype, (10.0, 5.0))
        edge = conn["edge"]
        # Connector center in world coords (PCB origin offset)
        pcb_cx = pcb_origin_x + conn["x_mm"] + conn["w_mm"] / 2
        pcb_cy = pcb_origin_y + conn["y_mm"] + conn["h_mm"] / 2
        # Cutout vertical center: at PCB top + connector z/2
        z_center = pcb_z_top + (conn.get("z_mm", 3.5) / 2) - outer_z / 2
        sk_alias = f"sk_cut_{i}"
        if edge in ("left", "right"):
            # Sketch is on YZ plane: in-plane cx = world Y, cy = world Z (centered)
            sk_cx = pcb_cy
            sk_cy = z_center
            sketch_plane = "YZ"
        else:  # top / bottom edge → XZ plane: in-plane cx = world X, cy = world Z
            sk_cx = pcb_cx
            sk_cy = z_center
            sketch_plane = "XZ"
        ops.append({"kind": "newSketch", "params": {
            "plane": sketch_plane, "alias": sk_alias,
            "name": f"Cutout for {conn['ref']}",
        }, "label": f"Side cutout sketch {i}"})
        ops.append({"kind": "sketchRect", "params": {
            "sketch": sk_alias, "cx": sk_cx, "cy": sk_cy,
            "w": cw, "h": ch,
        }, "label": f"{ctype} {cw:.1f}x{ch:.1f}"})
        ops.append({"kind": "extrude", "params": {
            "sketch": sk_alias, "distance": outer_w + outer_h,
            "operation": "cut", "target": "outer_box",
            "alias": f"cut_{conn['ref']}",
        }, "label": f"Cut {conn['ref']} through wall"})

    # Mount-hole bosses on cavity floor. Outer cylinder (boss), inner
    # circle (clearance hole). Both circles use canonical (cx,cy,r)
    # schema; cylinder centered on PCB-origin-offset coords.
    for i, mh in enumerate(c.get("mount_holes", [])):
        sk_cx = pcb_origin_x + mh["x_mm"]
        sk_cy = pcb_origin_y + mh["y_mm"]
        boss_r = max(mh["diameter_mm"] / 2 + 2.0, 3.0)
        hole_r = mh["diameter_mm"] / 2
        boss_h = 5.0
        sk_boss = f"sk_boss_{i}"
        ops.append({"kind": "newSketch", "params": {
            "plane": "XY", "alias": sk_boss,
            "name": f"Mount boss {i}", "offset": wall_mm,
        }, "label": f"Mount boss {i} sketch"})
        ops.append({"kind": "sketchCircle", "params": {
            "sketch": sk_boss, "cx": sk_cx, "cy": sk_cy, "r": boss_r,
        }, "label": f"Boss Ø{boss_r * 2:.1f}"})
        ops.append({"kind": "extrude", "params": {
            "sketch": sk_boss, "distance": boss_h,
            "operation": "join", "target": "outer_box",
            "alias": f"boss_{i}",
        }, "label": f"Extrude boss {i}"})
        # Clearance hole through the boss
        sk_hole = f"sk_hole_{i}"
        ops.append({"kind": "newSketch", "params": {
            "plane": "XY", "alias": sk_hole,
            "offset": wall_mm,
        }, "label": f"Clearance hole {i} sketch"})
        ops.append({"kind": "sketchCircle", "params": {
            "sketch": sk_hole, "cx": sk_cx, "cy": sk_cy, "r": hole_r,
        }, "label": f"Hole Ø{hole_r * 2:.1f}"})
        ops.append({"kind": "extrude", "params": {
            "sketch": sk_hole, "distance": boss_h + 1,
            "operation": "cut", "target": "outer_box",
            "alias": f"hole_{i}",
        }, "label": f"Cut clearance {i}"})

    return ops


class BoardAndEnclosureRequest(BaseModel):
    goal: str
    mcad_cad: str = "rhino"          # rhino | solidworks
    quality_tier: str = "balanced"
    wall_mm: float = 3.0
    cavity_clearance_mm: float = 1.0
    deterministic_fallback: bool = True   # use _build_enclosure_ops_deterministic if planner fails


@app.post("/api/system/board-and-enclosure")
def board_and_enclosure(req: BoardAndEnclosureRequest):
    """End-to-end MCAD↔ECAD round-trip for an "X board in Y enclosure" goal."""
    import httpx as _httpx

    mcad = req.mcad_cad.lower().strip()
    if mcad not in _CAD_BASE_URL:
        raise HTTPException(400, f"unknown mcad cad: {mcad}")
    mcad_base = _CAD_BASE_URL[mcad]
    ecad_base = _ECAD_BASE_URL["kicad"]

    # Confirm both listeners are up + reset both doc states.
    try:
        with _httpx.Client(timeout=5.0) as c:
            ms = c.get(f"{mcad_base}/status").json()
            es = c.get(f"{ecad_base}/status").json()
            if not ms.get("ok"):
                raise HTTPException(502, f"mcad ({mcad}) listener: {ms}")
            if not es.get("ok"):
                raise HTTPException(502, f"ecad listener: {es}")
            # Fresh state for both halves — old objects must NOT leak across
            # runs. ECAD uses /quit (drops state to defaults). MCAD uses
            # /new_doc (opens a fresh Rhino/SW document).
            try: c.post(f"{ecad_base}/quit", json={}, timeout=10.0)
            except Exception: pass
            try: c.post(f"{mcad_base}/new_doc", json={}, timeout=15.0)
            except Exception: pass
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502,
            f"listener check failed: {type(exc).__name__}: {exc}")

    # Phase 1: ECAD planner.
    try:
        from aria_os.ecad.ecad_generator import (
            parse_components, _llm_enrich_components,
            _compute_mcu_peripheral_nets, _assign_component_nets,
            place_components, parse_board_dimensions, _slug,
        )
    except Exception as exc:
        raise HTTPException(500,
            f"ecad import failed: {type(exc).__name__}: {exc}")

    board_name = _slug(req.goal)
    board_w, board_h = parse_board_dimensions(req.goal)
    components = parse_components(req.goal)
    components = _llm_enrich_components(req.goal, components, REPO_ROOT)
    _compute_mcu_peripheral_nets(components, req.goal)
    _assign_component_nets(components, req.goal)
    place_components(components, board_w, board_h)
    if not components:
        raise HTTPException(400,
            "no PCB components inferred from goal — clarify the board content")

    # Phase 2: extract constraints.
    constraints = _extract_pcb_constraints(components, board_w, board_h)

    # Phase 3: dispatch ECAD ops to the listener.
    pcb_results = []
    with _httpx.Client(timeout=120.0) as c:
        c.post(f"{ecad_base}/new_board", json={
            "name":        board_name,
            "board_w_mm":  board_w,
            "board_h_mm":  board_h,
            "n_layers":    4 if "4-layer" in req.goal.lower()
                                or "4 layer" in req.goal.lower() else 2,
        })
        for comp in components:
            r = c.post(f"{ecad_base}/op", json={
                "kind": "placeComponent",
                "params": {
                    "ref":          comp.ref,
                    "value":        comp.value,
                    "footprint":    comp.footprint,
                    "x_mm":         comp.x_mm, "y_mm": comp.y_mm,
                    "width_mm":     comp.width_mm,
                    "height_mm":    comp.height_mm,
                    "rotation_deg": 0.0,
                    "nets":         list(comp.nets) if comp.nets
                                    else (list(set(comp.net_map.values()))
                                          if comp.net_map else []),
                    "net_map":      dict(comp.net_map) if comp.net_map else {},
                    "description":  comp.description,
                }})
            rj = r.json()
            pcb_results.append({"ref": comp.ref, "value": comp.value,
                                 "ok": (rj.get("result") or {}).get("ok",
                                                                       rj.get("ok"))})
        c.post(f"{ecad_base}/op", json={"kind": "addZone", "params": {
            "net_name": "GND", "layer": "B.Cu",
        }})
        save_resp = c.post(f"{ecad_base}/save_pcb", json={}).json()
        gerber_resp = c.post(f"{ecad_base}/export_gerbers", json={}).json()

    # Phase 4: synthesize MCAD enclosure goal from constraints.
    enclosure_goal = _enclosure_goal_from_constraints(
        constraints, wall_mm=req.wall_mm,
        cavity_clearance_mm=req.cavity_clearance_mm)

    # Phase 5: dispatch MCAD goal to the chosen MCAD listener.
    # Try the LLM planner first — it has access to the full schema and
    # can handle features the deterministic builder doesn't (rounded
    # corners, draft, lid, etc). Fall back to the deterministic builder
    # if the LLM fails or returns nothing — guarantees the round-trip
    # always produces a working enclosure regardless of LLM state.
    mcad_ops: list[dict] = []
    planner_error: str | None = None
    try:
        from aria_os.native_planner.llm_planner import plan_from_llm
        mcad_ops = plan_from_llm(enclosure_goal, {},
                                  quality=req.quality_tier,
                                  repo_root=REPO_ROOT) or []
    except Exception as exc:
        planner_error = f"{type(exc).__name__}: {exc}"

    used_fallback = False
    if not mcad_ops:
        if not req.deterministic_fallback:
            return {"ok": False, "phase": "mcad_planner",
                     "error": planner_error or "planner returned no ops",
                     "pcb": save_resp, "constraints": constraints,
                     "enclosure_goal": enclosure_goal}
        mcad_ops = _build_enclosure_ops_deterministic(
            constraints,
            wall_mm=req.wall_mm,
            cavity_clearance_mm=req.cavity_clearance_mm,
        )
        used_fallback = True

    mcad_results = []
    failed_at = None
    with _httpx.Client(timeout=120.0) as c:
        for i, op in enumerate(mcad_ops):
            try:
                r = c.post(f"{mcad_base}/op", json={
                    "kind": op.get("kind"), "params": op.get("params", {})})
                rj = r.json()
                ok = (rj.get("result") or {}).get("ok", rj.get("ok"))
                mcad_results.append({"i": i, "kind": op.get("kind"),
                                      "label": op.get("label"), "ok": ok,
                                      "result": rj.get("result")})
                if not ok:
                    failed_at = i
                    break
            except Exception as exc:
                mcad_results.append({"i": i, "kind": op.get("kind"),
                                      "ok": False,
                                      "error": f"transport: "
                                               f"{type(exc).__name__}: {exc}"})
                failed_at = i
                break

    return {
        "ok":               failed_at is None,
        "goal":             req.goal,
        "board_name":       board_name,
        "constraints":      constraints,
        "enclosure_goal":   enclosure_goal,
        "pcb": {
            "save":           save_resp,
            "gerber_export":  gerber_resp,
            "n_components":   len(components),
            "n_ops_ok":       sum(1 for r in pcb_results if r["ok"]),
        },
        "enclosure": {
            "cad":              mcad,
            "n_ops_planned":    len(mcad_ops),
            "n_ops_succeeded":  sum(1 for r in mcad_results if r["ok"]),
            "failed_at":        failed_at,
            "used_fallback":    used_fallback,
            "planner_error":    planner_error,
            # All ops verbatim (post-normalization) — useful when debugging
            # "result: null" at the bridge: shows whether the LLM emitted
            # canonical params after planner-side aliasing.
            "ops_preview":      mcad_ops,
            "results":          mcad_results,
        },
    }


# --------------------------------------------------------------------------- #
# /api/system/full-build — END-TO-END drone (or any electromechanical product)
# build pipeline. Goes from a text goal all the way to:
#   1. KiCad PCB (Gerbers + STEP)
#   2. SolidWorks frame (STEP + screenshot)
#   3. Combined assembly STEP (mated)
#   4. eBOM (electrical, from KiCad components)
#   5. MBOM (mechanical, from frame parts + catalog: motors, props, ESCs, …)
#   6. Engineering drawings (PDF for PCB fab, optional GD&T for frame)
#   7. Assembly manual (markdown — uses existing drone presets)
#
# This is the "fully integrate the two CADs" pipeline — MCAD↔ECAD coupling
# extends here from the geometric handshake (board outline → enclosure
# cavity) into manufacturing handoff (assembly + BOMs + docs).
# --------------------------------------------------------------------------- #

def _drone_catalog_match(*, motor_count: int, prop_size_in: float,
                           battery_cells: int, battery_capacity_mah: int
                           ) -> list[dict]:
    """Pick canonical aria_os.components designations from rough specs.
    Closest-match heuristic — the catalog isn't fully covered, so we
    pick the nearest available size/spec.
    """
    # Motor pick: 2207 family for 5", 2812 for 7", 3510 for 10", 5010 for 13".
    if prop_size_in <= 4.5:    motor = "2207-1750KV"
    elif prop_size_in <= 6:    motor = "2207-1750KV"   # standard 5" racing
    elif prop_size_in <= 8:    motor = "2812-1450KV"
    elif prop_size_in <= 11:   motor = "3510-700KV"
    elif prop_size_in <= 14:   motor = "5010-340KV"
    else:                      motor = "6215-250KV"
    # Propeller pick: closest 3-blade by size
    prop_table = {
        3: "3x3_2blade", 5: "5x4.3_3blade",
        7: "7x4.3_3blade", 10: "10x5.5_3blade",
        13: "13x5.5_3blade", 15: "15x5_2blade",
        18: "18x6.1_2blade", 24: "24x7.2_2blade",
    }
    nearest = min(prop_table.keys(), key=lambda d: abs(d - prop_size_in))
    prop = prop_table[nearest]
    # ESC pick: 30A 4in1 for ≤6", 45A for 7", 60A for ≥10"
    if prop_size_in <= 6:      esc = "ESC_30A_BLHeli32_4in1"
    elif prop_size_in <= 8:    esc = "ESC_45A_BLHeli32_4in1"
    else:                      esc = "ESC_60A_AM32_4in1"
    # Battery pick: cells × closest capacity
    cap = battery_capacity_mah
    if battery_cells <= 3:    bat = "LiPo_3S_1300mAh_75C"
    elif battery_cells == 4:
        bat = (f"LiPo_4S_{1500 if abs(cap - 1500) < abs(cap - 1300)
                              else (1300 if cap < 1500 else 1800)}"
                f"mAh_{120 if cap < 1700 else 100}C"
                if cap < 2500 else "LiPo_4S_3300mAh_60C")
    elif battery_cells == 6:
        bat = (f"LiPo_6S_{1500 if cap >= 1300 else 1100}"
                f"mAh_120C"
                if cap < 4000 else "LiPo_6S_5000mAh_50C")
    else:
        bat = "LiPo_4S_1500mAh_120C"
    return [
        {"designation": motor, "quantity": motor_count},
        {"designation": prop,  "quantity": motor_count},
        {"designation": esc,
         "quantity":    1 if "4in1" in esc else motor_count},
        {"designation": bat,   "quantity": 1},
        {"designation": "M3x8_12.9",
         "quantity":    motor_count * 4},  # motor mount screws (M3x6 not in catalog; M3x8 is closest)
        {"designation": "M3x10_12.9",
         "quantity":    4},  # FC stack screws — engage 10mm standoffs
        {"designation": "M3x10_brass_standoff",
         "quantity":    4},  # FC standoffs
        {"designation": "Velcro_strap_200x20mm",
         "quantity":    1},
        {"designation": "XT60_connector",
         "quantity":    1},
        {"designation": "GPS_M8N_module",
         "quantity":    1},
        {"designation": "Telemetry_LoRa_433",
         "quantity":    1},
        {"designation": "RC_receiver_ELRS_2.4G",
         "quantity":    1},
        {"designation": "VTX_5.8GHz_400mW",
         "quantity":    1},
        {"designation": "FPV_camera_micro",
         "quantity":    1},
    ]


class FullBuildRequest(BaseModel):
    goal: str
    pcb_goal: str | None = None       # override goal for ECAD half
    frame_goal: str | None = None     # override goal for MCAD half
    mcad_cad: str = "solidworks"
    quality_tier: str = "fast"
    bundle_name: str = "drone_build"
    motor_count: int = 4
    prop_size_in: float = 5.0
    battery_capacity_mah: int = 1500
    battery_cells: int = 4


@app.post("/api/system/full-build")
def full_build(req: FullBuildRequest):
    """End-to-end drone build: PCB + frame + assembly + BOM + docs."""
    import httpx as _httpx
    import shutil

    bundle = REPO_ROOT / "outputs" / "system_builds" / req.bundle_name
    bundle.mkdir(parents=True, exist_ok=True)

    pcb_goal = req.pcb_goal or req.goal
    frame_goal = req.frame_goal or req.goal
    mcad_base = _CAD_BASE_URL.get(req.mcad_cad.lower())
    ecad_base = _ECAD_BASE_URL["kicad"]
    if not mcad_base:
        raise HTTPException(400, f"unknown mcad_cad: {req.mcad_cad}")

    log: list[dict] = []
    def step(name, ok, **extra):
        rec = {"step": name, "ok": ok, **extra}
        log.append(rec)
        return rec

    # --- 1. ECAD: generate PCB --------------------------------------------
    with _httpx.Client(timeout=180.0) as c:
        try:
            r = c.post(f"http://localhost:8001/api/ecad/text-to-board",
                        json={"goal": pcb_goal})
            ecad = r.json()
            step("ecad_generate", ecad.get("ok"),
                  components=ecad.get("n_components"),
                  ops_ok=ecad.get("n_ops_succeeded"))
        except Exception as exc:
            step("ecad_generate", False, error=str(exc))
            return {"ok": False, "log": log, "bundle": str(bundle)}

    pcb_kicad_path = None
    for r in (ecad.get("results") or []):
        if r.get("kind") == "save_pcb":
            pcb_kicad_path = (r.get("result") or {}).get("path")
            break
    if not pcb_kicad_path:
        return {"ok": False, "log": log, "error": "no PCB save path"}

    # Export PCB as STEP
    cli = shutil.which("kicad-cli") or \
        r"C:\Users\jonko\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe"
    pcb_step = bundle / "fc_pcb.step"
    try:
        proc = __import__("subprocess").run(
            [cli, "pcb", "export", "step",
             "--output", str(pcb_step), pcb_kicad_path],
            capture_output=True, timeout=60)
        step("ecad_step_export", proc.returncode == 0,
              size=pcb_step.stat().st_size if pcb_step.exists() else 0)
    except Exception as exc:
        step("ecad_step_export", False, error=str(exc))

    # --- 2. MCAD: generate frame ------------------------------------------
    # In-process call (NOT HTTP recursion) — single-worker uvicorn would
    # deadlock on a self-call: the inner request waits for the worker
    # that's blocked waiting on the outer. Calling the helper directly
    # bypasses the HTTP layer and keeps the same goal text → planner →
    # SW dispatch flow.
    try:
        mcad = _run_text_to_part_inproc(
            frame_goal, req.mcad_cad, req.quality_tier)
        step("mcad_generate", mcad.get("ok"),
              ops_ok=mcad.get("n_ops_succeeded"),
              ops_total=mcad.get("n_ops_planned"),
              error=mcad.get("error") if not mcad.get("ok") else None)
    except Exception as exc:
        mcad = {}
        step("mcad_generate", False, error=str(exc))

    frame_step = bundle / "drone_frame.step"
    try:
        with _httpx.Client(timeout=60.0) as c:
            r = c.post(f"{mcad_base}/save_step",
                        json={"path": str(frame_step).replace("\\", "/")})
            step("mcad_step_export", r.json().get("ok"),
                  size=frame_step.stat().st_size if frame_step.exists() else 0)
    except Exception as exc:
        step("mcad_step_export", False, error=str(exc))

    # Self-healing: fall back to the canonical drone frame STEP if any
    # of these go wrong:
    #   1. SW didn't produce a frame (mcad_generate failed → no fresh
    #      part → mcad_step_export saved an empty/wrong doc).
    #   2. mcad_step_export saved a HUGE file (3+MB), which means it
    #      grabbed the previously-active assembly instead of a fresh
    #      frame part — wrong content even though it passes the size
    #      check.
    # The canonical frame is ~236 KB; anything outside [10KB, 1MB] is
    # almost certainly the wrong content.
    canonical = REPO_ROOT / "outputs" / "cad" / "step" / "drone_frame.step"
    bad = (not frame_step.is_file()
            or frame_step.stat().st_size < 10_000
            or frame_step.stat().st_size > 1_500_000
            or not mcad.get("ok"))
    if bad:
        if canonical.is_file() and canonical.stat().st_size > 10_000:
            shutil.copy(canonical, frame_step)
            step("mcad_step_fallback", True,
                  source=str(canonical),
                  size=frame_step.stat().st_size,
                  reason=("mcad_generate failed" if not mcad.get("ok")
                            else f"frame_step size out of range"))
        else:
            step("mcad_step_fallback", False,
                  reason="no canonical frame STEP available")

    # --- 3. Build a hierarchical assembly config --------------------------
    # Catalog parts — list_components_used() in hierarchical_assembly.py
    # counts INSTANCES, not quantity fields. So for 16 motor screws we
    # emit 16 separate parts entries each with `component: <designation>`.
    catalog_parts: list[dict] = []
    instance_idx = 0
    for cat_entry in _drone_catalog_match(
            motor_count=req.motor_count,
            prop_size_in=req.prop_size_in,
            battery_cells=req.battery_cells,
            battery_capacity_mah=req.battery_capacity_mah):
        for _ in range(int(cat_entry.get("quantity", 1))):
            instance_idx += 1
            catalog_parts.append({
                "id":         f"{cat_entry['designation']}__{instance_idx}",
                "component":  cat_entry["designation"],
                "pos":        [0, 0, 0],
                "rot":        [0, 0, 0],
            })

    config = {
        "name":    req.bundle_name,
        "preset":  "fpv_drone",
        "parts": [
            {"id": "frame",  "step": str(frame_step),
             "pos": [0, 0, 0], "rot": [0, 0, 0],
             "fabricated": True},
            {"id": "fc_pcb", "step": str(pcb_step),
             "pos": [0, 0, 8], "rot": [0, 0, 0],
             "ecad": pcb_kicad_path},
            *catalog_parts,
        ],
    }
    cfg_path = bundle / "build_config.json"
    cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # --- 4. Generate combined BOM (mBOM + eBOM) ---------------------------
    bom: dict = {}
    try:
        from aria_os.assembly_bom import generate_bom
        bom = generate_bom(config, config_path=cfg_path)
        bom_path = bundle / "bom.json"
        bom_path.write_text(json.dumps(bom, indent=2, default=str),
                              encoding="utf-8")
        step("bom_generate", True,
              total_parts=bom.get("summary", {}).get("total_parts"),
              total_cost_usd=bom.get("summary", {}).get(
                  "total_purchased_cost_usd"))
    except Exception as exc:
        step("bom_generate", False,
              error=f"{type(exc).__name__}: {exc}")

    # --- 5. eBOM via KiCad (component-list export) -----------------------
    try:
        proc = __import__("subprocess").run(
            [cli, "sch", "export", "bom",
             "--output", str(bundle / "ebom.csv"),
             pcb_kicad_path.replace(".kicad_pcb", ".kicad_sch")],
            capture_output=True, timeout=30)
        # Schematic may not exist (pcb_writer doesn't produce one) — try
        # alternative: list components from the .kicad_pcb directly.
        step("ebom_kicad", proc.returncode == 0,
              path=str(bundle / "ebom.csv") if (bundle / "ebom.csv").is_file()
                   else "(no schematic)")
    except Exception as exc:
        step("ebom_kicad", False, error=str(exc))

    # Fallback: extract eBOM from the .kicad_pcb footprint refs.
    # KiCad 9/10 uses `(property "Reference" "X")` (not the old
    # `fp_text reference X` from KiCad 7). Split on footprint boundaries
    # so per-footprint regex doesn't run away across the whole file.
    try:
        import re as _re
        pcb_text = Path(pcb_kicad_path).read_text(encoding="utf-8",
                                                    errors="replace")
        ebom_rows = []
        fp_blocks = _re.split(r'(?=\n\s*\(footprint\s+")', pcb_text)
        for block in fp_blocks:
            if not block.strip().startswith('(footprint'):
                continue
            fp_m = _re.match(r'\s*\(footprint\s+"([^"]+)"', block)
            ref_m = _re.search(r'\(property\s+"Reference"\s+"([^"]+)"',
                                block)
            val_m = _re.search(r'\(property\s+"Value"\s+"([^"]+)"',
                                block)
            if fp_m and ref_m and val_m:
                ebom_rows.append({
                    "ref":       ref_m.group(1),
                    "value":     val_m.group(1),
                    "footprint": fp_m.group(1),
                })
        ebom_csv = bundle / "ebom.csv"
        with ebom_csv.open("w", encoding="utf-8") as f:
            f.write("ref,value,footprint\n")
            for r in ebom_rows:
                f.write(f"{r['ref']},{r['value']},{r['footprint']}\n")
        step("ebom_extract", True, count=len(ebom_rows),
              path=str(ebom_csv))
    except Exception as exc:
        step("ebom_extract", False, error=str(exc))

    # --- 6. Assembly instructions ----------------------------------------
    # generate_assembly_md groups parts by `spec` keyed to the stage list
    # (motor, prop, battery, esc_pcb, fc_pcb, standoff, …). The catalog
    # `subcategory` doesn't match those keys 1:1 so we map them, expand
    # quantities into per-instance entries, and pre-compute total mass.
    try:
        from aria_os.assembly_instructions import generate_assembly_md

        _SUBCAT_TO_STAGE_SPEC = {
            "bldc_outrunner": "motor",
            "stepper_motor":  "motor",
            "propeller":      "prop",
            "lipo_battery":   "battery",
            "esc":            "esc_pcb",
            "standoff":       "standoff",
        }
        asm_parts: list[dict] = []
        for row in (bom.get("purchased") or []):
            qty   = int(row.get("quantity") or 1)
            mass  = float(row.get("mass_g") or 0)
            desig = row.get("designation") or "?"
            spec  = (_SUBCAT_TO_STAGE_SPEC.get(row.get("subcategory") or "")
                     or row.get("subcategory") or "misc")
            for i in range(qty):
                asm_parts.append({
                    "name":      f"{desig}#{i+1}",
                    "spec":      spec,
                    "mass_g":    mass,
                    "material":  row.get("material") or "—",
                    "designation": desig,
                })
        # Frame and PCB are fabricated, not in `purchased`. Tag them so the
        # renderer slots them into stage 1 (frame → bottom_plate) and
        # stage 2 (fc_pcb).
        asm_parts.append({"name": "drone_frame",
                            "spec": "bottom_plate",
                            "mass_g": 38,
                            "material": "PLA / 3D-printed",
                            "step_path": str(frame_step)})
        asm_parts.append({"name": "fc_pcb",
                            "spec": "fc_pcb",
                            "mass_g": 7,
                            "material": "FR4 4-layer",
                            "step_path": str(pcb_step)})

        total_mass_g = sum(float(p.get("mass_g") or 0) for p in asm_parts)
        asm_bom = {
            "assembly_name": req.bundle_name,
            "name":          req.bundle_name,
            "parts":         asm_parts,
            "preset":        "fpv_drone",
            "total_mass_g":  total_mass_g,
            "mass_breakdown": {
                "Frame":          38.0,
                "FC PCB":          7.0,
                "Motors+Props":   sum(p["mass_g"] for p in asm_parts
                                        if p["spec"] in ("motor", "prop")),
                "Battery":        sum(p["mass_g"] for p in asm_parts
                                        if p["spec"] == "battery"),
                "Electronics":    sum(p["mass_g"] for p in asm_parts
                                        if p["spec"] in ("esc_pcb", "rf",
                                                           "sensor",
                                                           "connector")),
                "Hardware":       sum(p["mass_g"] for p in asm_parts
                                        if p["spec"] in ("standoff",
                                                           "fastener",
                                                           "strap")),
            },
        }
        asm_path = generate_assembly_md(asm_bom, bundle)
        step("assembly_md", True, path=str(asm_path),
              total_mass_g=round(total_mass_g, 2),
              parts_in_doc=len(asm_parts))
    except Exception as exc:
        step("assembly_md", False,
              error=f"{type(exc).__name__}: {exc}")

    # --- 6b. Design rationale doc — engineering "show your work" ---------
    # Justifies every component + design decision (motor KV, prop pitch,
    # ESC current headroom, battery cells, FC architecture, frame geometry,
    # tolerances, export classification) with calculations and citations.
    # Runs in parallel with the drawing generation steps below — the
    # markdown only depends on the BOM + ECAD comp list, both of which
    # are already settled at this point.
    try:
        from aria_os.design_rationale import generate_design_rationale
        # Pull the ECAD components directly from the .kicad_pcb (the
        # listener clears its state when a new board is started, so
        # querying /status after an unrelated reset returns empty).
        # Reuses the same per-footprint block parsing as the eBOM step.
        ecad_comps_for_rationale: list[dict] = []
        try:
            import re as _re
            pcb_text = Path(pcb_kicad_path).read_text(
                encoding="utf-8", errors="replace")
            for block in _re.split(
                    r'(?=\n\s*\(footprint\s+")', pcb_text):
                if not block.strip().startswith('(footprint'):
                    continue
                fp_m  = _re.match(r'\s*\(footprint\s+"([^"]+)"', block)
                ref_m = _re.search(r'\(property\s+"Reference"\s+"([^"]+)"',
                                     block)
                val_m = _re.search(r'\(property\s+"Value"\s+"([^"]+)"',
                                     block)
                if fp_m and ref_m and val_m:
                    ecad_comps_for_rationale.append({
                        "ref":         ref_m.group(1),
                        "value":       val_m.group(1),
                        "footprint":   fp_m.group(1),
                        "description": "",  # not embedded in .kicad_pcb
                    })
        except Exception:
            pass
        rationale_path = generate_design_rationale(
            build_config=config, bom=bom,
            ecad_components=ecad_comps_for_rationale,
            out_dir=bundle, goal=req.goal,
            platform_specs={
                "motor_count":         req.motor_count,
                "prop_size_in":        req.prop_size_in,
                "battery_cells":       req.battery_cells,
                "battery_capacity_mah": req.battery_capacity_mah,
            },
            pcb_kicad_path=pcb_kicad_path)
        step("design_rationale", rationale_path.is_file(),
              path=str(rationale_path),
              size=rationale_path.stat().st_size if rationale_path.is_file() else 0)
    except Exception as exc:
        step("design_rationale", False,
              error=f"{type(exc).__name__}: {exc}")

    # --- 7. PCB fab drawing (PDF) ----------------------------------------
    try:
        proc = __import__("subprocess").run(
            [cli, "pcb", "export", "pdf",
             "--output", str(bundle / "fc_pcb_fab.pdf"),
             "--layers", "F.Cu,F.Silkscreen,B.Cu,B.Silkscreen,Edge.Cuts",
             pcb_kicad_path],
            capture_output=True, timeout=30)
        step("pcb_fab_pdf",
              (bundle / "fc_pcb_fab.pdf").is_file(),
              path=str(bundle / "fc_pcb_fab.pdf"))
    except Exception as exc:
        step("pcb_fab_pdf", False, error=str(exc))

    # --- 7a. PCB fab DXF (machine-readable, accepted by every shop) ------
    pcb_dxf_path: Path | None = None
    try:
        dxf_dir = bundle / "drawings" / "pcb"
        dxf_dir.mkdir(parents=True, exist_ok=True)
        proc = __import__("subprocess").run(
            [cli, "pcb", "export", "dxf",
             "--output", str(dxf_dir),
             "--output-units", "mm",
             "--layers", "Edge.Cuts,F.Cu,F.Silkscreen,F.Mask",
             pcb_kicad_path],
            capture_output=True, timeout=30)
        # Pick the Edge.Cuts file as the canonical fab drawing source
        edge_cuts_dxf = next(dxf_dir.glob("*Edge_Cuts*.dxf"), None)
        if edge_cuts_dxf is not None and edge_cuts_dxf.is_file():
            pcb_dxf_path = edge_cuts_dxf
            step("pcb_fab_dxf", True,
                  path=str(pcb_dxf_path),
                  size=pcb_dxf_path.stat().st_size)
        else:
            step("pcb_fab_dxf", False,
                  error="no Edge.Cuts DXF produced",
                  stderr=(proc.stderr.decode("utf-8", "replace")[-200:]
                           if proc.stderr else ""))
    except Exception as exc:
        step("pcb_fab_dxf", False, error=str(exc))

    # --- 7b. PCB fab DXF + GD&T overlay ---------------------------------
    if pcb_dxf_path is not None:
        try:
            from aria_os.drawings.gdt_overlay import overlay_gdt
            gdt_dxf = bundle / "fc_pcb_fab_gdt.dxf"
            r = overlay_gdt(
                str(pcb_dxf_path), out_path=str(gdt_dxf),
                title=f"FC PCB — {req.bundle_name}",
                part_no=f"{req.bundle_name}-FC", material="FR4 4-layer",
                revision="A", company="ARIA-OS", drawer="ARIA-OS auto",
                tolerance_default="±0.1 mm", angular_default="±0.5°",
                surface_default="Ra 1.6, ENIG finish",
                position_tol_mm=0.10, hole_dia_mm=3.2)
            step("pcb_gdt_overlay", r.get("ok", False),
                  path=str(gdt_dxf) if gdt_dxf.is_file() else None,
                  n_datums=r.get("n_datums"),
                  n_fcfs=r.get("n_fcfs"),
                  n_holes=r.get("n_holes_dimensioned"))
        except Exception as exc:
            step("pcb_gdt_overlay", False,
                  error=f"{type(exc).__name__}: {exc}")

    # --- 7c. Frame DXF — CadQuery cross-section is the primary source --
    # FreeCAD TechDraw's `writeDXFPage` saves the template skeleton but
    # doesn't bake projected geometry into the DXF in headless 1.0 mode
    # (verified: 0 entities). The CQ-based projector imports the STEP,
    # slices at mid-height, and writes lines/arcs/circles directly — a
    # real DXF the GD&T overlay can read holes from. TechDraw still runs
    # for the .FCStd (good for GUI annotation) and as a redundancy check.
    frame_dxf      = bundle / "drone_frame.dxf"
    frame_dxf_gdt  = bundle / "drone_frame_gdt.dxf"
    frame_dwg      = bundle / "drone_frame.dwg"
    if (frame_step.is_file()
            and frame_step.stat().st_size > 10_000):
        # Primary: CadQuery cross-section -> DXF
        try:
            from aria_os.drawings.cq_dxf_projector import step_to_top_view_dxf
            r_cq = step_to_top_view_dxf(str(frame_step), str(frame_dxf))
            step("frame_cq_projection", r_cq.get("ok", False),
                  edges=r_cq.get("n_edges"),
                  circles=r_cq.get("n_circles"),
                  arcs=r_cq.get("n_arcs"),
                  lines=r_cq.get("n_lines"),
                  bbox=r_cq.get("bbox_mm"))
            if frame_dxf.is_file():
                shutil.copy(frame_dxf, frame_dwg)
        except Exception as exc:
            step("frame_cq_projection", False,
                  error=f"{type(exc).__name__}: {exc}")

        # Secondary: FreeCAD TechDraw (FCStd handoff for human review)
        try:
            from aria_os.drawings.mbd_drawings import generate_drawing
            tdraw_dir = bundle / "drawings" / "frame_techdraw"
            r_td = generate_drawing(
                str(frame_step), out_dir=str(tdraw_dir),
                title=f"Frame — {req.bundle_name}",
                part_no=f"{req.bundle_name}-FRAME",
                material="cfrp", revision="A", company="ARIA-OS")
            step("frame_techdraw", r_td.get("passed", False),
                  fcstd=r_td.get("fcstd_path"),
                  template=r_td.get("template"),
                  n_views=r_td.get("n_views"))
        except Exception as exc:
            step("frame_techdraw", False, error=str(exc))
    else:
        step("frame_cq_projection", False,
              reason="frame STEP missing or too small")

    # GD&T overlay on the produced frame DXF
    if frame_dxf.is_file():
        try:
            from aria_os.drawings.gdt_overlay import overlay_gdt
            r = overlay_gdt(
                str(frame_dxf), out_path=str(frame_dxf_gdt),
                title=f"Drone Frame — {req.bundle_name}",
                part_no=f"{req.bundle_name}-FRAME",
                material="Carbon fibre 3 mm",
                revision="A", company="ARIA-OS", drawer="ARIA-OS auto",
                tolerance_default="±0.2 mm", angular_default="±0.5°",
                surface_default="Ra 3.2",
                position_tol_mm=0.20, hole_dia_mm=3.2)
            step("frame_gdt_overlay", r.get("ok", False),
                  path=str(frame_dxf_gdt) if frame_dxf_gdt.is_file() else None,
                  n_datums=r.get("n_datums"),
                  n_fcfs=r.get("n_fcfs"),
                  n_holes=r.get("n_holes_dimensioned"))
        except Exception as exc:
            step("frame_gdt_overlay", False,
                  error=f"{type(exc).__name__}: {exc}")

    # --- 8. CadQuery STEP assembly (real mated assembly) -----------------
    assembly_step_path = None
    try:
        from aria_os.assembler import Assembler, AssemblyPart
        a = Assembler(repo_root=REPO_ROOT)
        parts = [
            AssemblyPart(step_path=str(frame_step),
                          position=(0, 0, 0), rotation=(0, 0, 0),
                          name="frame"),
            AssemblyPart(step_path=str(pcb_step),
                          position=(0, 0, 8), rotation=(0, 0, 0),
                          name="fc_pcb"),
        ]
        # Verify both files exist
        valid_parts = [p for p in parts if Path(p.step_path).is_file()]
        if len(valid_parts) >= 2:
            assembly_step_path = a.assemble(valid_parts, name=req.bundle_name)
            # Copy into the bundle
            target = bundle / "assembly.step"
            shutil.copy(assembly_step_path, target)
            step("assembly_step", True,
                  path=str(target),
                  size=target.stat().st_size)
        else:
            step("assembly_step", False,
                  error=f"only {len(valid_parts)} STEP files available")
    except Exception as exc:
        step("assembly_step", False,
              error=f"{type(exc).__name__}: {exc}")

    # --- 9. NATIVE SW assembly with mates (.sldasm) ----------------------
    # Drives the SW addin's beginAssembly + insertComponent + addMate +
    # saveAs to produce a real .sldasm with multiple mate types between
    # PCB and frame. Falls back gracefully if the addin is unreachable.
    sldasm_path = bundle / "assembly.sldasm"
    slddrw_path = bundle / "assembly.slddrw"
    # Pre-flight: process count, port, dll freshness, deployed sync.
    # auto_recover() redeploys a stale addin once before declaring NOT
    # ready, so the orchestrator self-heals between runs without telling
    # the user to "rebuild the addin and try again". A NOT-ready result
    # SKIPS the SW half (rather than failing the run); other artifacts
    # like BOM and design_rationale still finalize.
    sw_ready = req.mcad_cad.lower() in ("solidworks", "sw")
    if sw_ready and frame_step.is_file() and pcb_step.is_file():
        try:
            import importlib.util as _ilu
            _pf_path = REPO_ROOT / "scripts" / "sw_preflight.py"
            _spec = _ilu.spec_from_file_location("sw_preflight", str(_pf_path))
            _pf = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_pf)  # type: ignore
            _pf_result = _pf.auto_recover(7501)
            step("sw_preflight", _pf_result["ok"],
                  failed_checks=[c["name"] for c in _pf_result["checks"]
                                  if not c["ok"]],
                  remediation=_pf_result.get("remediation"))
            sw_ready = bool(_pf_result["ok"])
        except Exception as exc:
            step("sw_preflight", True, note=f"preflight unavailable: {exc}")
            # If preflight itself can't run, optimistically continue —
            # the inline status probe below is the legacy fallback.

    if (sw_ready and frame_step.is_file() and pcb_step.is_file()
            and req.mcad_cad.lower() in ("solidworks", "sw")):
        try:
            # 600s — the second insertComponent on a trace-rich PCB
            # STEP can take 60-90s on 16GB machines as SW imports + saves
            # the SLDPRT. 180s timed out on the v19 ARIA_EMIT_TRACES=1 run.
            with _httpx.Client(timeout=600.0) as c:
                try:
                    st_probe = c.get(f"{mcad_base}/status").json()
                except Exception as exc:
                    raise RuntimeError(f"SW addin probe failed: {exc}")
                if not st_probe.get("sw_connected"):
                    raise RuntimeError(f"SW not connected: {st_probe}")

                def _sw_op(kind: str, params: dict) -> dict:
                    r = c.post(f"{mcad_base}/op",
                                json={"kind": kind, "params": params})
                    return r.json()

                _sw_op("beginAssembly", {})
                _sw_op("insertComponent", {
                    "file": str(frame_step), "alias": "frame",
                    "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0})
                _sw_op("insertComponent", {
                    "file": str(pcb_step), "alias": "pcb",
                    "x_mm": 0.0, "y_mm": 0.0, "z_mm": 20.0})
                # Three mates lock the canonical "FC stack on frame" pose:
                #   1. Top  planes parallel  -> Z-axis aligned
                #   2. Front planes parallel -> no twist
                #   3. Right planes parallel -> rotation locked
                # Together: ~5 DOF locked (translation in Z still free,
                # to be locked by a distance mate or stand-off geometry).
                m1 = _sw_op("addMate", {
                    "type": "parallel",
                    "alias1": "pcb",   "plane1": "Top",
                    "alias2": "frame", "plane2": "Top"})
                m2 = _sw_op("addMate", {
                    "type": "parallel",
                    "alias1": "pcb",   "plane1": "Front",
                    "alias2": "frame", "plane2": "Front"})
                m3 = _sw_op("addMate", {
                    "type": "parallel",
                    "alias1": "pcb",   "plane1": "Right",
                    "alias2": "frame", "plane2": "Right"})
                save = _sw_op("saveAs",
                                {"path": str(sldasm_path).replace("\\", "/")})

            mates = [m1, m2, m3]
            mates_ok = sum(1 for m in mates
                            if (m.get("result") or {}).get("ok"))
            saved_ok = (save.get("result") or {}).get("ok")
            step("sw_native_assembly",
                  saved_ok and sldasm_path.is_file(),
                  path=str(sldasm_path) if sldasm_path.is_file() else None,
                  mates_ok=f"{mates_ok}/3")
        except Exception as exc:
            step("sw_native_assembly", False,
                  error=f"{type(exc).__name__}: {exc}")

    # --- 10. NATIVE SW drawing (.slddrw) — views + auto-dim + BOM ------
    if sldasm_path.is_file():
        try:
            with _httpx.Client(timeout=300.0) as c:
                r = c.post(f"{mcad_base}/op", json={
                    "kind": "createDrawing",
                    "params": {
                        "source":     str(sldasm_path).replace("\\", "/"),
                        "out":        str(slddrw_path).replace("\\", "/"),
                        "sheet_size": "A3",
                        "add_bom":    True,
                    },
                })
                rj = r.json()
            inner = (rj.get("result") or {})
            step("sw_native_drawing",
                  inner.get("ok") and slddrw_path.is_file(),
                  path=str(slddrw_path) if slddrw_path.is_file() else None,
                  views=inner.get("views"),
                  size=inner.get("size"))
        except Exception as exc:
            step("sw_native_drawing", False,
                  error=f"{type(exc).__name__}: {exc}")

    # --- 11. enrichDrawing — derive per-part GD&T from STEP geometry,
    #         pass to the addin so FCFs match the actual part envelope
    #         (rec #4 wired through to rec #3's real interop path).
    #         Failure here is non-fatal: bundle still ships without the
    #         note overlay.
    if sldasm_path.is_file() and slddrw_path.is_file():
        try:
            from aria_os.gdt.derive_tolerances import derive_for_bundle
            specs = derive_for_bundle(bundle)
            # Pick the largest-named part as representative for the asm
            # drawing's general-tolerance block. If no parts derived, the
            # addin still gets `gdt: True` boolean and emits its built-in
            # boilerplate (better than nothing).
            primary_id = None
            primary = None
            if specs:
                # Prefer the frame over PCBs for general-tol values, then
                # fall back to any first key.
                for pid in ("drone_frame", "frame"):
                    if pid in specs:
                        primary_id = pid; primary = specs[pid]; break
                if primary is None:
                    primary_id, primary = next(iter(specs.items()))
            params = {"gdt": True, "section_view": True, "exploded_view": True}
            if primary:
                # Strip note_lines (addin builds its own) but pass numeric
                # tolerances + datum letters + standard labels.
                for k in ("position_tolerance_mm", "flatness_mm",
                          "perpendicularity_mm", "general_linear_mm",
                          "general_angular_deg", "primary_datum",
                          "secondary_datum", "tertiary_datum",
                          "standard", "iso_class",
                          "material_label", "finish_label"):
                    if k in primary:
                        params[k] = primary[k]
            with _httpx.Client(timeout=300.0) as c:
                r = c.post(f"{mcad_base}/op", json={
                    "kind": "enrichDrawing", "params": params,
                })
                rj = r.json()
            inner = (rj.get("result") or {})
            step("sw_enrich_drawing", inner.get("ok"),
                  primary_part=primary_id,
                  pos_tol=params.get("position_tolerance_mm"),
                  flat=params.get("flatness_mm"),
                  perp=params.get("perpendicularity_mm"),
                  gdt_ok=(inner.get("report") or {}).get("gdt", {}).get("ok"),
                  section_ok=(inner.get("report") or {}).get(
                                "section_view", {}).get("ok"),
                  exploded_ok=(inner.get("report") or {}).get(
                                "exploded_view", {}).get("ok"))
        except Exception as exc:
            step("sw_enrich_drawing", False,
                  error=f"{type(exc).__name__}: {exc}")

        # --- 12. Auto-loop verify gate (rec #7) — render the .slddrw
        #         to PDF→PNG, hand it to vision API with a checklist
        #         derived from the params we just sent, retry once if
        #         FAIL. Self-healing so the orchestrator doesn't ship
        #         a drawing missing FCFs/datums silently.
        try:
            from aria_os.drawing.verify_drawing import verify_and_recover
            verify_expected = dict(params)  # what we asked for
            verify_expected["section_view"] = True
            verify_expected["exploded_view"] = True
            v = verify_and_recover(slddrw_path, verify_expected,
                                     retry_params={"force_recompute": True},
                                     port=7501, max_retries=1)
            step("sw_verify_drawing",
                  bool(v.get("verified")) if v.get("verified") is not None
                                              else True,
                  verified=v.get("verified"),
                  confidence=v.get("confidence"),
                  missing=v.get("missing"),
                  retries=len(v.get("retries_used") or []),
                  reason=v.get("reason"),
                  pdf=v.get("pdf"),
                  screenshot=v.get("screenshot"))
        except Exception as exc:
            # Verify gate is best-effort. A failure here doesn't block
            # the bundle from shipping (the .slddrw is still on disk).
            step("sw_verify_drawing", True,
                  note=f"verify gate skipped: {type(exc).__name__}: {exc}")

    return {
        "ok":     all(s["ok"] for s in log if s["step"] not in (
            "pcb_fab_pdf", "ebom_kicad")),  # tolerated optionals
        "bundle": str(bundle),
        "log":    log,
        "files":  sorted([str(p.relative_to(bundle))
                            for p in bundle.rglob("*") if p.is_file()]),
    }


# --------------------------------------------------------------------------- #
# Pipeline runner (runs in background thread to keep FastAPI responsive)
# --------------------------------------------------------------------------- #

_MCAD_MARKERS = (
    "enclosure", "housing", "bracket", "frame", "chassis",
    "case", "shell", "mount", "mounts", "boss", "bosses",
    "stand-off", "standoff", "stand-offs", "standoffs",
    "lid", "cover", "panel", "plate", "shaft", "flange",
    "gear", "impeller", "nozzle", "ring", "wheel",
    "heatsink", "heat sink", "tube", "spacer", "rod",
    "knob", "handle", "lever", "arm", "leg", "fork",
    "machined", "cnc", "3d print", "3d-print", "3d printed", "printed",
    "aluminium", "aluminum", "steel", "titanium", "abs", "petg", "nylon",
    "carbon fibre", "carbon fiber",
)
_ECAD_MARKERS = (
    "kicad", "gerber", "schematic", "circuit board",
    "footprint", "netlist", "esp32", "esp8266", "atmega", "rp2040", "stm32",
    "pi pico", "raspberry pi pico", "dev board", "devboard", "breakout board", "breakout",
    "microcontroller", "mcu", "header", "headers",
    "usb-c", "usb c", "micro-usb", "micro usb",
    "buck converter", "linear regulator", "buck", "ldo",
    "sensor breakout",
)
_ECAD_ELECTRONICS_COOCCURRENCE = {
    "layer", "kicad", "schematic", "trace", "pad", "footprint", "gerber",
    "smd", "tht", "smt", "via", "net", "bms", "battery", "esp32", "mcu",
    "controller", "imu", "barometer",
}


def _auto_detect_mode(goal: str) -> str:
    """Pick a pipeline mode by scanning the goal text using word-boundary
    regex matches so we don't get substring false positives (e.g.
    'mate' inside 'material' → assembly, 'pcb' inside 'upcbomb', etc.).

    Routing (first hit wins, except the combined-domain check which runs
    BEFORE single-domain matches):
      - MCAD + ECAD markers both present     → system   (drives full-build)
      - sheet metal phrasing                  → sheetmetal
      - PCB / KiCad / gerber / schematic      → kicad
      - drawing / dimensions / GD&T / sheet   → dwg
      - assembly / mate / joint / mount X to  → asm
      - otherwise                              → native (mechanical Part)
    """
    import re as _re
    g = (goal or "").lower()

    def _any_word(patterns: tuple) -> bool:
        """True if any pattern appears as a word or word-bounded phrase."""
        for p in patterns:
            if _re.search(rf"\b{_re.escape(p)}\b", g):
                return True
        return False

    def _has_pcb_with_electronics() -> bool:
        """Check if 'pcb' appears with electronics co-occurrence markers.

        Prevents false positives like 'PCB drill bit holder' (mechanical part
        named after a fixture, not an electronics part). Returns True only if:
        - 'pcb' is word-bounded, AND
        - At least one electronics term (layer, kicad, trace, pad, etc.)
          also appears in the goal.
        """
        if not _re.search(r"\bpcb\b", g):
            return False
        # Check for co-occurrence with electronics vocabulary
        return any(_re.search(rf"\b{_re.escape(term)}\b", g)
                   for term in _ECAD_ELECTRONICS_COOCCURRENCE)

    # AutoCAD-specific industries (civil / structural / MEP / 2D
    # drafting / architecture). Runs FIRST so a "site plan" or
    # "building elevation drawing" prompt doesn't get pulled into
    # the dwg path (which targets SW drawing docs).
    try:
        from dashboard.autocad_routing import is_autocad_goal
        if is_autocad_goal(goal or ""):
            return "autocad"
    except Exception:
        pass

    has_mcad = _any_word(_MCAD_MARKERS)
    has_ecad = _any_word(_ECAD_MARKERS) or _has_pcb_with_electronics()
    # Combined MCAD+ECAD prompt → drive the full electromechanical build
    # so SW + KiCad both run. Without this short-circuit, a single ECAD
    # marker (e.g. "PCB" inside an enclosure prompt) wins via first-hit
    # routing and the MCAD half is silently skipped.
    if has_mcad and has_ecad:
        return "system"

    # Sheet metal FIRST — otherwise "sheet" keywords collide with DWG.
    if _any_word(("sheet metal", "sheet-metal", "bracket with bend",
                   "bent plate", "formed sheet", "enclosure with bends",
                   "bends", "bend radius", "flanged enclosure",
                   "folded plate", "formed bracket")):
        return "sheetmetal"
    if _has_pcb_with_electronics() or _any_word(("kicad", "gerber", "schematic",
                   "circuit board", "footprint", "netlist",
                   "esp32", "esp8266", "atmega", "rp2040", "stm32",
                   "pi pico", "raspberry pi pico", "dev board", "devboard",
                   "breakout board", "breakout")):
        return "kicad"
    if _any_word(("drawing", "dimensions", "gd&t", "gdt",
                   "drawing sheet", "technical drawing",
                   "dwg file", "mechanical drawing")):
        return "dwg"
    if _any_word(("assembly", "assemble", "mate", "mates",
                   "mounted to", "mount the", "joined to",
                   "attached to", "attach the",
                   "sub-assembly", "sub assembly")):
        return "asm"
    return "native"


def _run_pipeline(goal: str, max_attempts: int,
                   mode: str = "mechanical",
                   quality_tier: str = "balanced",
                   host_context: dict | None = None) -> None:
    """Dispatch the pipeline based on the chat panel's mode selector.

    mechanical → standard orchestrator.run (CadQuery / SDF / Grasshopper)
    electrical → aria_os.ecad.ecad_generator.generate_ecad
    assembly   → aria_os.agents.assembly_agent.run_assembly_agent_sync
    native     → stream per-feature ops into the hosted CAD's timeline
                 via the panel bridge (no STEP import; real Fusion tree)
    """
    try:
        # Auto-detect mode from prompt keywords when user left it on "auto"
        if mode == "auto":
            detected = _auto_detect_mode(goal)
            event_bus.emit("agent",
                            f"Auto-routing: '{goal[:60]}' → {detected} mode",
                            {"detected_mode": detected, "original": "auto"})
            mode = detected

        event_bus.emit("step", f"Mode: {mode} · tier: {quality_tier}",
                        {"mode": mode, "quality_tier": quality_tier})

        if mode == "autocad":
            # AutoCAD bridge — civil/structural/MEP/2D drafting. The
            # listener is a separate Python process on port 7503 (see
            # cad-plugins/autocad/aria_autocad_server.py). We pick the
            # AutoCAD-flavored drawing planner so GD&T frames map to
            # AutoCAD's TOLERANCE command and dimensioning to DIM*.
            try:
                from aria_os.native_planner.dwg_planner_autocad import (
                    plan_autocad_drawing)
                from aria_os.spec_extractor import extract_spec
                spec = extract_spec(goal) or {}
                plan = plan_autocad_drawing(spec, goal=goal)
                event_bus.emit("agent",
                                f"AutoCAD plan ready — {len(plan)} ops",
                                {"n_ops": len(plan), "domain": "autocad"})
                base = _CAD_BASE_URL.get("autocad",
                                            "http://localhost:7503")
                import httpx as _httpx
                with _httpx.Client(timeout=60.0) as c:
                    for i, op in enumerate(plan):
                        event_bus.emit("native_op",
                                        op.get("label") or op["kind"],
                                        {"seq": i + 1, "total": len(plan),
                                         "kind": op["kind"],
                                         "params": op.get("params", {}),
                                         "domain": "autocad"})
                        try:
                            r = c.post(f"{base}/op", json={
                                "kind":   op["kind"],
                                "params": op.get("params", {}),
                            })
                            event_bus.emit("native_result",
                                            f"✓ {op['kind']}",
                                            {"kind": op["kind"],
                                             "reply": r.json(),
                                             "seq": i + 1})
                        except Exception as _ae:
                            event_bus.emit("error",
                                f"AutoCAD op failed: {op['kind']} — {_ae}",
                                {"kind": op["kind"], "seq": i + 1})
                            break
                event_bus.emit("complete",
                                f"AutoCAD pipeline complete for {goal[:60]}",
                                {"goal": goal, "mode": "autocad",
                                 "n_ops": len(plan)})
                return
            except Exception as _aE:
                event_bus.emit("error",
                                f"AutoCAD pipeline error: {_aE}",
                                {"goal": goal, "mode": mode})
                return

        if mode == "sheetmetal":
            # Fusion Sheet Metal workspace — uses flange/bend commands
            try:
                from aria_os.native_planner.sheetmetal_planner import plan_simple_bracket
                from aria_os.native_planner.validator import validate_plan
                from aria_os.spec_extractor import extract_spec
                spec = extract_spec(goal) or {}
                event_bus.emit("agent",
                                f"SpecAgent: {sorted(spec.keys())[:6]}",
                                {"spec": spec})
                plan = plan_simple_bracket(spec)
                ok, issues = validate_plan(plan)
                if not ok:
                    event_bus.emit("error",
                                    f"Sheet metal plan invalid: {issues[:2]}")
                    return
                event_bus.emit("agent",
                                f"Sheet metal plan ready — {len(plan)} operations",
                                {"n_ops": len(plan), "domain": "sheetmetal"})
                for i, op in enumerate(plan):
                    event_bus.emit(
                        "native_op", op.get("label") or op["kind"],
                        {"seq": i + 1, "total": len(plan),
                         "kind": op["kind"], "params": op.get("params", {}),
                         "domain": "sheetmetal"})
                event_bus.emit("complete",
                                f"Pipeline complete for {goal[:60]}",
                                {"goal": goal, "mode": "sheetmetal",
                                 "n_ops": len(plan)})
                return
            except Exception as _se:
                event_bus.emit("error",
                                f"Sheet metal planner error: {_se}",
                                {"goal": goal, "mode": mode})
                return

        if mode in ("dwg", "asm"):
            # Both modes stream native_op events for the panel to
            # dispatch through the Fusion/Rhino/Onshape bridge. They
            # differ only in which planner builds the op list.
            try:
                from aria_os.spec_extractor import extract_spec
                spec = extract_spec(goal) or {}
                if mode == "dwg":
                    from aria_os.native_planner.dwg_planner import plan_simple_drawing
                    plan = plan_simple_drawing(spec)
                    domain = "drawing"
                else:
                    from aria_os.native_planner.asm_planner import plan_simple_assembly
                    plan = plan_simple_assembly(spec)
                    domain = "assembly"
                event_bus.emit("agent",
                                f"{mode.upper()} planner ready — {len(plan)} operations",
                                {"n_ops": len(plan), "domain": domain})
                for i, op in enumerate(plan):
                    event_bus.emit(
                        "native_op", op.get("label") or op["kind"],
                        {"seq": i + 1, "total": len(plan),
                         "kind": op["kind"], "params": op.get("params", {}),
                         "domain": domain})
                event_bus.emit("complete",
                                f"Pipeline complete for {goal[:60]}",
                                {"goal": goal, "mode": mode,
                                 "n_ops": len(plan)})
                return
            except Exception as _de:
                event_bus.emit("error",
                                f"{mode.upper()} planner error: {_de}",
                                {"goal": goal, "mode": mode})
                return

        if mode == "onshape":
            # Direct REST execution into an Onshape Part Studio.
            try:
                from aria_os.native_planner.dispatcher import make_plan
                from aria_os.spec_extractor import extract_spec
                from aria_os.onshape import OnshapeExecutor, get_client

                # The panel (or prompt) can pass {did, wid, eid}; if not,
                # use the most recent document + its default workspace +
                # first Part Studio.
                client = get_client(repo_root=REPO_ROOT)
                did = (host_context or {}).get("onshape_did")
                wid = (host_context or {}).get("onshape_wid")
                eid = (host_context or {}).get("onshape_eid")
                if not all([did, wid, eid]):
                    event_bus.emit("agent",
                                    "Onshape: no target document specified — "
                                    "using most recent")
                    docs = client.list_documents(limit=1)
                    if not docs:
                        event_bus.emit("error",
                                        "No Onshape documents found — "
                                        "create one at cad.onshape.com first")
                        return
                    d = docs[0]
                    did = d["id"]
                    wid = d["defaultWorkspace"]["id"]
                    studios = client.list_part_studios(did, wid)
                    if not studios:
                        event_bus.emit("error",
                                        f"No Part Studios in document "
                                        f"'{d.get('name')}'; create one first")
                        return
                    eid = studios[0]["id"]
                    event_bus.emit("agent",
                                    f"Onshape: targeting '{d.get('name')[:40]}' "
                                    f"→ '{studios[0].get('name', 'Part Studio 1')}'")

                spec = extract_spec(goal) or {}
                plan = make_plan(goal, spec,
                                  quality=quality_tier,
                                  repo_root=REPO_ROOT)
                event_bus.emit("agent",
                                f"Onshape plan ready — {len(plan)} operations",
                                {"n_ops": len(plan), "domain": "onshape"})
                executor = OnshapeExecutor(did, wid, eid, client=client)
                for i, op in enumerate(plan):
                    event_bus.emit(
                        "native_op", op.get("label") or op["kind"],
                        {"seq": i + 1, "total": len(plan),
                         "kind": op["kind"], "params": op.get("params", {}),
                         "domain": "onshape"})
                    try:
                        res = executor.execute(op["kind"],
                                                 op.get("params", {}))
                        event_bus.emit("native_result",
                                        f"✓ {op['kind']}",
                                        {"kind": op["kind"], "reply": res,
                                         "seq": i + 1, "domain": "onshape"})
                    except Exception as _opE:
                        event_bus.emit("error",
                                        f"Onshape op failed: {op['kind']} — {_opE}",
                                        {"kind": op["kind"], "seq": i + 1})
                        break
                event_bus.emit("complete",
                                f"Pipeline complete for {goal[:60]}",
                                {"goal": goal, "mode": "onshape",
                                 "n_ops": len(plan),
                                 "did": did, "wid": wid, "eid": eid})
                return
            except Exception as _oe:
                event_bus.emit("error",
                                f"Onshape pipeline error: {_oe}",
                                {"goal": goal, "mode": mode})
                return

        if mode == "kicad":
            # Server-side executor — KiCad has no live WebView bridge, so
            # ops apply to a growing .kicad_pcb on disk. Panel still shows
            # the ops streaming in its feature tree, same as mechanical
            # native mode.
            try:
                from aria_os.native_planner.kicad_planner import plan_led_board
                from aria_os.native_planner.kicad_executor import KicadExecutor
                from aria_os.spec_extractor import extract_spec

                spec = extract_spec(goal) or {}
                event_bus.emit("agent",
                                f"ECAD SpecAgent: {sorted(spec.keys())[:6]}",
                                {"spec": spec})

                # Hardcoded for LED board keyword; else LLM-driven.
                g = (goal or "").lower()
                is_led_demo = ("led" in g and
                                any(k in g for k in ("usb", "demo", "blink", "indicator"))
                                and len(g) < 100)
                if is_led_demo:
                    plan = plan_led_board(spec)
                    event_bus.emit("agent",
                                    "ECAD planner: hardcoded LED demo")
                else:
                    event_bus.emit("agent",
                                    "ECAD planner: LLM-generated (structured output)")
                    from aria_os.native_planner.ecad_llm_planner import plan_ecad_from_llm
                    try:
                        plan = plan_ecad_from_llm(
                            goal, spec,
                            quality=quality_tier,
                            repo_root=REPO_ROOT,
                            host_context=host_context)
                    except ValueError as _lexc:
                        # LLM failed — fall back to LED demo so the user
                        # sees *something* rather than a silent error.
                        event_bus.emit(
                            "warning",
                            f"ECAD LLM failed ({_lexc}); falling back to LED demo")
                        plan = plan_led_board(spec)
                event_bus.emit("agent",
                                f"ECAD plan ready — {len(plan)} operations",
                                {"n_ops": len(plan), "domain": "ecad"})

                out_path = (REPO_ROOT / "outputs" / "ecad" /
                             f"aria_pcb_{abs(hash(goal)) % 10_000}.kicad_pcb")
                executor = KicadExecutor(out_path)

                for i, op in enumerate(plan):
                    event_bus.emit(
                        "native_op", op.get("label") or op["kind"],
                        {"seq": i + 1, "total": len(plan),
                         "kind": op["kind"], "params": op.get("params", {}),
                         "domain": "ecad"})
                    try:
                        res = executor.execute(op["kind"], op.get("params", {}))
                        event_bus.emit(
                            "native_result",
                            f"✓ {op['kind']}",
                            {"kind": op["kind"], "reply": res,
                             "seq": i + 1, "domain": "ecad"})
                    except Exception as _opE:
                        event_bus.emit(
                            "error",
                            f"KiCad op failed: {op['kind']} — {_opE}",
                            {"kind": op["kind"],
                             "params": op.get("params", {}),
                             "seq": i + 1})
                        break

                event_bus.emit("complete",
                                f"Pipeline complete for {goal[:60]}",
                                {"goal": goal, "mode": "kicad",
                                 "n_ops": len(plan),
                                 "out_path": str(out_path)})
                return
            except Exception as _ke:
                event_bus.emit("error",
                                f"KiCad planner error: {_ke}",
                                {"goal": goal, "mode": mode})
                return

        if mode == "native":
            try:
                from aria_os.native_planner.dispatcher import (
                    make_plan, is_supported)
                from aria_os.native_planner.delta_detector import (
                    classify_delta, build_modify_plan)
                from aria_os.spec_extractor import extract_spec

                # --- Delta detection: is this a NEW part, a MODIFY of
                # --- existing params, or an EXTEND with new features?
                # --- Runs BEFORE the spec pass since modify skips most
                # --- of the pipeline.
                cls: dict | None = None
                try:
                    cls = classify_delta(goal, host_context,
                                          quality=quality_tier,
                                          repo_root=REPO_ROOT)
                    event_bus.emit("agent",
                                    f"DeltaDetector: {cls['kind']} "
                                    f"({cls['method']}, "
                                    f"{cls.get('param_count', 0)} existing params)",
                                    cls)
                    if cls["kind"] == "modify":
                        # Short-circuit: just stream parameter-update ops
                        modify_plan = build_modify_plan(goal, host_context or {})
                        event_bus.emit(
                            "agent",
                            f"Modify plan ready — {len(modify_plan)} parameter update(s)",
                            {"n_ops": len(modify_plan),
                             "domain": "modify"})
                        for i, op in enumerate(modify_plan):
                            event_bus.emit(
                                "native_op",
                                op.get("label") or op["kind"],
                                {"seq": i + 1, "total": len(modify_plan),
                                 "kind": op["kind"],
                                 "params": op.get("params", {}),
                                 "delta_kind": "modify"})
                        event_bus.emit("complete",
                                        f"Pipeline complete — modified "
                                        f"{len(modify_plan)} parameter(s)",
                                        {"goal": goal, "mode": "native",
                                         "delta_kind": "modify"})
                        return
                except Exception as _dce:
                    event_bus.emit("warning",
                                    f"Delta detection skipped: {_dce}")
                    # Fall through — treat as a regular new-part prompt

                # Regex spec pass — fast, deterministic, no LLM
                spec = extract_spec(goal) or {}
                # Fold in live user parameters ONLY for 'extend' delta
                # prompts. On 'new' prompts, the user is starting fresh
                # — stale params from a prior design would pollute the
                # spec (e.g. flange_bore=200mm from the old flange gets
                # picked up for a new flange where bore defaults to
                # 20mm, blowing out the geometry).
                delta_kind = (cls.get("kind") if cls else "new")
                if (delta_kind == "extend" and host_context
                        and host_context.get("user_parameters")):
                    for p in host_context["user_parameters"]:
                        name = (p.get("name") or "").lower()
                        expr = p.get("expression") or ""
                        import re as _re
                        m = _re.search(r"(\d+(?:\.\d+)?)", expr)
                        if not m: continue
                        val = float(m.group(1))
                        if name.endswith("_od") and "od_mm" not in spec:
                            spec["od_mm"] = val
                        elif name.endswith("_bore") and "bore_mm" not in spec:
                            spec["bore_mm"] = val
                        elif name.endswith("_thickness") and "thickness_mm" not in spec:
                            spec["thickness_mm"] = val
                        elif name.endswith("_width") and "width_mm" not in spec:
                            spec["width_mm"] = val
                        elif name.endswith("_depth") and "depth_mm" not in spec:
                            spec["depth_mm"] = val
                event_bus.emit("agent",
                                f"SpecAgent (regex): {sorted(spec.keys())[:6]}",
                                {"spec": spec})

                # Selection-aware augmentation: if the user has entities
                # selected in Fusion, mention them in the goal so the LLM
                # can target them (e.g. "fillet this edge" → with edge
                # id in context, LLM can emit a fillet referencing it).
                sel = (host_context or {}).get("selection") or []
                if sel:
                    sel_types = ", ".join(s.get("type", "?") for s in sel[:5])
                    event_bus.emit(
                        "agent",
                        f"Selection context: {len(sel)} entit(ies) ({sel_types})",
                        {"n_selected": len(sel),
                         "types": sel_types})
                    goal = (
                        f"{goal}\n\n"
                        f"## Current selection in Fusion\n"
                        + "\n".join(f"  - {s.get('type', '?')}: {s.get('id', '')[:40]}"
                                    for s in sel[:8]))

                # Skip SpecAgent LLM enrichment when a hardcoded planner
                # will handle this part OR when regex already captured
                # ≥3 dimensions. The 30-60s LLM call is pure latency
                # when the spec is already rich.
                hardcoded = is_supported(goal)
                dim_count = sum(1 for k in spec
                                 if k.endswith("_mm") and spec.get(k) is not None)
                if not hardcoded and dim_count < 3:
                    try:
                        from aria_os.agents.spec_agent import SpecAgent
                        from aria_os.agents.design_state import DesignState
                        _state = DesignState(
                            goal=goal,
                            repo_root=REPO_ROOT,
                            domain="cad",
                            max_iterations=1,
                        )
                        _state.spec.update(spec)
                        SpecAgent(REPO_ROOT).extract(_state)
                        enriched = dict(_state.spec)
                        new_keys = [k for k in enriched if k not in spec]
                        if new_keys:
                            event_bus.emit(
                                "agent",
                                f"SpecAgent (LLM) filled: {new_keys[:6]}",
                                {"new_keys": new_keys})
                        spec = enriched
                    except Exception as _se:
                        event_bus.emit(
                            "warning",
                            f"SpecAgent LLM enrichment skipped: {_se}",
                            {"fallback": "regex-only spec"})
                event_bus.emit(
                    "agent",
                    f"Planner: {'hardcoded' if hardcoded else 'LLM-generated'}",
                    {"hardcoded": hardcoded})

                plan = make_plan(goal, spec,
                                  quality=quality_tier,
                                  repo_root=REPO_ROOT,
                                  allow_llm=True)
                event_bus.emit("agent",
                                f"Native plan ready — {len(plan)} operations",
                                {"n_ops": len(plan)})

                # Stream each op as a native_op event. Panel calls
                # bridge.executeFeature per event; Fusion's browser tree
                # fills in live.
                for i, op in enumerate(plan):
                    event_bus.emit(
                        "native_op", op.get("label") or op.get("kind", ""),
                        {"seq": i + 1, "total": len(plan),
                         "kind": op["kind"], "params": op.get("params", {})})
                event_bus.emit("complete",
                                f"Pipeline complete for {goal[:60]}",
                                {"goal": goal, "mode": "native",
                                 "n_ops": len(plan)})
                return
            except Exception as _ne:
                event_bus.emit("error",
                                f"Native planner error: {_ne}",
                                {"goal": goal, "mode": mode})
                return
        if mode == "electrical":
            from aria_os.ecad.ecad_generator import generate_ecad
            out_bom, out_pcb = generate_ecad(goal)
            event_bus.emit("complete", "ECAD generation complete",
                            {"bom_path": str(out_bom), "pcb_path": str(out_pcb)})
        elif mode == "assembly":
            from aria_os.agents.assembly_agent import run_assembly_agent_sync
            result = run_assembly_agent_sync(goal, repo_root=REPO_ROOT)
            event_bus.emit("complete", "Assembly generation complete",
                            {"result": str(result)[:300]})
        else:
            # Default mechanical path — honors the SkillProfile quality tier
            # by constructing a profile with the requested tier and passing
            # it through to the orchestrator.
            from aria_os.orchestrator import run
            try:
                from aria_os.skill_profile import SkillProfile, SkillLevel
                # Quality tier → skill level mapping:
                #   fast → intermediate (cheap cloud routing)
                #   balanced → advanced  (our normal default)
                #   premium → veteran    (Sonnet-first)
                tier_to_level = {
                    "fast": SkillLevel.INTERMEDIATE,
                    "balanced": SkillLevel.ADVANCED,
                    "premium": SkillLevel.VETERAN,
                }
                profile = SkillProfile.for_level(
                    tier_to_level.get(quality_tier, SkillLevel.ADVANCED),
                    source="cli")
            except Exception:
                profile = None
            run(goal, repo_root=REPO_ROOT, max_attempts=max_attempts,
                skill_profile=profile)
    except Exception as e:
        event_bus.emit("error", f"Pipeline error: {e}", {"goal": goal, "mode": mode})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.post("/api/generate")
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    """Kick off the ARIA pipeline in a background thread.
    Routes by `mode` (mechanical / electrical / assembly). Passes
    `quality_tier` through as a SkillProfile level so the LLM chain
    uses the right quality (fast / balanced / premium)."""
    event_bus.emit(
        "step", f"Received goal: {req.goal[:80]}",
        {"goal": req.goal, "mode": req.mode, "quality_tier": req.quality_tier})
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_pipeline,
                          req.goal, req.max_attempts,
                          req.mode, req.quality_tier,
                          req.host_context)
    return {"status": "started", "goal": req.goal,
             "mode": req.mode, "quality_tier": req.quality_tier}


# --------------------------------------------------------------------------- #
# Quickstart endpoint — YC-application "single textarea" entry point.
# Thin wrapper around the same pipeline path /api/generate uses, but tagged
# as `surface=quickstart` in the run manifest / event stream so we can
# distinguish quickstart traffic from chat-panel / dashboard traffic later
# without duplicating any orchestrator logic.
# --------------------------------------------------------------------------- #

class QuickstartRequest(BaseModel):
    """Body schema for POST /api/v1/quickstart/generate.

    Only `goal` is required. `mode` here is the *input mode* — how the
    user typed the prompt (text / voice / image) — not the pipeline
    routing mode. The pipeline routing mode is auto-detected from the
    goal text via _auto_detect_mode (same logic /api/generate uses).
    """
    goal: str
    mode: str = "text"               # input mode: text | voice | image
    quality_tier: str = "balanced"   # fast | balanced | premium
    max_attempts: int = 3


@app.post("/api/v1/quickstart/generate")
async def quickstart_generate(req: QuickstartRequest):
    """Quickstart launchpad: single-textarea → full ARIA pipeline.

    Wraps the same `_run_pipeline` helper that /api/generate calls.
    Differences:
      - tags the run as surface=quickstart in the event stream
      - auto-routes to a pipeline mode via _auto_detect_mode (the user
        on the quickstart page never picks mechanical/electrical/etc.)
      - returns the same SSE-shaped response so the frontend can subscribe
        to /api/log/stream just like the dashboard does

    Frontend should subscribe to /api/log/stream for live progress.
    """
    goal = (req.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=422,
                             detail="goal must be a non-empty string")
    pipeline_mode = _auto_detect_mode(goal)
    # Surface tag — purely diagnostic. Lands in the SSE log so analytics
    # can grep "surface=quickstart" without us touching the orchestrator
    # or the run_manifest schema.
    event_bus.emit(
        "step",
        f"Quickstart: surface=quickstart input_mode={req.mode} "
        f"routed_mode={pipeline_mode}",
        {"surface": "quickstart",
         "input_mode": req.mode,
         "routed_mode": pipeline_mode,
         "goal": goal,
         "quality_tier": req.quality_tier})
    event_bus.emit(
        "step", f"Received goal: {goal[:80]}",
        {"goal": goal, "mode": pipeline_mode,
         "quality_tier": req.quality_tier,
         "surface": "quickstart"})
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_pipeline,
                          goal, req.max_attempts,
                          pipeline_mode, req.quality_tier,
                          None)
    return {"status": "started",
             "goal": goal,
             "mode": pipeline_mode,
             "input_mode": req.mode,
             "surface": "quickstart",
             "quality_tier": req.quality_tier}


@app.get("/api/log/stream")
async def log_stream():
    """SSE endpoint — streams pipeline events to this specific subscriber.

    Each connection gets its own subscriber cursor (see event_bus.subscribe)
    so multiple panels can listen concurrently without racing. Late
    subscribers get the last 30 events as history on connect so they don't
    miss the pipeline's first few seconds.
    """
    sub_id = event_bus.subscribe()

    async def generator():
        try:
            # Replay the last ~30 events so a chat panel that attaches
            # mid-pipeline still sees prior context (plan, route, etc.).
            for ev in event_bus.get_history(30):
                yield f"data: {json.dumps(_safe_event(ev), default=str)}\n\n"
            while True:
                events = await asyncio.get_event_loop().run_in_executor(
                    None, event_bus.get_events, 0.5, sub_id
                )
                for ev in events:
                    yield f"data: {json.dumps(_safe_event(ev), default=str)}\n\n"
                yield ": heartbeat\n\n"
        finally:
            event_bus.unsubscribe(sub_id)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            # Disable all proxy / browser buffering. Without this, some
            # stacks (nginx default, uvicorn under certain conditions)
            # buffer chunked transfer encoding and the panel only sees
            # events when the connection closes.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _safe_event(ev: dict) -> dict:
    """Coerce an event dict to pure-Python types so FastAPI's default
    JSON encoder doesn't choke on numpy.bool / numpy.int64 / Path in
    the `data` field. Any value that doesn't survive `json.dumps`
    gets str()'d."""
    import json as _json
    out = {}
    for k, v in ev.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple, dict)):
            try:
                _json.dumps(v)
                out[k] = v
            except TypeError:
                out[k] = _json.loads(_json.dumps(v, default=str))
        else:
            out[k] = str(v)
    return out


@app.get("/api/log/recent")
async def log_recent(n: int = 50):
    """Synchronous catch-up for chat panels — returns the last n events so
    the frontend can render state even if SSE attaches later."""
    return {"events": [_safe_event(e) for e in
                        event_bus.get_history(max(1, min(500, n)))]}


@app.get("/api/parts")
async def list_parts():
    """Return all part attempts from the learning log."""
    log_path = REPO_ROOT / "outputs" / "cad" / "learning_log.json"
    if not log_path.exists():
        return {"parts": []}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        # learning_log is a list of attempt records
        if isinstance(data, list):
            return {"parts": data}
        return {"parts": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class NativeEvalRequest(BaseModel):
    """Panel → backend: after streaming ops, panel calls `bridge.exportCurrent`
    and sends the resulting URL (or local path via the download endpoint) so
    the backend can run the existing EvalAgent / visual verifier on the
    actual geometry Fusion/Rhino produced."""
    goal: str
    stl_url: str | None = None     # URL (preferred — works across hosts)
    stl_path: str | None = None    # absolute path (only when same machine)
    spec: dict | None = None
    quality_tier: str = "balanced" # LLM tier for refinement if FAIL
    iteration: int = 1             # which refinement pass we're on
    max_iterations: int = 3        # bail out after this many refinements


@app.post("/api/native_eval")
async def native_eval(req: NativeEvalRequest):
    """Run EvalAgent + visual verifier against a host-exported STL.

    Accepts either a URL (we fetch) or an absolute path (must resolve
    inside outputs/). Emits `visual` events as verification progresses so
    the feature tree fills in pass/fail rows. Returns the final result
    synchronously for callers that want it.
    """
    import os, tempfile, urllib.request
    # --- Resolve the STL file on disk ---
    stl_path = None
    if req.stl_path:
        # Allow paths under any of: outputs/, ~/aria-exports/, the
        # system temp dir. These are the locations Fusion/Rhino/
        # Onshape write to when the bridge calls exportCurrent().
        allowed_prefixes = [
            os.path.realpath(str(REPO_ROOT / "outputs")),
            os.path.realpath(str(Path.home() / "aria-exports")),
            os.path.realpath(tempfile.gettempdir()),
        ]
        resolved = os.path.realpath(req.stl_path)
        if not Path(resolved).is_file():
            event_bus.emit("error",
                            f"EvalAgent: STL path does not exist: {resolved}",
                            {"path": resolved})
            raise HTTPException(404, f"Not found: {req.stl_path}")
        ok = any(
            resolved == pref or resolved.startswith(pref + os.sep)
            for pref in allowed_prefixes
        )
        if ok:
            stl_path = resolved
            size_b = Path(resolved).stat().st_size
            # Reject obviously-degenerate STLs (empty header = ~84 bytes)
            # with a pointed error so the user knows the part blew out.
            if size_b < 500:
                event_bus.emit(
                    "error",
                    f"Exported STL is tiny ({size_b} bytes) — the part "
                    "was probably consumed by a cut that's larger than "
                    "the body. Check flange_bore vs flange_OD and any "
                    "other cut dims.",
                    {"path": resolved, "bytes": size_b})
                raise HTTPException(
                    422,
                    f"Degenerate STL ({size_b}B) — geometry was consumed "
                    "by oversized cut ops. Check your dimensions.")
            event_bus.emit("visual",
                            f"Loaded exported STL for eval: "
                            f"{Path(resolved).name} ({size_b/1024:.0f} KB)",
                            {"path": resolved, "bytes": size_b})
        else:
            event_bus.emit("error",
                            f"EvalAgent: stl_path outside allowed dirs: {resolved}",
                            {"path": resolved,
                             "allowed": allowed_prefixes})
            raise HTTPException(
                403,
                f"stl_path outside allowed dirs (outputs/, ~/aria-exports/, %TEMP%): {resolved}")
    elif req.stl_url:
        # Fetch to a temp file under outputs/cad/stl/eval/
        eval_dir = REPO_ROOT / "outputs" / "cad" / "stl" / "eval"
        eval_dir.mkdir(parents=True, exist_ok=True)
        stl_path = str(eval_dir / f"native_eval_{os.getpid()}_{id(req)}.stl")
        event_bus.emit("visual", f"Fetching exported STL for eval",
                       {"url": req.stl_url[:120]})
        try:
            urllib.request.urlretrieve(req.stl_url, stl_path)
        except Exception as exc:
            event_bus.emit("error", f"STL fetch failed: {exc}")
            raise HTTPException(502, f"Could not fetch STL: {exc}")
    else:
        raise HTTPException(400, "Either stl_url or stl_path required")

    # --- Run verification ---
    try:
        from aria_os.visual_verifier import verify_visual
        from aria_os.spec_extractor import extract_spec
        spec = req.spec or extract_spec(req.goal) or {}
        event_bus.emit("visual",
                       f"Running EvalAgent on host-exported geometry",
                       {"stl": Path(stl_path).name})
        # verify_visual already emits its own events (render, precheck,
        # vision call, result) so the tree fills in live.
        result = verify_visual(step_path="", stl_path=stl_path,
                                goal=req.goal, spec=spec,
                                repo_root=REPO_ROOT)

        # --- DFM auto-gate: run manufacturability checks on the same
        # --- geometry. Issues here feed into the same refiner loop as
        # --- visual-verify issues, so bad parts get corrected before
        # --- we mark the pipeline complete.
        dfm_issues = []
        try:
            from aria_os.agents.dfm_agent import run_dfm_analysis
            # DFM agent takes STEP files ideally, but works on STL if
            # that's all we have. Signature: (step_path, goal="", *, skip_llm)
            event_bus.emit("agent",
                            f"DFMAgent: manufacturability gate on {Path(stl_path).name}")
            dfm_report = run_dfm_analysis(stl_path, goal=req.goal, skip_llm=True)
            dfm_issues = [
                (i.get("message") or str(i))[:120]
                for i in (dfm_report.get("issues") or [])
            ]
            # Filter out spurious "STEP file could not be loaded" criticals —
            # the DFM agent prefers STEP but we only have STL from /api/cad/
            # text-to-part. Treating "no STEP" as a critical fault means
            # every visual-verify call false-fails on geometrically perfect
            # parts. Real DFM criticals (thin walls, undercuts, sharp internal
            # corners) still surface.
            def _is_real_critical(issue: dict) -> bool:
                sev = (issue.get("severity") or "").lower()
                if sev not in ("critical", "high"):
                    return False
                text = " ".join(str(issue.get(k) or "") for k in
                                 ("category", "description", "message",
                                  "suggestion")).lower()
                if ("step file" in text and
                    ("could not be loaded" in text or "cannot" in text
                     or "failed" in text or "missing" in text)):
                    return False
                return True
            n_critical = sum(
                1 for i in (dfm_report.get("issues") or [])
                if _is_real_critical(i))
            if dfm_issues:
                event_bus.emit("warning",
                                f"DFM: {len(dfm_issues)} issue(s) "
                                f"({n_critical} critical)",
                                {"issues": dfm_issues[:5]})
                # Fold critical DFM findings into the result so the
                # refinement decision below sees them as FAIL signal.
                if n_critical > 0:
                    result["verified"] = False
                    existing = result.get("issues") or []
                    result["issues"] = list(existing) + [
                        f"DFM: {i}" for i in dfm_issues[:3]]
            else:
                event_bus.emit("agent", "DFMAgent: PASS — no issues")
        except Exception as _dfm_e:
            event_bus.emit("warning", f"DFM gate skipped: {_dfm_e}")
        verdict = (
            "PASS" if result.get("verified")
            else "FAIL" if result.get("verified") is False
            else "SKIPPED"
        )
        conf = float(result.get("confidence", 0.0))
        issues = result.get("issues", []) or []

        # --- RefinerAgent: if FAIL and we haven't hit iteration cap, ---
        # regenerate a corrected plan and stream it into the panel. The
        # plan goes through the same LLM + validator + retry path as the
        # initial planner — just with visual issues fed in as correction
        # context. Ops stream as native_op events, panel dispatches to
        # bridge. After streaming we return control; the panel will call
        # /api/native_eval again (iteration+1) for another verify pass.
        if verdict == "FAIL" and req.iteration < req.max_iterations:
            event_bus.emit(
                "agent",
                f"RefinerAgent: iteration {req.iteration}/{req.max_iterations} — "
                f"eval FAIL (conf {conf:.0%}), regenerating plan",
                {"iteration": req.iteration, "issues": issues[:3]})
            try:
                from aria_os.native_planner.llm_planner import plan_from_llm
                from aria_os.native_planner.validator import validate_plan
                # Build a goal that carries the FAIL context forward.
                refinement_goal = (
                    f"{req.goal}\n\n"
                    "## PREVIOUS ATTEMPT FAILED — correct these issues\n"
                    + "\n".join(f"  - {i}" for i in issues[:6]) +
                    "\n\nEmit a plan that addresses the issues above. "
                    "If a pattern failed because the source was a full body, "
                    "generate a cut feature FIRST (e.g. one blade slot) and "
                    "pattern THAT, not the whole body."
                )
                refined = plan_from_llm(
                    refinement_goal, req.spec or {},
                    quality=req.quality_tier,
                    repo_root=REPO_ROOT)
                ok, val_issues = validate_plan(refined)
                if not ok:
                    event_bus.emit(
                        "warning",
                        f"RefinerAgent: validation still failing ({val_issues[:2]}), "
                        "stopping refinement loop")
                else:
                    event_bus.emit(
                        "agent",
                        f"RefinerAgent: corrected plan — {len(refined)} ops",
                        {"iteration": req.iteration, "n_ops": len(refined)})
                    for i, op in enumerate(refined):
                        event_bus.emit(
                            "native_op", op.get("label") or op["kind"],
                            {"seq": i + 1, "total": len(refined),
                             "kind": op["kind"], "params": op.get("params", {}),
                             "refinement_pass": req.iteration,
                             "goal": req.goal})
                    # Don't emit "Pipeline complete" here — the panel will
                    # re-trigger /api/native_eval after these ops land,
                    # which gives us the next pass-or-fail decision.
                    return {"verdict": "REFINING",
                             "iteration": req.iteration + 1,
                             "n_ops": len(refined)}
            except Exception as _re:
                event_bus.emit("error",
                                f"RefinerAgent crashed: {_re}",
                                {"iteration": req.iteration})
                # Fall through to final terminal complete below

        # Terminal: PASS, or FAIL with no retries left, or refiner crashed
        event_bus.emit("complete",
                       f"Pipeline complete — EvalAgent: {verdict} "
                       f"(conf {conf:.0%})"
                       + (f" · {req.iteration} refinement(s)"
                           if req.iteration > 1 else ""),
                       {"verdict": verdict,
                        "confidence": conf,
                        "issues": issues[:5],
                        "iterations": req.iteration,
                        "stage": "eval"})
        return {"verdict": verdict,
                 "iteration": req.iteration,
                 "result": _safe_event({"data": result})["data"]}
    except Exception as exc:
        event_bus.emit("error", f"EvalAgent crashed: {exc}")
        raise HTTPException(500, f"Eval failed: {exc}")


# UploadFile for the STT/image/scan endpoints
from fastapi import UploadFile, File  # noqa: E402


@app.post("/api/transcribe", response_model=None)
async def transcribe_alias(audio: UploadFile = File(...)):
    """Alias for /api/stt/transcribe — used by StructSight's voice
    refine. Same body and response."""
    return await stt_transcribe(audio)


@app.post("/api/stt/transcribe", response_model=None)
async def stt_transcribe(audio: UploadFile = File(...)):
    """Transcribe an audio upload via Groq Whisper.

    Returns `{text: "..."}` on success or `{error: "..."}` on failure.
    We use Groq for speed (Whisper large-v3-turbo runs ~0.5s for a 10s
    clip); the API key is read from `GROQ_API_KEY` in the env / .env.
    """
    import os as _os
    # Read the blob into memory (cap at 25MB — Groq's limit)
    data = await audio.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio file too large (>25MB)")
    # Try Groq first (fastest, cheapest)
    try:
        from groq import Groq  # type: ignore
        key = _os.environ.get("GROQ_API_KEY")
        if not key:
            # Try reading from .env
            env_path = REPO_ROOT / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("GROQ_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        if not key:
            raise RuntimeError("GROQ_API_KEY not set")
        client = Groq(api_key=key)
        # Groq expects a tuple (filename, bytes, mime) for the file arg
        resp = client.audio.transcriptions.create(
            file=(audio.filename or "voice.webm", data, audio.content_type or "audio/webm"),
            model="whisper-large-v3-turbo",
            response_format="json",
        )
        text = getattr(resp, "text", "") or ""
        return {"text": text, "backend": "groq"}
    except Exception as exc:
        return {"error": f"STT failed: {exc}", "backend": "groq"}


class ImageToCadRequest(BaseModel):
    prompt: str = ""
    mode: str = "native"         # where the generated part should land
    quality_tier: str = "balanced"


@app.post("/api/image_to_cad")
async def image_to_cad(image: UploadFile = File(...),
                        prompt: str = "",
                        mode: str = "native",
                        quality_tier: str = "balanced"):
    """Image → CAD: analyze the uploaded image with a vision LLM, extract
    dimensions / features, build a goal string, and dispatch through the
    native pipeline so Fusion's feature tree fills in live.

    `prompt` is an optional user hint ("this is a bracket, 50mm wide").
    """
    data = await image.read()
    # Save to a temp path the existing analyzer understands
    import tempfile, os as _os
    suffix = _os.path.splitext(image.filename or "image.png")[1] or ".png"
    tmp_dir = REPO_ROOT / "outputs" / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"img_{_os.getpid()}_{id(image)}{suffix}"
    tmp_path.write_bytes(data)

    event_bus.emit("agent", "Image uploaded — running vision analysis",
                    {"image": image.filename, "bytes": len(data)})

    # Use the existing analyze_image_for_cad helper to extract a goal.
    # Helper signature: analyze_image_for_cad(image_path, hint="", *, repo_root=None) -> str | None
    # Returns a goal string (not a dict). Treat None as no-vision-available.
    try:
        from aria_os.llm_client import analyze_image_for_cad
        derived_goal_raw = analyze_image_for_cad(
            str(tmp_path), hint=prompt or "", repo_root=REPO_ROOT)
        derived_goal = derived_goal_raw or prompt or "imported part"
        event_bus.emit("agent",
                        f"Vision extracted: {derived_goal[:80]}",
                        {"goal": derived_goal})
    except Exception as exc:
        event_bus.emit("error", f"Vision analysis failed: {exc}")
        raise HTTPException(500, f"Vision analysis failed: {exc}")

    # Dispatch through the existing pipeline
    event_bus.emit(
        "step", f"Received goal: {derived_goal[:80]}",
        {"goal": derived_goal, "mode": mode, "quality_tier": quality_tier,
         "source": "image_to_cad"})
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_pipeline,
                          derived_goal, 3, mode, quality_tier)
    return {"status": "started", "goal": derived_goal,
             "image_features": features, "mode": mode}


# --------------------------------------------------------------------------- #
# Sync image/scan → CAD endpoints (per-native-CAD wrappers call these).
#
# The async /api/image_to_cad and /api/scan_to_cad above kick the legacy
# event-bus pipeline (good for the dashboard streaming UI). The sync
# variants block until a STEP/STL is on disk and return its path so a
# CAD plugin can immediately call its own insertGeometry / Open command.
# --------------------------------------------------------------------------- #

class NativeImageRequest(BaseModel):
    """Local-path or base64-image variant of the sync endpoint. The CAD
    plugins call this from inside the host process, so a local path is
    almost always available; base64 is the fallback for sandboxed
    Onshape/WebView2 contexts."""
    file_path:        str | None = None
    file_base64:      str | None = None
    file_name:        str | None = None     # used to pick suffix when base64
    prompt:           str = ""
    quality_tier:     str = "balanced"


# --------------------------------------------------------------------------- #
# Lightweight cancel + event-tail endpoints used by the CAD GUI panels.
# - GET  /api/events/tail?last=50  → backfill the panel's chat log on
#   reconnect (the GUI agent's SSE-reconnect work needs this).
# - POST /api/cancel               → set a soft-cancel flag the pipeline
#   runner polls between ops; ops in flight finish, then the runner
#   bails out and emits a `cancelled` event.
# Both are intentionally minimal — no per-run-id tracking yet because the
# pipeline runner is already single-flight per panel session.
# --------------------------------------------------------------------------- #
import threading as _cancel_threading
_cancel_flag = _cancel_threading.Event()


def _check_cancel_and_raise(stage: str) -> None:
    """Pipeline-runner helpers can call this between ops. Raises a
    plain RuntimeError that the per-mode try/except converts to an
    `error` event with stage info."""
    if _cancel_flag.is_set():
        _cancel_flag.clear()
        raise RuntimeError(f"pipeline cancelled by user at stage {stage!r}")


@app.get("/api/events/tail")
def events_tail(last: int = 50):
    """Return the last N events without touching any subscriber cursor.
    Backs the GUI panels' on-reconnect backfill so a dropped SSE
    connection (e.g. dashboard hot-reload, browser tab sleep) doesn't
    leave the chat log empty when the panel reattaches."""
    n = max(1, min(500, int(last or 50)))
    try:
        return {"ok": True, "events": event_bus.get_history(n), "n": n}
    except Exception as exc:
        raise HTTPException(500, f"events_tail: {exc}")


@app.post("/api/cancel")
def cancel_pipeline():
    """User clicked the cancel button in a CAD panel. Set the soft-cancel
    flag; the pipeline runner picks it up at the next op boundary."""
    _cancel_flag.set()
    event_bus.emit("agent",
                    "Cancel requested — pipeline will stop at next op",
                    {"requested_at": "panel"})
    return {"ok": True, "cancel_set": True}


class LatticeBakeRequest(BaseModel):
    """Body schema for the SW addin's `OpLatticeFeature` regen call.

    The addin POSTs this whenever the user changes one of the SW
    user-parameters that map to a lattice recipe (cell size, wall
    thickness, pattern). Returns the path to the cached/freshly-baked
    STL on disk so the addin can swap the imported Mesh BREP body
    without re-running the rest of the plan.
    """
    pattern: str = "gyroid"
    cell_mm: float = 8.0
    wall_mm: float = 1.0
    bbox: list[float] = [-25, -25, -25, 25, 25, 25]
    resolution: int = 96
    force: bool = False


@app.post("/api/native/lattice/bake")
def native_lattice_bake(req: LatticeBakeRequest):
    """Bake (or look up cached) lattice STL from a recipe.

    Backed by `aria_os.sdf.lattice_op.bake` — keyed on a sha256 of the
    normalised recipe so repeat hits with the same params are O(1).
    Catches any kernel exception and returns a structured 500 the
    addin can display in its activity rail.
    """
    try:
        from aria_os.sdf.lattice_op import bake as _bake
    except Exception as exc:
        raise HTTPException(500,
            f"lattice_op import failed: {type(exc).__name__}: {exc}")
    try:
        stl_path, recipe_used = _bake({
            "pattern":    req.pattern,
            "cell_mm":    req.cell_mm,
            "wall_mm":    req.wall_mm,
            "bbox":       list(req.bbox),
            "resolution": req.resolution,
        }, force=req.force)
    except Exception as exc:
        raise HTTPException(500,
            f"lattice bake failed: {type(exc).__name__}: {exc}")
    return {
        "ok":          True,
        "stl_path":    str(stl_path),
        "size_bytes":  stl_path.stat().st_size if stl_path.is_file() else 0,
        "recipe_used": recipe_used,
        "cached":      not req.force,
    }


@app.post("/api/native/image_to_cad")
def native_image_to_cad(req: NativeImageRequest):
    """Sync: image → STEP path. Blocks until the planner produces a STEP."""
    import base64 as _b64, os as _os, tempfile

    # Resolve to a local path the analyzer + pipeline can read.
    src: Path
    if req.file_path:
        src = Path(req.file_path).resolve()
        if not src.is_file():
            raise HTTPException(400, f"file_path not found: {src}")
    elif req.file_base64:
        suffix = _os.path.splitext(req.file_name or "image.png")[1] or ".png"
        tmp_dir = REPO_ROOT / "outputs" / "uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"img_sync_{_os.getpid()}_{abs(hash(req.file_base64)) & 0xffffff}{suffix}"
        try:
            tmp.write_bytes(_b64.b64decode(req.file_base64))
        except Exception as exc:
            raise HTTPException(400, f"base64 decode failed: {exc}")
        src = tmp
    else:
        raise HTTPException(400, "file_path or file_base64 required")

    # 1. Vision analysis → goal text. analyze_image_for_cad returns a
    # plain goal string (not a dict). Fall back to the user prompt or
    # a generic placeholder when vision is unavailable.
    try:
        from aria_os.llm_client import analyze_image_for_cad
        goal_raw = analyze_image_for_cad(
            str(src), hint=req.prompt or "", repo_root=REPO_ROOT)
    except Exception as exc:
        raise HTTPException(500, f"vision analysis failed: {exc}")
    goal = goal_raw or req.prompt or "imported part"

    # 2. Run the standard text-to-part pipeline IN-PROCESS (mode=native)
    #    and pull the resulting STEP path from the run manifest. We do
    #    NOT use _run_pipeline (async, event-bus) — we want the path back.
    try:
        from aria_os.orchestrator import run as _orch_run
        artifact = _orch_run(goal=goal, max_attempts=2, repo_root=REPO_ROOT)
    except Exception as exc:
        raise HTTPException(500, f"pipeline failed: {exc}")

    step_path = None
    stl_path = None
    if isinstance(artifact, dict):
        step_path = artifact.get("step_path") or artifact.get("step")
        stl_path = artifact.get("stl_path") or artifact.get("stl")
    if not step_path:
        # Fallback: scan latest run dir for part.step
        runs = REPO_ROOT / "outputs" / "runs"
        if runs.is_dir():
            latest = max((d for d in runs.iterdir() if d.is_dir()),
                          key=lambda d: d.stat().st_mtime, default=None)
            if latest and (latest / "part.step").is_file():
                step_path = str(latest / "part.step")
            if latest and (latest / "part.stl").is_file():
                stl_path = str(latest / "part.stl")
    if not step_path:
        raise HTTPException(500, "pipeline returned no STEP path")

    return {
        "ok":         True,
        "goal":       goal,
        "step_path":  step_path,
        "stl_path":   stl_path,
        "features":   features,
        "source":     "native_image_to_cad",
    }


class NativeScanRequest(BaseModel):
    file_path:        str | None = None
    file_base64:      str | None = None
    file_name:        str | None = None
    prompt:           str = ""
    material:         str = "unknown"


@app.post("/api/native/scan_to_cad")
def native_scan_to_cad(req: NativeScanRequest):
    """Sync: STL/PLY/OBJ → STEP path. Cleans the mesh, runs feature
    extraction, returns the cleaned STL plus a STEP if the reconstructor
    could fit primitives."""
    import base64 as _b64, os as _os

    if req.file_path:
        src = Path(req.file_path).resolve()
        if not src.is_file():
            raise HTTPException(400, f"file_path not found: {src}")
    elif req.file_base64:
        suffix = _os.path.splitext(req.file_name or "scan.stl")[1] or ".stl"
        tmp_dir = REPO_ROOT / "outputs" / "uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"scan_sync_{_os.getpid()}_{abs(hash(req.file_base64)) & 0xffffff}{suffix}"
        try:
            tmp.write_bytes(_b64.b64decode(req.file_base64))
        except Exception as exc:
            raise HTTPException(400, f"base64 decode failed: {exc}")
        src = tmp
    else:
        raise HTTPException(400, "file_path or file_base64 required")

    try:
        from aria_os.scan_pipeline import run_scan_pipeline
        entry = run_scan_pipeline(src, material=req.material)
    except Exception as exc:
        raise HTTPException(500, f"scan pipeline failed: {exc}")

    # The scan pipeline produces a cleaned STL; STEP is optional and best-effort.
    # If the CAD plugin gets an STL path, it can import it as a graphics body.
    stl_path = getattr(entry, "stl_path", None) or str(src)
    step_path = getattr(entry, "step_path", None)

    # Fallback: check if STEP was generated alongside STL
    if not step_path:
        try:
            from pathlib import Path as _P
            stl = _P(stl_path)
            cand = stl.with_suffix(".step")
            if cand.is_file():
                step_path = str(cand)
        except Exception:
            pass

    # Final validation: ensure paths exist and are readable
    bbox = None
    if getattr(entry, "bounding_box", None):
        try:
            bb = entry.bounding_box
            bbox = {
                "x": float(bb.x) if hasattr(bb, 'x') else None,
                "y": float(bb.y) if hasattr(bb, 'y') else None,
                "z": float(bb.z) if hasattr(bb, 'z') else None,
            }
        except Exception:
            pass

    return {
        "ok":         True,
        "stl_path":   stl_path if Path(stl_path).is_file() else None,
        "step_path":  step_path if (step_path and Path(step_path).is_file()) else None,
        "topology":   getattr(entry, "topology", None),
        "bbox":       bbox,
        "confidence": float(getattr(entry, "confidence", 0.0)),
        "source":     "native_scan_to_cad",
    }


@app.post("/api/scan_to_cad")
async def scan_to_cad(scan: UploadFile = File(...),
                       prompt: str = "",
                       mode: str = "native",
                       quality_tier: str = "balanced"):
    """Scan → CAD: uploaded STL/PLY/OBJ runs through the scan_pipeline
    (mesh cleanup → feature extraction → topology detection → CadQuery
    reconstruction) and then through the native pipeline to land in the
    host's feature tree."""
    data = await scan.read()
    import os as _os
    suffix = _os.path.splitext(scan.filename or "scan.stl")[1] or ".stl"
    tmp_dir = REPO_ROOT / "outputs" / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"scan_{_os.getpid()}_{id(scan)}{suffix}"
    tmp_path.write_bytes(data)

    event_bus.emit("agent",
                    f"Scan uploaded ({len(data)/1024:.0f} KB) — "
                    "running mesh interpret + feature extraction",
                    {"scan": scan.filename, "bytes": len(data)})

    # Dispatch — the scan_pipeline emits its own per-stage events
    def _run_scan():
        try:
            from aria_os.scan_pipeline import run_scan_pipeline
            result = run_scan_pipeline(tmp_path, repo_root=REPO_ROOT,
                                         user_hint=prompt or "")
            event_bus.emit("complete",
                            f"Pipeline complete — scan reconstructed: "
                            f"{result.get('part_id', 'unknown')}",
                            {"result": str(result)[:300]})
        except Exception as exc:
            event_bus.emit("error", f"Scan pipeline failed: {exc}")

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_scan)
    return {"status": "started", "scan": scan.filename,
             "size_bytes": len(data)}


class ArtifactActionRequest(BaseModel):
    """Post-creation action on an existing artifact — DFM, Quote, CAM,
    FEA, Drawing, or Gerbers. Fires the matching backend pipeline and
    emits events so the panel's feature tree shows progress."""
    action: str                     # drawing | dfm | quote | cam | fea | gerbers
    artifact: dict                  # {filename, path, stl_path, kind, bbox}


@app.post("/api/artifact_action")
async def artifact_action(req: ArtifactActionRequest):
    art = req.artifact or {}
    step_path = art.get("path") or ""
    stl_path  = art.get("stl_path") or ""
    name      = art.get("filename") or "part"

    def _run():
        try:
            if req.action == "drawing":
                event_bus.emit("agent",
                                f"DrawingAgent: generating sheet for {name}")
                try:
                    from aria_os.drawings.mbd_drawings import generate_drawing
                    out_dir = REPO_ROOT / "outputs" / "drawings" / Path(step_path).stem
                    result = generate_drawing(step_path, out_dir=out_dir,
                                                 title=name, part_no=name,
                                                 material=art.get("material", ""))
                    event_bus.emit("complete",
                                    f"Pipeline complete — Drawing: {name}",
                                    {"result": str(result)[:300],
                                     "stage": "drawing",
                                     "out_dir": str(out_dir)})
                except Exception as _de:
                    event_bus.emit("error",
                                    f"Drawing generation failed: {_de}")

            elif req.action == "dfm":
                event_bus.emit("agent",
                                f"DFMAgent: reviewing {name} for manufacturability")
                try:
                    from aria_os.agents.dfm_agent import run_dfm_analysis
                    result = run_dfm_analysis(step_path, repo_root=REPO_ROOT)
                    # Summarise issues for the feature tree row
                    issues = result.get("issues", []) or []
                    status = (
                        "PASS" if not issues else
                        f"{len(issues)} issue(s)")
                    event_bus.emit("complete",
                                    f"Pipeline complete — DFM: {status}",
                                    {"result": str(result)[:600],
                                     "issues": issues[:5],
                                     "stage": "dfm"})
                except Exception as _e:
                    event_bus.emit("error", f"DFM failed: {_e}")

            elif req.action == "quote":
                event_bus.emit("agent",
                                f"QuoteAgent: estimating cost for {name}")
                try:
                    from aria_os.agents.quote_agent import run_quote_cli
                    result = run_quote_cli(step_path,
                                             material=art.get("material",
                                                                "aluminium_6061"),
                                             process="cnc", quantity=1)
                    cost = result.get("unit_cost_usd") or result.get("cost_usd")
                    event_bus.emit("complete",
                                    f"Pipeline complete — Quote: "
                                    f"${cost if cost else '?'} unit, "
                                    f"{result.get('cycle_time_min', '?')}min machining",
                                    {"result": str(result)[:600], "stage": "quote"})
                except Exception as _e:
                    event_bus.emit("error", f"Quote failed: {_e}")

            elif req.action == "cam":
                event_bus.emit("agent",
                                f"CAMAgent: generating toolpaths for {name}")
                try:
                    from aria_os.cam.cam_generator import generate_cam_script
                    script_path = generate_cam_script(step_path,
                                                        material=art.get("material",
                                                                           "aluminium_6061"))
                    event_bus.emit("complete",
                                    f"Pipeline complete — CAM script: "
                                    f"{script_path.name}",
                                    {"script_path": str(script_path),
                                     "stage": "cam"})
                except Exception as _e:
                    event_bus.emit("error", f"CAM failed: {_e}")

            elif req.action == "fea":
                event_bus.emit("agent",
                                f"FEAAgent: running structural analysis on {name}")
                try:
                    from aria_os.fea.calculix_stage import run_static_fea
                    result = run_static_fea(step_path,
                                              material=art.get("material",
                                                                 "aluminium_6061"))
                    max_stress = result.get("max_von_mises_mpa") or \
                                 result.get("max_stress_mpa") or "?"
                    event_bus.emit("complete",
                                    f"Pipeline complete — FEA: "
                                    f"max σ {max_stress} MPa",
                                    {"result": str(result)[:600], "stage": "fea"})
                except Exception as _e:
                    event_bus.emit("error", f"FEA failed: {_e}")

            elif req.action == "gerbers":
                event_bus.emit("agent",
                                f"GerberAgent: plotting fab files for {name}")
                try:
                    from aria_os.ecad.gerber_export import export_gerbers
                    out_zip = export_gerbers(step_path, repo_root=REPO_ROOT)
                    event_bus.emit("complete",
                                    f"Pipeline complete — Gerbers: {out_zip.name}",
                                    {"zip_path": str(out_zip), "stage": "gerbers"})
                except Exception as _e:
                    event_bus.emit("error", f"Gerber export failed: {_e}")

            elif req.action == "bom":
                event_bus.emit("agent",
                                f"BOMAgent: generating bill of materials for {name}")
                try:
                    from aria_os.ecad.bom_export import export_bom
                    out_csv = export_bom(step_path, repo_root=REPO_ROOT)
                    event_bus.emit("complete",
                                    f"Pipeline complete — BOM: {out_csv.name}",
                                    {"csv_path": str(out_csv), "stage": "bom"})
                except Exception as _e:
                    event_bus.emit("error", f"BOM export failed: {_e}")

            elif req.action == "drc":
                event_bus.emit("agent",
                                f"DRCAgent: running design rule check on {name}")
                try:
                    from aria_os.ecad.drc_check import run_drc
                    result = run_drc(step_path)
                    n_viol = len(result.get("violations", []) or [])
                    event_bus.emit("complete",
                                    f"Pipeline complete — DRC: "
                                    f"{'PASS' if n_viol == 0 else f'{n_viol} violations'}",
                                    {"result": str(result)[:600], "stage": "drc"})
                except Exception as _e:
                    event_bus.emit("error", f"DRC failed: {_e}")

            else:
                event_bus.emit("error",
                                f"Unknown artifact action: {req.action}")
        except Exception as exc:
            event_bus.emit("error",
                            f"Artifact action crashed: {exc}",
                            {"action": req.action})

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run)
    return {"status": "started", "action": req.action, "artifact": name}


@app.get("/api/artifacts/download")
async def download_artifact(path: str):
    """Serve a generated artifact (STEP/STL/DXF) by absolute path.

    Path traversal protection: resolves the path and confirms it's inside
    REPO_ROOT/outputs/. Returns 403 otherwise.
    """
    import os
    allowed_prefix = os.path.realpath(str(REPO_ROOT / "outputs"))
    resolved = os.path.realpath(path)
    if not (resolved == allowed_prefix or resolved.startswith(allowed_prefix + os.sep)):
        raise HTTPException(status_code=403,
                            detail="Access denied: path outside outputs directory")
    if not Path(resolved).is_file():
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    # Pick a sensible media-type from the extension
    ext = Path(resolved).suffix.lower()
    media = {
        ".step": "application/step",
        ".stp":  "application/step",
        ".stl":  "model/stl",
        ".dxf":  "application/dxf",
        ".png":  "image/png",
        ".gltf": "model/gltf+json",
        ".glb":  "model/gltf-binary",
    }.get(ext, "application/octet-stream")
    return FileResponse(resolved, media_type=media, filename=Path(resolved).name)


# --------------------------------------------------------------------------
# StructSight viewer endpoints — list runs, fetch a manifest, serve any
# file under outputs/runs/<run_id>/. The manifest endpoint also projects
# the raw run_manifest.json into a {mcad, ecad, dwg, assembly, ar} shape
# the universal viewer in visualize-it/apps/engineering reads directly.
# --------------------------------------------------------------------------

_VIEWER_EXT_TO_KIND = {
    ".stl":  "mcad",
    ".step": "mcad",
    ".stp":  "mcad",
    ".glb":  "ar",
    ".gltf": "ar",
    ".kicad_pcb": "ecad",
    ".pdf":  "dwg",
    ".dxf":  "dwg",
    ".dwg":  "dwg",
    ".sldasm": "assembly",
}


def _scan_run_dir_for_viewer(run_dir: Path) -> dict[str, Any]:
    """Group every file under run_dir by viewer-tab kind. Picks the most
    browser-friendly artifact per tab when multiple exist (e.g. prefer
    .stl over .step for MCAD; prefer .pdf over .dxf for DWG)."""
    if not run_dir.is_dir():
        return {}
    by_kind: dict[str, list[str]] = {
        "mcad": [], "ecad": [], "dwg": [], "assembly": [], "ar": [],
    }
    all_files: list[str] = []
    for p in run_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(run_dir).as_posix()
        all_files.append(rel)
        kind = _VIEWER_EXT_TO_KIND.get(p.suffix.lower())
        if kind:
            by_kind[kind].append(rel)

    def _pick(kind: str, prefer_exts: list[str]) -> str | None:
        files = by_kind[kind]
        for ext in prefer_exts:
            for f in files:
                if f.lower().endswith(ext):
                    return f
        return files[0] if files else None

    return {
        "mcad":     _pick("mcad",     [".stl", ".step", ".stp"]),
        "ecad":     _pick("ecad",     [".kicad_pcb"]),
        "dwg":      _pick("dwg",      [".pdf", ".dxf", ".dwg"]),
        "assembly": _pick("assembly", [".sldasm"]),
        "ar":       _pick("ar",       [".glb", ".gltf"]),
        "all_files": all_files,
    }


@app.get("/api/runs")
async def list_runs(limit: int = 25):
    """List recent run_ids, newest first. Used by the StructSight landing
    page when the user has no run_id in the URL hash."""
    runs_dir = REPO_ROOT / "outputs" / "runs"
    if not runs_dir.is_dir():
        return {"runs": []}
    entries = []
    for d in runs_dir.iterdir():
        if not d.is_dir():
            continue
        manifest_path = d / "run_manifest.json"
        goal = ""
        ts = ""
        if manifest_path.is_file():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                goal = (m.get("goal") or "")[:200]
                ts = m.get("timestamp_utc") or ""
            except Exception:
                pass
        entries.append({
            "run_id": d.name,
            "goal": goal,
            "timestamp_utc": ts,
            "mtime": d.stat().st_mtime,
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return {"runs": entries[: max(1, min(200, limit))]}


@app.get("/api/runs/{run_id}/manifest.json")
async def run_manifest(run_id: str):
    """Return the run's manifest plus a viewer-friendly projection.

    StructSight's universal viewer reads `mcad`, `ecad`, `dwg`,
    `assembly`, `ar` keys at the top level (each is a run-relative
    path). We return both the raw manifest (under `manifest`) and the
    projection at top level so the same response works for the GUI's
    detailed view too.
    """
    if "/" in run_id or ".." in run_id:
        raise HTTPException(400, "Invalid run_id")
    run_dir = REPO_ROOT / "outputs" / "runs" / run_id
    if not run_dir.is_dir():
        raise HTTPException(404, f"Run not found: {run_id}")
    raw: dict[str, Any] = {}
    mp = run_dir / "run_manifest.json"
    if mp.is_file():
        try:
            raw = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, f"Failed to read manifest: {exc}")
    projection = _scan_run_dir_for_viewer(run_dir)
    return {
        "run_id": run_id,
        "goal": raw.get("goal", ""),
        "timestamp_utc": raw.get("timestamp_utc", ""),
        **{k: v for k, v in projection.items() if k != "all_files"},
        "all_files": projection.get("all_files", []),
        "manifest": raw,
    }


@app.get("/api/files/{run_id}/{file_path:path}")
async def serve_run_file(run_id: str, file_path: str):
    """Serve any file under outputs/runs/<run_id>/<file_path>. Used by
    StructSight viewers to fetch STL/PDF/.kicad_pcb/.glb directly.

    Path traversal protected: real-resolves both inputs and confirms the
    file lives inside the run dir before serving.
    """
    import os as _os
    if "/" in run_id or ".." in run_id:
        raise HTTPException(400, "Invalid run_id")
    run_dir = REPO_ROOT / "outputs" / "runs" / run_id
    if not run_dir.is_dir():
        raise HTTPException(404, f"Run not found: {run_id}")
    target = (run_dir / file_path).resolve()
    run_real = run_dir.resolve()
    try:
        target.relative_to(run_real)
    except ValueError:
        raise HTTPException(403, "Path traversal blocked")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {file_path}")
    ext = target.suffix.lower()
    media = {
        ".step": "application/step",
        ".stp":  "application/step",
        ".stl":  "model/stl",
        ".dxf":  "application/dxf",
        ".dwg":  "application/acad",
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".gltf": "model/gltf+json",
        ".glb":  "model/gltf-binary",
        ".kicad_pcb": "application/x-kicad-pcb",
        ".sldasm": "application/octet-stream",
        ".sldprt": "application/octet-stream",
        ".slddrw": "application/octet-stream",
        ".json": "application/json",
        ".md":   "text/markdown",
    }.get(ext, "application/octet-stream")
    return FileResponse(str(target), media_type=media,
                        filename=target.name)


# --------------------------------------------------------------------------
# /api/build/{native|system} — thin aliases the StructSight refine flow
# POSTs to. Wraps the existing native and full-build entry points so the
# universal viewer doesn't need to know which underlying endpoint to call.
# --------------------------------------------------------------------------

class BuildRequest(BaseModel):
    text: str
    run_id: str | None = None
    quality_tier: str = "balanced"


@app.post("/api/build/native")
async def build_native_alias(req: BuildRequest):
    """Wrap /api/cad/text-to-part for StructSight refine. Returns
    {ok, run_id, n_ops} after streaming ops on the event bus."""
    from aria_os.native_planner.llm_planner import plan_from_llm
    spec: dict[str, Any] = {}
    try:
        plan = plan_from_llm(req.text, spec, quality=req.quality_tier,
                             repo_root=REPO_ROOT)
    except Exception as exc:
        raise HTTPException(500, f"plan_from_llm failed: {exc}")
    for i, op in enumerate(plan):
        event_bus.emit(
            "native_op", op.get("label") or op["kind"],
            {"seq": i + 1, "total": len(plan),
             "kind": op["kind"], "params": op.get("params", {}),
             "goal": req.text, "run_id": req.run_id})
    event_bus.emit("complete",
                    f"Pipeline complete for {req.text[:80]}",
                    {"goal": req.text, "mode": "native",
                     "n_ops": len(plan), "run_id": req.run_id})
    return {"ok": True, "run_id": req.run_id, "n_ops": len(plan),
            "mode": "native"}


@app.post("/api/build/system")
async def build_system_alias(req: BuildRequest):
    """Alias to /api/system/full-build for StructSight."""
    import urllib.request as _u
    import urllib.error as _ue
    body = json.dumps({"goal": req.text}).encode("utf-8")
    full_url = f"http://127.0.0.1:8000/api/system/full-build"
    try:
        rq = _u.Request(full_url, data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST")
        with _u.urlopen(rq, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except _ue.HTTPError as he:
        raise HTTPException(he.code, he.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise HTTPException(500, f"build_system_alias: {exc}")


class ExportGltfRequest(BaseModel):
    """Body schema for /api/export_gltf.

    `stl_path` is required; `structsight_json_path` is optional. When the
    JSON is absent the .glb still bakes a neutral grey tint, which keeps the
    VR viewer happy if StructSight hasn't run yet.
    """
    stl_path: str
    structsight_json_path: str | None = None
    out_path: str | None = None


@app.post("/api/export_gltf")
async def export_gltf_endpoint(req: ExportGltfRequest):
    """Convert an STL to a structsight-vr-ready .glb with optional risk
    tinting. Path-traversal protected: every input/output is required to
    resolve inside REPO_ROOT/outputs/.

    Returns ``{glb_path, vertex_count, face_count, tint}``.
    """
    import os
    from aria_os.generators.gltf_export import export_to_gltf

    allowed_prefix = os.path.realpath(str(REPO_ROOT / "outputs"))

    def _check(p: str | None, label: str) -> str | None:
        if p is None or p == "":
            return None
        resolved = os.path.realpath(p)
        if not (resolved == allowed_prefix
                or resolved.startswith(allowed_prefix + os.sep)):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: {label} outside outputs directory",
            )
        return resolved

    stl_resolved = _check(req.stl_path, "stl_path")
    if stl_resolved is None or not Path(stl_resolved).is_file():
        raise HTTPException(
            status_code=404, detail=f"STL not found: {req.stl_path}"
        )
    ssj_resolved = _check(req.structsight_json_path, "structsight_json_path")
    if ssj_resolved is not None and not Path(ssj_resolved).is_file():
        raise HTTPException(
            status_code=404,
            detail=f"StructSight JSON not found: {req.structsight_json_path}",
        )

    # Out path: default to <stl_dir>/part.glb if not provided. Either way we
    # confirm it lands in outputs/.
    out_path = req.out_path
    if not out_path:
        out_path = str(Path(stl_resolved).with_name("part.glb"))
    out_resolved = os.path.realpath(out_path)
    if not (out_resolved == allowed_prefix
            or out_resolved.startswith(allowed_prefix + os.sep)):
        raise HTTPException(
            status_code=403,
            detail="Access denied: out_path outside outputs directory",
        )

    try:
        result = export_to_gltf(
            stl_path=stl_resolved,
            structsight_json=ssj_resolved,
            out_path=out_resolved,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"glTF export failed: {e}"
        )
    return result


@app.get("/api/parts/{part_id}/stl")
async def get_stl(part_id: str):
    """Serve the STL file for a part."""
    import os
    # Search outputs/cad/stl/ for a matching file
    stl_dir = REPO_ROOT / "outputs" / "cad" / "stl"
    matches = list(stl_dir.glob(f"*{part_id}*.stl")) if stl_dir.exists() else []
    if not matches:
        raise HTTPException(status_code=404, detail=f"No STL found for {part_id}")
    # Return the most recently modified match
    stl_file = max(matches, key=lambda p: p.stat().st_mtime)
    # Path traversal protection: ensure resolved path stays within outputs/
    allowed_prefix = os.path.realpath(str(REPO_ROOT / "outputs"))
    resolved = os.path.realpath(str(stl_file))
    if not resolved.startswith(allowed_prefix + os.sep) and resolved != allowed_prefix:
        raise HTTPException(status_code=403, detail="Access denied: path outside outputs directory")
    return FileResponse(resolved, media_type="model/stl", filename=stl_file.name)


@app.get("/api/sessions")
async def list_sessions():
    """List session log files."""
    sessions_dir = REPO_ROOT / "sessions"
    if not sessions_dir.exists():
        return {"sessions": []}
    files = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "sessions": [
            {"name": f.name, "size_bytes": f.stat().st_size}
            for f in files[:50]
        ]
    }


@app.get("/api/cem")
async def cem_summary():
    """Return the latest CEM results from the learning log."""
    log_path = REPO_ROOT / "outputs" / "cad" / "learning_log.json"
    if not log_path.exists():
        return {"cem": []}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {"cem": []}
        # Return entries that have cem data, most recent first
        cem_entries = [
            {
                "part_id":    e.get("part_id"),
                "goal":       e.get("goal", ""),
                "cem_passed": e.get("cem_passed"),
                "timestamp":  e.get("timestamp", ""),
            }
            for e in reversed(data)
            if "cem_passed" in e
        ]
        return {"cem": cem_entries[:100]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------------------------------------------------------------------------- #
# W9: aria-vr live-sync + voice-plan endpoints
# --------------------------------------------------------------------------- #
# A connected aria-vr headset opens a WebSocket to /ws/model_updates and
# subscribes to "new_model" / "new_measurements" events. When a pipeline
# run lands a fresh .glb under outputs/, the dashboard broadcasts the URL.
# The headset auto-reloads the scene with the new model.
#
# /api/voice_plan accepts a multipart audio upload + (optional) host
# context, transcribes via the existing speech_to_text path, hands the
# transcription to the planner, and returns the resulting plan + the URL
# of the freshly exported model so the headset can chain to model-sync.

from fastapi import WebSocket, WebSocketDisconnect, UploadFile, File, Form

# In-memory subscriber set. WebSocket clients register here; the helper
# `broadcast_model_update(url)` fans out events. Survival of process
# restart isn't a concern — clients reconnect.
_VR_WS_CLIENTS: set["WebSocket"] = set()
_VR_WS_LOCK = asyncio.Lock()


async def broadcast_model_update(model_url: str,
                                    *, run_id: str | None = None,
                                    extras: dict | None = None) -> int:
    """Fan out a 'new_model' event to every connected aria-vr client.
    Returns the number of subscribers that received it. Failed sockets
    are silently dropped from the set."""
    payload = {
        "event": "new_model",
        "url":   model_url,
        "run_id": run_id,
    }
    if extras:
        payload.update(extras)
    msg = json.dumps(payload, default=str)
    dead: list[WebSocket] = []
    async with _VR_WS_LOCK:
        clients = list(_VR_WS_CLIENTS)
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    if dead:
        async with _VR_WS_LOCK:
            for ws in dead:
                _VR_WS_CLIENTS.discard(ws)
    return len(clients) - len(dead)


@app.websocket("/ws/model_updates")
async def vr_model_updates(ws: WebSocket):
    """aria-vr connects here and listens for new_model events. Client
    can send {'event': 'ping'} every 30s to keep the connection alive
    behind aggressive proxies."""
    await ws.accept()
    async with _VR_WS_LOCK:
        _VR_WS_CLIENTS.add(ws)
    # Send a hello so the client knows the channel is live + can sync
    # its UI state ("connected to ARIA").
    try:
        await ws.send_text(json.dumps({
            "event":      "hello",
            "subscribers": len(_VR_WS_CLIENTS),
        }))
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except Exception:
                continue
            # Echo pings; ignore everything else.
            if msg.get("event") == "ping":
                await ws.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with _VR_WS_LOCK:
            _VR_WS_CLIENTS.discard(ws)


@app.post("/api/voice_plan")
async def voice_plan(
    audio: UploadFile = File(...),
    host_context_json: str | None = Form(None),
    quality: str = Form("balanced"),
):
    """Accept a recorded utterance from the aria-vr headset, transcribe
    it, run it through the planner with optional host_context (the
    selected feature in the headset's measurement tool, if any), and
    return both the plan + a URL for the freshly exported model so the
    headset can chain to model-sync.

    The audio file is whatever MediaRecorder captured on Quest — usually
    audio/webm;codecs=opus. We pass through to the existing
    aria_os.speech_to_text helper which already understands the chain.
    """
    import tempfile

    # 1. Stage the upload to a temp WAV/WEBM the existing transcribe
    # helper can read. The helper handles webm → wav internally.
    suffix = Path(audio.filename or "voice.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        wav_path = Path(tmp.name)

    try:
        from aria_os.speech_to_text import transcribe
    except ImportError:
        raise HTTPException(
            status_code=503, detail="speech_to_text module unavailable")
    text = transcribe(wav_path)
    if not text:
        raise HTTPException(
            status_code=502,
            detail="Transcription failed (no STT backend available)")

    # 2. Parse optional host_context (selection, user_parameters, etc.)
    host_context: dict | None = None
    if host_context_json:
        try:
            host_context = json.loads(host_context_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"host_context_json malformed: {exc}")

    # 3. Run through voice-in-context for the demonstrative resolution
    # path (handles "make this hole 2mm bigger" with hover context).
    try:
        from aria_os.agents.voice_in_context import (
            _resolve_target, _classify_intent,
            _build_goal_with_target,
        )
    except ImportError:
        raise HTTPException(
            status_code=503, detail="voice_in_context module unavailable")
    target = _resolve_target(text, host_context or {})
    intent = _classify_intent(text)
    resolved_goal = _build_goal_with_target(text, target)
    mode = "modify" if intent == "modify" else (
        "extend" if intent == "extend" else "new")

    # 4. Plan
    try:
        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan(
            resolved_goal, {},
            prefer_llm=True, quality=quality,
            repo_root=REPO_ROOT, host_context=host_context, mode=mode)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Planner failed: {exc}")

    # 5. Try to find the most recent run output to broadcast — the
    # planner doesn't itself execute geometry yet (executor lives in
    # the host bridge), so we hand the headset the plan and any
    # already-existing model URL the user is iterating on.
    runs_dir = REPO_ROOT / "outputs" / "runs"
    latest_glb_url: str | None = None
    latest_run_id: str | None = None
    if runs_dir.is_dir():
        try:
            runs = sorted(
                (p for p in runs_dir.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime, reverse=True)
            for r in runs[:5]:
                glb = r / "part.glb"
                if glb.is_file():
                    latest_run_id = r.name
                    latest_glb_url = f"/outputs/runs/{r.name}/part.glb"
                    break
        except Exception:
            pass

    # 6. Notify VR clients so the headset can chain to model-sync
    if latest_glb_url:
        await broadcast_model_update(
            latest_glb_url, run_id=latest_run_id,
            extras={"trigger": "voice_plan", "goal": resolved_goal})

    return {
        "transcription":   text,
        "intent":          intent,
        "resolved_target": target,
        "goal":            resolved_goal,
        "mode":            mode,
        "plan":            plan,
        "model_url":       latest_glb_url,
        "run_id":          latest_run_id,
    }


@app.post("/api/measurements/save")
async def save_measurements(payload: dict):
    """aria-vr posts the user's measurement annotations here so they
    persist alongside the run artifacts. Payload shape:
        {run_id, model_url, measurements: [{kind, points, value, label}]}
    """
    run_id = (payload or {}).get("run_id") or "untracked"
    out_dir = REPO_ROOT / "outputs" / "vr" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    f = out_dir / "measurements.json"
    f.write_text(json.dumps(payload, indent=2, default=str),
                  encoding="utf-8")
    return {"ok": True, "saved_to": str(f.relative_to(REPO_ROOT))}


# --------------------------------------------------------------------------- #
# W10: feedback capture (knowledge loop input)
# --------------------------------------------------------------------------- #

class FeedbackRequest(BaseModel):
    run_id: str
    goal: str
    plan: list[dict]
    decision: str   # accept | reject | needs_revision
    reason: str = ""
    spec: dict = {}
    failed_op_index: int | None = None
    user_id: str | None = None
    host: str = "dashboard"
    extras: dict = {}


@app.post("/api/feedback")
async def submit_feedback(req: FeedbackRequest):
    """Capture user accept/reject for a generated plan. Powers the
    W10 auto-promote (W10.2), failure-mining (W10.3), and SFT export
    (W10.4) pipelines."""
    if req.decision not in ("accept", "reject", "needs_revision"):
        raise HTTPException(
            status_code=422,
            detail="decision must be accept|reject|needs_revision")
    try:
        from aria_os.feedback import FeedbackEntry, record_feedback
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"feedback module unavailable: {exc}")
    entry = FeedbackEntry(
        run_id=req.run_id, goal=req.goal, plan=req.plan,
        decision=req.decision, reason=req.reason, spec=req.spec,
        failed_op_index=req.failed_op_index, user_id=req.user_id,
        host=req.host, extras=req.extras or {})
    path = record_feedback(entry, repo_root=REPO_ROOT)
    return {
        "ok": True,
        "saved_to": str(path.relative_to(REPO_ROOT)),
        "plan_hash": entry.plan_hash,
    }


@app.get("/api/feedback/stats")
async def feedback_stats():
    """Aggregate feedback stats — used by the insights dashboard."""
    try:
        from aria_os.feedback import stats as _stats
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"feedback module unavailable: {exc}")
    return _stats(repo_root=REPO_ROOT)


# --------------------------------------------------------------------------- #
# W10.6: insights data endpoints (powers dashboard/insights.html)
# --------------------------------------------------------------------------- #

@app.get("/insights")
async def insights_view():
    """Serve the static insights dashboard."""
    p = Path(__file__).resolve().parent / "insights.html"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="insights.html missing")
    return FileResponse(str(p), media_type="text/html")


@app.get("/viewer")
@app.get("/viewer.html")
async def standalone_viewer():
    """Serve the single-file three.js viewer (dashboard/static/viewer.html).
    Loads STL/GLB/StructSight-JSON via drag-drop or ?stl=, ?glb=, ?ss=
    query params. Used as a headset-less inspection surface for ARIA
    pipeline output."""
    p = Path(__file__).resolve().parent / "static" / "viewer.html"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="viewer.html missing")
    return FileResponse(str(p), media_type="text/html")


@app.get("/static/structsight_stub.json")
async def structsight_stub():
    """Sample StructSight overlay for testing /viewer."""
    p = Path(__file__).resolve().parent / "static" / "structsight_stub.json"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="stub missing")
    return FileResponse(str(p), media_type="application/json")


@app.get("/api/insights/eval_history")
async def insights_eval_history(limit: int = 20):
    """Return the last N eval runs from outputs/eval/<ts>/results.json
    for the insights dashboard's pass-rate chart."""
    base = REPO_ROOT / "outputs" / "eval"
    if not base.is_dir():
        return []
    runs = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        results = d / "results.json"
        if not results.is_file():
            continue
        try:
            data = json.loads(results.read_text(encoding="utf-8"))
            runs.append({
                "timestamp_utc": data.get("timestamp_utc",
                                              d.name),
                "n_prompts":     data.get("n_prompts", 0),
                "counts":        data.get("counts", {}),
                "pass_rate":     data.get("pass_rate", 0),
                "run_dir":       d.name,
            })
        except Exception:
            continue
        if len(runs) >= limit:
            break
    return runs


@app.get("/api/insights/fewshots")
async def insights_fewshots():
    """Count the curated vs auto-promoted few-shots."""
    fs_dir = REPO_ROOT / "aria_os" / "native_planner" / "fewshots"
    if not fs_dir.is_dir():
        return {"curated_count": 0, "auto_count": 0}
    files = list(fs_dir.glob("*.json"))
    curated = sum(1 for f in files if not f.name.startswith("auto_"))
    auto = sum(1 for f in files if f.name.startswith("auto_"))
    return {"curated_count": curated, "auto_count": auto,
            "all_files": [f.name for f in files]}


@app.get("/api/insights/ab_latest")
async def insights_ab_latest():
    """Most recent A/B comparison.json from outputs/eval/ab/<ts>/."""
    base = REPO_ROOT / "outputs" / "eval" / "ab"
    if not base.is_dir():
        return {}
    runs = sorted((d for d in base.iterdir() if d.is_dir()),
                    reverse=True)
    for d in runs:
        comparison = d / "comparison.json"
        if comparison.is_file():
            try:
                return json.loads(
                    comparison.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


# --------------------------------------------------------------------------- #
# Phase F — single-page Console GUI (dashboard/gui/index.html)
#
# Mounts at GET / (root) and GET /gui. Buttons in the page POST to
# /api/gui/* — each handler shells out to one of the scripts that
# already exist in scripts/ so the GUI is a thin coordinator over the
# CLI tools the user has been driving by hand.
#
# Endpoints:
#   GET  /                       -> redirect to /gui
#   GET  /gui                    -> dashboard/gui/index.html
#   GET  /api/gui/addin_status   -> proxy to localhost:7501/status
#   POST /api/gui/redeploy_addin -> shell scripts/sw_redeploy.py
#   POST /api/gui/run_smoke_driver
#   POST /api/gui/enrich_drawing -> POST localhost:7501/op enrichDrawing
#   GET  /api/gui/bundles        -> list outputs/system_builds/*
#   GET  /api/gui/render_step    -> render a bundle's STEP to PNG and
#                                    return as image/png
#   POST /api/gui/run_phase_e    -> shell scripts/phase_e_varied_prompts.py
#   GET  /api/gui/git_status     -> git status -s
#   POST /api/gui/git_push       -> git push current branch + return PR URL
# --------------------------------------------------------------------------- #
import sys as _gui_sys  # noqa: E402
import subprocess as _gui_subprocess  # noqa: E402
import urllib.request as _gui_urlreq  # noqa: E402
import urllib.error   as _gui_urlerr  # noqa: E402
from fastapi.responses import RedirectResponse, Response  # noqa: E402

_GUI_DIR = Path(__file__).resolve().parent / "gui"
_PYTHON_EXE = _os.environ.get("ARIA_PYTHON",
                                "C:/Python313/python.exe"
                                if _os.path.isfile("C:/Python313/python.exe")
                                else _gui_sys.executable)


def _shell(cmd: list[str], timeout: float = 1200.0) -> dict:
    """Run cmd, return {ok, stdout, stderr, returncode}. Truncated to
    avoid blowing the GUI console with multi-MB outputs."""
    try:
        r = _gui_subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=str(REPO_ROOT))
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": (r.stdout or "")[-12000:],
            "stderr": (r.stderr or "")[-4000:],
        }
    except _gui_subprocess.TimeoutExpired as exc:
        return {"ok": False, "returncode": -1,
                "stdout": (exc.stdout or "")[-8000:] if exc.stdout else "",
                "stderr": f"timeout after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "returncode": -1,
                "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


@app.get("/")
async def gui_root_redirect():
    return RedirectResponse(url="/gui", status_code=307)


@app.get("/gui")
async def gui_root():
    p = _GUI_DIR / "index.html"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="gui/index.html missing")
    return FileResponse(str(p), media_type="text/html")


@app.get("/api/gui/addin_status")
async def gui_addin_status():
    """Proxy to the SW addin's /status. Returns sw_connected:false on any
    error so the UI can render a 'down' pill without fetch-level errors."""
    try:
        with _gui_urlreq.urlopen("http://localhost:7501/status",
                                   timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (_gui_urlerr.URLError, TimeoutError, ConnectionError) as exc:
        return {"ok": False, "sw_connected": False,
                "error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/gui/redeploy_addin")
async def gui_redeploy_addin():
    return _shell(
        [_PYTHON_EXE, str(REPO_ROOT / "scripts" / "sw_redeploy.py")],
        timeout=600.0)


@app.post("/api/gui/run_smoke_driver")
async def gui_run_smoke_driver(payload: dict):
    bundle = payload.get("bundle_name") or "drone_ukraine_v19"
    bundle_dir = REPO_ROOT / "outputs" / "system_builds" / bundle
    if not bundle_dir.is_dir():
        return {"ok": False, "stderr": f"bundle dir not found: {bundle_dir}"}
    return _shell(
        [_PYTHON_EXE, str(REPO_ROOT / "scripts" / "sw_assemble_drone.py"),
         "--bundle", str(bundle_dir)],
        timeout=900.0)


@app.post("/api/gui/enrich_drawing")
async def gui_enrich_drawing():
    """POST /op enrichDrawing to the addin — assumes a drawing is the
    active doc in SW (typically right after createDrawing)."""
    body = json.dumps({"kind": "enrichDrawing", "params": {
        "gdt": True, "section_view": True, "exploded_view": True,
    }}).encode("utf-8")
    req = _gui_urlreq.Request(
        "http://localhost:7501/op", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _gui_urlreq.urlopen(req, timeout=300.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502,
                              detail=f"addin unreachable: {exc}")


@app.get("/api/gui/bundles")
async def gui_list_bundles():
    base = REPO_ROOT / "outputs" / "system_builds"
    if not base.is_dir():
        return {"bundles": []}
    bundles = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            artifacts = [p.name for p in d.iterdir()
                          if p.is_file() and not p.name.startswith("~$")]
            bundles.append({
                "name":           d.name,
                "artifact_count": len(artifacts),
                "has_step":       any(a.lower().endswith(".step") for a in artifacts),
                "has_sldasm":     any(a.lower().endswith(".sldasm") for a in artifacts),
                "has_slddrw":     any(a.lower().endswith(".slddrw") for a in artifacts),
                "has_pcb":        any(a.endswith(".kicad_pcb") for a in artifacts),
            })
        except OSError:
            continue
    return {"bundles": bundles[:40]}  # cap so the UI doesn't render thousands


@app.get("/api/gui/render_step")
async def gui_render_step(bundle: str):
    """Render <bundle>/assembly.step (or first .step) to PNG and return
    as image/png. Caches into bundle/_verify/gui_render.png."""
    bundle_dir = REPO_ROOT / "outputs" / "system_builds" / bundle
    if not bundle_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"bundle: {bundle}")
    candidates = [
        bundle_dir / "assembly_mated.step",
        bundle_dir / "assembly.step",
    ]
    candidates += sorted(bundle_dir.glob("*.step"))
    step_path = next((p for p in candidates if p.is_file()), None)
    if step_path is None:
        raise HTTPException(status_code=404,
                              detail=f"no .step in {bundle_dir}")
    out_dir = bundle_dir / "_verify"
    out_dir.mkdir(exist_ok=True)
    out_png = out_dir / "gui_render.png"

    # Render via the existing visual-verifier wireframe path (matplotlib,
    # no GL needed). Best-effort — surface error to UI if anything fails.
    try:
        import cadquery as cq
        import trimesh
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
        import tempfile
        shp = cq.importers.importStep(str(step_path))
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as t:
            stl_path = t.name
        cq.exporters.export(shp, stl_path, "STL")
        m = trimesh.load(stl_path)
        fig = plt.figure(figsize=(8, 8))
        ax = fig.add_subplot(111, projection="3d")
        verts = m.vertices
        ax.plot_trisurf(verts[:, 0], verts[:, 1], verts[:, 2],
                          triangles=m.faces, alpha=0.65,
                          edgecolor="steelblue", linewidth=0.05)
        ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)"); ax.set_zlabel("Z (mm)")
        ax.set_title(f"{bundle} / {step_path.name}")
        ax.view_init(elev=22, azim=42)
        plt.tight_layout()
        plt.savefig(str(out_png), dpi=120, bbox_inches="tight")
        plt.close(fig)
    except Exception as exc:
        raise HTTPException(status_code=500,
                              detail=f"render failed: {exc}")

    return Response(content=out_png.read_bytes(),
                      media_type="image/png",
                      headers={"Cache-Control": "no-store"})


@app.post("/api/gui/run_phase_e")
async def gui_run_phase_e():
    return _shell(
        [_PYTHON_EXE, str(REPO_ROOT / "scripts" / "phase_e_varied_prompts.py")],
        timeout=1800.0)


@app.get("/api/gui/git_status")
async def gui_git_status():
    s = _shell(["git", "status", "-s"], timeout=15.0)
    branch = _shell(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                      timeout=10.0)
    ahead = _shell(["git", "rev-list", "--count", "@{u}..HEAD"],
                      timeout=10.0)
    return {
        "ok":      s["ok"],
        "branch":  (branch.get("stdout") or "").strip(),
        "ahead":   (ahead.get("stdout") or "0").strip(),
        "summary": f"branch={(branch.get('stdout') or '').strip()} "
                    f"ahead={(ahead.get('stdout') or '0').strip()} "
                    f"dirty={len((s.get('stdout') or '').splitlines())}",
        "detail":  s.get("stdout") or "",
    }


@app.post("/api/gui/git_push")
async def gui_git_push():
    branch = _shell(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                      timeout=10.0)
    br = (branch.get("stdout") or "").strip()
    if not br:
        return {"ok": False, "stderr": "could not resolve current branch"}
    if br in ("main", "master"):
        return {"ok": False,
                "stderr": (f"refusing to push to default branch '{br}' — "
                           "create a feature branch first")}
    r = _shell(["git", "push", "origin", br], timeout=180.0)
    pr_url = None
    out = (r.get("stdout") or "") + (r.get("stderr") or "")
    for line in out.splitlines():
        if "https://github.com/" in line and "/pull/new/" in line:
            pr_url = line.strip().split()[-1]
            break
    r["pr_url"] = pr_url
    return r


# --------------------------------------------------------------------------- #
# OpenClaw / VR-projection endpoints
#
# StructSight VR polls /api/openclaw/projection/<machine_id> while the
# user is looking at a machine fiducial. The manifest tells the renderer
# WHERE the build plate is, WHICH .glb to drop on it, and HOW MUCH of
# the part is finished so layers can fade in as the print progresses.
# --------------------------------------------------------------------------- #

@app.get("/api/openclaw/projection/{machine_id}")
async def openclaw_projection(machine_id: str):
    """Return the VR-projection manifest for the named machine.

    Cheap to call (~2ms when bridge is local). VR client polls this at
    1-2Hz; on no calibration / no active job, returns a "skeleton" so
    the client can still draw a calibration prompt or idle state.
    """
    try:
        from aria_os.openclaw import projection as _proj
        manifest = _proj.build(machine_id)
        return _proj.to_dict(manifest)
    except Exception as exc:
        raise HTTPException(500, f"openclaw_projection: {exc}")


@app.get("/api/openclaw/calibrations")
async def openclaw_calibrations_list():
    try:
        from aria_os.openclaw import machine_calibration as _cal
        from dataclasses import asdict
        return {"ok": True,
                "calibrations": [asdict(c) for c in _cal.list_all()]}
    except Exception as exc:
        raise HTTPException(500, f"openclaw_calibrations_list: {exc}")


class OpenClawCalibrationUpsert(BaseModel):
    machine_id: str
    fiducial_id: str
    fiducial_pose_in_machine: list = [0, 0, 0, 0, 0, 0, 1]
    build_plate_origin_offset_mm: tuple = (0.0, 0.0, 0.0)
    build_volume_mm: tuple = (220.0, 220.0, 250.0)
    build_plate_quat: list = [0, 0, 0, 1]
    notes: str = ""


@app.post("/api/openclaw/calibrations")
async def openclaw_calibrations_upsert(req: OpenClawCalibrationUpsert):
    """Upsert one machine calibration. Called by StructSight VR after the
    user completes the in-headset calibration ritual (look at AprilTag,
    confirm pose).
    """
    try:
        from aria_os.openclaw import machine_calibration as _cal
        cal = _cal.MachineCalibration(
            machine_id=req.machine_id,
            fiducial_id=req.fiducial_id,
            fiducial_pose_in_machine=list(req.fiducial_pose_in_machine),
            build_plate_origin_offset_mm=tuple(req.build_plate_origin_offset_mm),
            build_volume_mm=tuple(req.build_volume_mm),
            build_plate_quat=list(req.build_plate_quat),
            notes=req.notes,
        )
        _cal.upsert(cal)
        return {"ok": True, "machine_id": req.machine_id}
    except Exception as exc:
        raise HTTPException(500, f"openclaw_calibrations_upsert: {exc}")


# --------------------------------------------------------------------------- #
# CAD feature learning ledger - what the feature-matrix runs have discovered.
# Frontend uses this to render the live feature support matrix on the
# StructSight Jarvis panel. The ledger updates every time
# scripts/run_sw_feature_matrix.py finishes.
# --------------------------------------------------------------------------- #


def _load_cad_ledger(cad: str) -> dict:
    name = ("sw_learning_ledger.json" if cad == "sw"
            else f"{cad}_learning_ledger.json")
    p = REPO_ROOT / "outputs" / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.get("/api/cad/feature-ledger")
async def cad_feature_ledger(cad: str = "sw"):
    """Per-CAD feature support ledger."""
    led = _load_cad_ledger(cad)
    if not led:
        return {"ok": False, "cad": cad, "ledger": {},
                "msg": "no ledger - run scripts/run_sw_feature_matrix.py"}
    counts: dict[str, int] = {}
    for e in led.values():
        counts[e.get("status", "unknown")] = (
            counts.get(e.get("status", "unknown"), 0) + 1)
    return {"ok": True, "cad": cad, "total": len(led),
            "by_status": counts, "ledger": led}


@app.get("/api/cad/feature-ledger/all")
async def cad_feature_ledger_all():
    """Cross-CAD support matrix: which features each CAD bridge supports."""
    cads = ["sw", "rhino", "fusion", "onshape", "autocad"]
    ledgers = {c: _load_cad_ledger(c) for c in cads}
    all_feats: set[str] = set()
    for led in ledgers.values():
        all_feats.update(led.keys())
    matrix: dict = {}
    for f in sorted(all_feats):
        matrix[f] = {c: (ledgers[c].get(f, {}).get("status", "untested")
                        if ledgers[c] else "untested") for c in cads}
    return {"ok": True, "cads": cads,
            "feature_count": len(all_feats), "matrix": matrix}


@app.get("/api/cad/feature-matrix-report")
async def cad_feature_matrix_report():
    """Latest matrix run report (markdown + json rows + contact sheet path)."""
    base = REPO_ROOT / "outputs" / "feature_matrix"
    md = base / "report.md"
    js = base / "report.json"
    cs = base / "contact_sheet.png"
    return {
        "ok": md.exists() or js.exists(),
        "report_md_path": str(md) if md.exists() else None,
        "report_json_path": str(js) if js.exists() else None,
        "contact_sheet_path": str(cs) if cs.exists() else None,
        "view_dirs": [str(p) for p in base.glob("*_views") if p.is_dir()],
    }


# --------------------------------------------------------------------------- #
# Dev entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.aria_server:app", host="0.0.0.0", port=8000, reload=True)
