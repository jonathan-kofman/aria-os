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
# its own well-known port. Adding fusion/onshape later means another
# entry here + listener in that plugin.
_CAD_BASE_URL = {
    "solidworks": "http://localhost:7501",
    "sw":         "http://localhost:7501",
    "rhino":      "http://localhost:7502",
}


@app.post("/api/cad/text-to-part")
def text_to_part(req: TextToPartRequest):
    """End-to-end: goal text → planner ops → target CAD via HTTP."""
    import httpx as _httpx

    cad = (req.cad or "solidworks").lower().strip()
    base = _CAD_BASE_URL.get(cad)
    if not base:
        raise HTTPException(400, f"unknown cad: {cad!r}")

    # Confirm the listener is up — fail fast with a useful error rather
    # than dispatching ops into a void.
    try:
        with _httpx.Client(timeout=5.0) as c:
            status = c.get(f"{base}/status").json()
            if not status.get("ok"):
                raise HTTPException(502, f"{cad} listener not ready: {status}")
    except Exception as exc:
        raise HTTPException(502,
            f"{cad} listener at {base} unreachable: {type(exc).__name__}: {exc}")

    # Plan via the LLM planner — same path /api/generate uses.
    from aria_os.native_planner.llm_planner import plan_from_llm
    try:
        spec = {}  # spec extraction would be nice; planner can survive empty
        ops = plan_from_llm(req.goal, spec,
                             quality=req.quality_tier,
                             repo_root=REPO_ROOT)
    except Exception as exc:
        raise HTTPException(500,
            f"planner failed: {type(exc).__name__}: {exc}")
    if not ops:
        return {"ok": False, "error": "planner returned no ops",
                 "cad": cad, "goal": req.goal}

    # Dispatch each op to the target CAD listener. Stop at first failure
    # — the cache + LLM-args layer in the addin handles arg-combo retries
    # internally; an op-level failure here means something genuinely
    # outside the autonomous-recovery scope.
    results = []
    failed_at = None
    with _httpx.Client(timeout=120.0) as c:
        for i, op in enumerate(ops):
            payload = {"kind": op.get("kind"), "params": op.get("params", {})}
            try:
                r = c.post(f"{base}/op", json=payload)
                r_json = r.json()
                results.append({
                    "i": i, "kind": payload["kind"],
                    "label": op.get("label"),
                    "ok": (r_json.get("result") or {}).get("ok",
                                                            r_json.get("ok")),
                    "result": r_json.get("result"),
                    "error": r_json.get("error"),
                })
                if not results[-1]["ok"]:
                    failed_at = i
                    break
            except Exception as exc:
                results.append({
                    "i": i, "kind": payload["kind"], "ok": False,
                    "error": f"transport: {type(exc).__name__}: {exc}",
                })
                failed_at = i
                break

    n_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": failed_at is None,
        "cad": cad,
        "goal": req.goal,
        "n_ops_planned": len(ops),
        "n_ops_dispatched": len(results),
        "n_ops_succeeded": n_ok,
        "failed_at": failed_at,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Pipeline runner (runs in background thread to keep FastAPI responsive)
# --------------------------------------------------------------------------- #

def _auto_detect_mode(goal: str) -> str:
    """Pick a pipeline mode by scanning the goal text using word-boundary
    regex matches so we don't get substring false positives (e.g.
    'mate' inside 'material' → assembly, 'pcb' inside 'upcbomb', etc.).

    Routing (first hit wins):
      - PCB / KiCad / gerber / schematic     → kicad
      - drawing / dimensions / GD&T / sheet  → dwg
      - assembly / mate / joint / mount X to → asm
      - otherwise                             → native (mechanical Part)
    """
    import re as _re
    g = (goal or "").lower()

    def _any_word(patterns: tuple) -> bool:
        """True if any pattern appears as a word or word-bounded phrase."""
        for p in patterns:
            if _re.search(rf"\b{_re.escape(p)}\b", g):
                return True
        return False

    # Sheet metal FIRST — otherwise "sheet" keywords collide with DWG.
    if _any_word(("sheet metal", "sheet-metal", "bracket with bend",
                   "bent plate", "formed sheet", "enclosure with bends",
                   "bends", "bend radius", "flanged enclosure",
                   "folded plate", "formed bracket")):
        return "sheetmetal"
    if _any_word(("pcb", "kicad", "gerber", "schematic",
                   "circuit board", "footprint", "netlist")):
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
            n_critical = sum(
                1 for i in (dfm_report.get("issues") or [])
                if (i.get("severity") or "").lower() in ("critical", "high"))
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

    # Use the existing analyze_image_for_cad helper to extract a goal
    try:
        from aria_os.llm_client import analyze_image_for_cad
        features = analyze_image_for_cad(
            str(tmp_path), user_prompt=prompt, repo_root=REPO_ROOT)
        derived_goal = features.get("description") or prompt or "imported part"
        event_bus.emit("agent",
                        f"Vision extracted: {derived_goal[:80]}",
                        {"features": features})
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
# Dev entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.aria_server:app", host="0.0.0.0", port=8000, reload=True)
