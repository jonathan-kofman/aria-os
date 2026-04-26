r"""aria_kicad_server.py — Headless HTTP entry point for the ECAD pipeline.

Mirrors the SolidWorks (port 7501) and Rhino (port 7502) HTTP listeners so
the orchestrator can drive KiCad/PCB generation the same way: curl POST
/op, curl GET /screenshot, curl GET /status. No GUI clicks required.

KiCad is different from SW/Rhino — there is no in-process plugin host.
The listener runs as a standalone Python service that:
  * Holds an in-memory `BoardState` (components, tracks, vias, zones).
  * Mutates state on each /op (placeComponent, addTrack, addVia, addZone).
  * Materialises state to a real .kicad_pcb on /save_pcb via the existing
    aria_os.ecad.kicad_pcb_writer.write_kicad_pcb function.
  * Calls kicad-cli for /screenshot (SVG render) and /export_gerbers.
  * Looks up the recipe cache (aria_os.ecad.recipe_db) for known-good
    footprint resolutions and track widths before falling back to defaults
    or the LLM-args synthesizer.

Endpoints (all bound to http://localhost:7505/):
  GET  /status         — { ok, has_active_board, components, recipe_count }
  GET  /info           — full BoardState dump (debug)
  POST /new_board      — body:{ board_w_mm, board_h_mm, n_layers, name }
  POST /op             — body:{ kind, params }
  POST /save_pcb       — body:{ path }
  POST /export_gerbers — body:{ out_dir }
  GET  /screenshot     — SVG render of the active board
  POST /run_drc        — kicad-cli pcb drc
  POST /quit           — clear state, returns { ok }

Launch:
  python -m cad_plugins.kicad.aria_kicad_server         (default port 7505)
  ARIA_KICAD_PORT=7600 python -m ...                     (override)

The orchestrator reaches it via /api/ecad/text-to-board on the dashboard
backend (mirrors /api/cad/text-to-part for SW/Rhino).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

# Make sibling aria_os/ packages importable when running from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aria_os.ecad import recipe_db          # type: ignore  # noqa: E402
from aria_os.ecad.kicad_pcb_writer import (  # type: ignore  # noqa: E402
    export_gerbers,
    write_kicad_pcb,
)


# ---------------------------------------------------------------------------
# Op kind aliases — handle LLM hallucinations the same way the SW/Rhino
# bridges do. The LLM commonly emits "addComponent", "place", "route" instead
# of the canonical names; map them all to one form before dispatch.
# ---------------------------------------------------------------------------
_OP_ALIASES = {
    "addComponent":    "placeComponent",
    "place":           "placeComponent",
    "addPart":         "placeComponent",
    "putComponent":    "placeComponent",
    "route":           "addTrack",
    "addSegment":      "addTrack",
    "addTrace":        "addTrack",
    "addPour":         "addZone",
    "addCopperPour":   "addZone",
    "groundPlane":     "addZone",
    "drc":             "runDrc",
    "newPcb":          "newBoard",
    "newPCB":          "newBoard",
    "createBoard":     "newBoard",
}


# ---------------------------------------------------------------------------
# In-memory board state
# ---------------------------------------------------------------------------
@dataclass
class BoardState:
    name: str = "aria_board"
    board_w_mm: float = 60.0
    board_h_mm: float = 40.0
    n_layers: int = 2
    components: list[dict] = field(default_factory=list)
    extra_tracks: list[dict] = field(default_factory=list)
    extra_vias: list[dict] = field(default_factory=list)
    extra_zones: list[dict] = field(default_factory=list)
    last_save_path: Optional[str] = None
    ops_dispatched: int = 0

    def reset(self, *, name: str, board_w_mm: float, board_h_mm: float,
              n_layers: int) -> None:
        self.name = name
        self.board_w_mm = board_w_mm
        self.board_h_mm = board_h_mm
        self.n_layers = n_layers
        self.components = []
        self.extra_tracks = []
        self.extra_vias = []
        self.extra_zones = []
        self.last_save_path = None

    def to_bom_dict(self) -> dict:
        """Materialise current state as a BOM dict that kicad_pcb_writer
        accepts. The writer expects a JSON file on disk, so callers must
        json.dumps + tempfile this dict before passing the path in."""
        return {
            "board_name":    self.name,
            "board_w_mm":    self.board_w_mm,
            "board_h_mm":    self.board_h_mm,
            "n_layers":      self.n_layers,
            "components":    list(self.components),
            # extras are not part of the BOM schema today; the writer
            # ignores keys it doesn't know. We pass them through anyway
            # so any future writer can pick them up.
            "extra_tracks":  list(self.extra_tracks),
            "extra_vias":    list(self.extra_vias),
            "extra_zones":   list(self.extra_zones),
        }


_STATE = BoardState()
_LOCK = threading.Lock()


def _port() -> int:
    env = os.environ.get("ARIA_KICAD_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return 7505


def _kicad_cli() -> Optional[str]:
    env = os.environ.get("KICAD_CLI")
    if env and Path(env).is_file():
        return env
    on_path = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if on_path:
        return on_path
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) /
            "Programs" / "KiCad" / "10.0" / "bin" / "kicad-cli.exe",
        Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"),
        Path("C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"),
        Path("C:/Program Files/KiCad/8.0/bin/kicad-cli.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


# ---------------------------------------------------------------------------
# Per-op handlers
# ---------------------------------------------------------------------------
def _op_new_board(p: dict) -> dict:
    name = str(p.get("name") or "aria_board")
    w = float(p.get("board_w_mm", p.get("width_mm", 60.0)))
    h = float(p.get("board_h_mm", p.get("height_mm", 40.0)))
    layers = int(p.get("n_layers", p.get("layers", 2)))
    if layers not in (2, 4):
        layers = 2
    _STATE.reset(name=name, board_w_mm=w, board_h_mm=h, n_layers=layers)
    return {"ok": True, "name": name, "board_w_mm": w, "board_h_mm": h,
            "n_layers": layers}


def _op_place_component(p: dict) -> dict:
    """Append a component. params:
       ref, value, footprint, x_mm, y_mm, width_mm, height_mm, rotation_deg,
       nets (list[str]), net_map (dict[str, str]).

    Footprint resolution path:
      1. Explicit `footprint` field if it contains a colon (lib:fp form).
      2. Recipe cache lookup_footprint_recipe(value, package).
      3. Pass through value as the lib:fp; downstream writer's
         _try_real_footprint cascade does the actual library scan.
    """
    ref = str(p.get("ref") or f"U{len(_STATE.components) + 1}")
    value = str(p.get("value", ""))
    package = p.get("package")
    footprint = str(p.get("footprint", "")).strip()

    if not footprint or ":" not in footprint:
        # Try recipe cache before defaulting.
        cached = recipe_db.lookup_footprint_recipe(value, package)
        if cached and cached.get("lib") and cached.get("fp"):
            footprint = f"{cached['lib']}:{cached['fp']}"
        else:
            # Fall back to a heuristic — kicad_pcb_writer will run its
            # own multi-step cascade and store any winning hit back into
            # the recipe cache (downstream).
            footprint = footprint or f"Generic:{value or 'unknown'}"

    comp = {
        "ref":           ref,
        "value":         value,
        "footprint":     footprint,
        "x_mm":          float(p.get("x_mm", 0.0)),
        "y_mm":          float(p.get("y_mm", 0.0)),
        "width_mm":      float(p.get("width_mm", 5.0)),
        "height_mm":     float(p.get("height_mm", 3.0)),
        "rotation_deg":  float(p.get("rotation_deg", 0.0)),
        "nets":          list(p.get("nets") or []),
        "net_map":       dict(p.get("net_map") or {}),
        "description":   str(p.get("description", "")),
    }
    _STATE.components.append(comp)
    return {"ok": True, "ref": ref, "footprint": footprint,
            "n_components": len(_STATE.components)}


def _op_add_track(p: dict) -> dict:
    """Add an explicit copper segment.
    params: net_name, start: [x, y], end: [x, y], width_mm?, layer?
    Width pulled from recipe cache (add_track_default) when absent.
    """
    net = str(p.get("net_name", p.get("net", "")))
    start = p.get("start") or [p.get("x1", 0), p.get("y1", 0)]
    end = p.get("end") or [p.get("x2", 0), p.get("y2", 0)]
    layer = str(p.get("layer", "F.Cu"))

    width = p.get("width_mm")
    if width is None:
        rec = recipe_db.lookup("add_track_default") or {}
        # Power nets get the wider default; signal nets get the narrow one.
        upper = net.upper()
        is_power = upper in ("GND", "AGND", "DGND", "VBAT", "VCC", "VDD",
                              "VBUS", "VIN", "+3V3", "+5V", "+12V", "+24V")
        width = rec.get("width_mm_power" if is_power else "width_mm_signal",
                        0.5 if is_power else 0.25)
    width = float(width)

    track = {
        "net":      net,
        "start":    [float(start[0]), float(start[1])],
        "end":      [float(end[0]),   float(end[1])],
        "width_mm": width,
        "layer":    layer,
        "tstamp":   str(uuid4()),
    }
    _STATE.extra_tracks.append(track)
    # Record the actual win — by the time we get here we believe the
    # combination works. The recipe stores the full default, not per-net.
    recipe_db.record_success("add_track_default", {
        "method":          "PCB_TRACK",
        "width_mm_signal": 0.25,
        "width_mm_power":  0.5,
        "layer":           "F.Cu",
    })
    return {"ok": True, "net": net, "width_mm": width, "layer": layer,
            "n_tracks": len(_STATE.extra_tracks)}


def _op_add_via(p: dict) -> dict:
    """Drop a via. params: at: [x,y], drill_mm?, diameter_mm?, net_name?"""
    at = p.get("at") or [p.get("x", 0), p.get("y", 0)]
    drill = p.get("drill_mm")
    diameter = p.get("diameter_mm")
    if drill is None or diameter is None:
        rec = recipe_db.lookup("add_via_default") or {}
        drill = drill if drill is not None else rec.get("drill_mm", 0.3)
        diameter = diameter if diameter is not None \
            else rec.get("diameter_mm", 0.6)
    via = {
        "at":          [float(at[0]), float(at[1])],
        "drill_mm":    float(drill),
        "diameter_mm": float(diameter),
        "net":         str(p.get("net_name", p.get("net", ""))),
        "tstamp":      str(uuid4()),
    }
    _STATE.extra_vias.append(via)
    recipe_db.record_success("add_via_default", {
        "method": "PCB_VIA",
        "drill_mm": float(drill),
        "diameter_mm": float(diameter),
    })
    return {"ok": True, "n_vias": len(_STATE.extra_vias)}


def _op_add_zone(p: dict) -> dict:
    """Add a copper pour rectangle.
    params: net_name, layer, points (list[[x,y]]) OR rect: [x,y,w,h]"""
    net = str(p.get("net_name", p.get("net", "GND")))
    layer = str(p.get("layer", "B.Cu"))
    points = p.get("points")
    if not points:
        rect = p.get("rect")
        if rect:
            x, y, w, h = (float(rect[0]), float(rect[1]),
                          float(rect[2]), float(rect[3]))
            points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        else:
            # Default: pour over full board area.
            points = [
                [0, 0],
                [_STATE.board_w_mm, 0],
                [_STATE.board_w_mm, _STATE.board_h_mm],
                [0, _STATE.board_h_mm],
            ]
    zone = {
        "net":      net,
        "layer":    layer,
        "points":   [[float(pt[0]), float(pt[1])] for pt in points],
        "tstamp":   str(uuid4()),
    }
    _STATE.extra_zones.append(zone)
    recipe_db.record_success("add_zone_default", {
        "method":           "ZONE",
        "default_layer":    layer,
        "clearance_mm":     0.2,
        "min_thickness_mm": 0.25,
    })
    return {"ok": True, "n_zones": len(_STATE.extra_zones), "layer": layer}


def _op_set_layers(p: dict) -> dict:
    layers = int(p.get("n_layers", p.get("layers", 2)))
    if layers not in (2, 4):
        return {"ok": False, "error": f"unsupported layer count {layers}"}
    _STATE.n_layers = layers
    return {"ok": True, "n_layers": layers}


_OP_HANDLERS = {
    "newBoard":         _op_new_board,
    "placeComponent":   _op_place_component,
    "addTrack":         _op_add_track,
    "addVia":           _op_add_via,
    "addZone":          _op_add_zone,
    "setLayerStack":    _op_set_layers,
}


# ---------------------------------------------------------------------------
# Top-level dispatch (routing + LLM-args fallback)
# ---------------------------------------------------------------------------
def _dispatch_op(kind: str, params: dict) -> dict:
    """Run one op. On KeyError / ValueError, ask the synthesize-args
    backend for a corrected params dict and retry once. Mirrors the SW
    plugin's TrySynthesizeAndCut flow."""
    canonical = _OP_ALIASES.get(kind, kind)
    handler = _OP_HANDLERS.get(canonical)
    if handler is None:
        return {"ok": False, "error": f"unknown op kind {kind!r}"}
    _STATE.ops_dispatched += 1
    try:
        return handler(params)
    except Exception as ex:
        first_err = f"{type(ex).__name__}: {ex}"
        # LLM-args fallback — only worthwhile for placeComponent today.
        synth = _try_synthesize_args(canonical, params, [first_err])
        if synth is not None:
            try:
                result = handler(synth)
                result["recovered_via"] = "synthesize-args"
                return result
            except Exception as ex2:
                return {"ok": False, "error": first_err,
                        "synth_error": f"{type(ex2).__name__}: {ex2}"}
        return {"ok": False, "error": first_err,
                "trace": traceback.format_exc(limit=4)}


def _try_synthesize_args(kind: str, params: dict,
                         failure_msgs: list[str]) -> Optional[dict]:
    """POST to /api/cad/synthesize-args (same backend the SW addin uses).
    Returns the LLM-suggested params dict, or None on failure."""
    base = os.environ.get("ARIA_DASHBOARD_BASE", "http://localhost:8000")
    try:
        import urllib.request
        body = json.dumps({
            "cad":             "kicad",
            "op":              kind,
            "method":          kind,
            "signature":       "kicad_listener",
            "prior_attempts":  [params],
            "failure_msgs":    failure_msgs,
            "context":         {"state": _summary_for_llm()},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/cad/synthesize-args",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        synth = data.get("args")
        if isinstance(synth, dict):
            return synth
    except Exception:
        return None
    return None


def _summary_for_llm() -> dict:
    return {
        "name":          _STATE.name,
        "board_w_mm":    _STATE.board_w_mm,
        "board_h_mm":    _STATE.board_h_mm,
        "n_layers":      _STATE.n_layers,
        "n_components":  len(_STATE.components),
        "n_tracks":      len(_STATE.extra_tracks),
        "n_vias":        len(_STATE.extra_vias),
        "n_zones":       len(_STATE.extra_zones),
        "first_5_refs":  [c["ref"] for c in _STATE.components[:5]],
    }


# ---------------------------------------------------------------------------
# /save_pcb — dump the in-memory BOM to a tempfile, run write_kicad_pcb.
# ---------------------------------------------------------------------------
def _save_pcb(out_path: Path) -> dict:
    bom = _STATE.to_bom_dict()
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".bom.json", encoding="utf-8",
            delete=False) as tmp:
        json.dump(bom, tmp, indent=2)
        bom_path = Path(tmp.name)
    try:
        out_pcb = write_kicad_pcb(
            bom_path,
            out_path,
            board_name=_STATE.name,
            n_layers=_STATE.n_layers,
        )
        _STATE.last_save_path = str(out_pcb)
        return {
            "ok":   True,
            "path": str(out_pcb),
            "size_bytes": out_pcb.stat().st_size,
        }
    finally:
        try: bom_path.unlink()
        except Exception: pass


def _render_screenshot() -> tuple[bytes, str]:
    """Run kicad-cli pcb export svg on the last saved board. Returns
    (bytes, content-type). If kicad-cli isn't available, raises
    RuntimeError so the HTTP handler can return 500."""
    cli = _kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not on PATH")
    if not _STATE.last_save_path or not Path(_STATE.last_save_path).is_file():
        # Save to a temp first if not yet persisted.
        tmp = Path(tempfile.gettempdir()) / f"aria_kicad_render_{uuid4().hex}.kicad_pcb"
        _save_pcb(tmp)
    out_svg = Path(tempfile.gettempdir()) / f"aria_kicad_render_{uuid4().hex}.svg"
    try:
        # kicad-cli 9 writes to a directory by default; some versions take
        # a file path. We pass --output as the file path; KiCad 8/9 accept
        # both forms for SVG.
        proc = subprocess.run(
            [cli, "pcb", "export", "svg",
             "--output", str(out_svg),
             "--layers", "F.Cu,F.Silkscreen,F.Mask,B.Cu,Edge.Cuts",
             "--page-size-mode", "2",  # crop to board outline
             str(_STATE.last_save_path)],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"kicad-cli svg export failed: "
                f"{proc.stderr.decode('utf-8', errors='replace')[:500]}")
        if not out_svg.exists():
            # Some KiCad versions write to {output}/board.svg when output is dir.
            alt = Path(_STATE.last_save_path).with_suffix(".svg")
            if alt.exists():
                out_svg = alt
            else:
                # Try the dir-output convention
                out_dir = out_svg.parent / out_svg.stem
                if out_dir.is_dir():
                    svgs = list(out_dir.glob("*.svg"))
                    if svgs:
                        out_svg = svgs[0]
        if not out_svg.exists():
            raise RuntimeError("kicad-cli reported success but no SVG produced")
        data = out_svg.read_bytes()
    finally:
        try: out_svg.unlink()
        except Exception: pass
    return data, "image/svg+xml"


def _run_drc() -> dict:
    cli = _kicad_cli()
    if not cli:
        return {"ok": False, "error": "kicad-cli not on PATH"}
    if not _STATE.last_save_path:
        return {"ok": False, "error": "no saved PCB — call /save_pcb first"}
    out_json = Path(tempfile.gettempdir()) / f"aria_drc_{uuid4().hex}.json"
    try:
        proc = subprocess.run(
            [cli, "pcb", "drc",
             "--output", str(out_json),
             "--format", "json",
             str(_STATE.last_save_path)],
            capture_output=True, timeout=60,
        )
        if out_json.exists():
            data = json.loads(out_json.read_text("utf-8"))
            violations = data.get("violations", []) or []
            unconnected = data.get("unconnected_items", []) or []
            return {
                "ok":               proc.returncode == 0,
                "violations":       len(violations),
                "unconnected":      len(unconnected),
                "first_5_problems": (violations + unconnected)[:5],
            }
        return {"ok": False,
                "stderr": proc.stderr.decode("utf-8", errors="replace")[:500]}
    finally:
        try: out_json.unlink()
        except Exception: pass


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Send to stderr without the noisy default time prefix.
        sys.stderr.write(f"[aria-kicad] {fmt % args}\n")

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.rstrip("/").lower() or "/"
        try:
            with _LOCK:
                if path == "/status":
                    self._json(200, {
                        "ok":               True,
                        "has_active_board": bool(_STATE.components),
                        "name":             _STATE.name,
                        "board_w_mm":       _STATE.board_w_mm,
                        "board_h_mm":       _STATE.board_h_mm,
                        "n_layers":         _STATE.n_layers,
                        "n_components":     len(_STATE.components),
                        "n_tracks":         len(_STATE.extra_tracks),
                        "n_vias":           len(_STATE.extra_vias),
                        "n_zones":          len(_STATE.extra_zones),
                        "ops_dispatched":   _STATE.ops_dispatched,
                        "recipe_count":     recipe_db.count(),
                        "last_save_path":   _STATE.last_save_path,
                        "kicad_cli":        _kicad_cli() or "(not found)",
                        "port":             _port(),
                    })
                    return
                if path == "/info":
                    self._json(200, {
                        "ok":          True,
                        "state":       asdict(_STATE),
                    })
                    return
                if path == "/screenshot":
                    data, ct = _render_screenshot()
                    self._binary(200, data, ct)
                    return
            self._json(404, {"ok": False, "error": f"unknown route GET {path}"})
        except Exception as ex:
            self._json(500, {"ok": False,
                              "error": f"{type(ex).__name__}: {ex}",
                              "trace": traceback.format_exc(limit=4)})

    def do_POST(self):
        path = self.path.rstrip("/").lower() or "/"
        try:
            body = self._read_body()
            with _LOCK:
                if path == "/new_board":
                    self._json(200, _op_new_board(body))
                    return
                if path == "/op":
                    kind = body.get("kind", "")
                    params = body.get("params") or {}
                    if not kind:
                        self._json(400,
                            {"ok": False, "error": "op requires 'kind'"})
                        return
                    result = _dispatch_op(kind, params)
                    self._json(200, {"ok": result.get("ok", True),
                                      "kind": kind, "result": result})
                    return
                if path == "/save_pcb":
                    out = body.get("path") \
                        or str(_REPO_ROOT / "outputs" / "ecad" /
                                f"{_STATE.name}.kicad_pcb")
                    out_path = Path(out)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    self._json(200, _save_pcb(out_path))
                    return
                if path == "/export_gerbers":
                    if not _STATE.last_save_path:
                        # Auto-save to default first.
                        default_pcb = (_REPO_ROOT / "outputs" / "ecad" /
                                       f"{_STATE.name}.kicad_pcb")
                        default_pcb.parent.mkdir(parents=True, exist_ok=True)
                        _save_pcb(default_pcb)
                    out_dir = body.get("out_dir") or \
                        str(Path(_STATE.last_save_path).parent / "gerbers")
                    result = export_gerbers(_STATE.last_save_path, out_dir)
                    self._json(200, result)
                    return
                if path == "/run_drc":
                    self._json(200, _run_drc())
                    return
                if path == "/quit":
                    _STATE.reset(name="aria_board",
                                  board_w_mm=60.0, board_h_mm=40.0,
                                  n_layers=2)
                    self._json(200, {"ok": True})
                    return
            self._json(404, {"ok": False,
                              "error": f"unknown route POST {path}"})
        except Exception as ex:
            self._json(500, {"ok": False,
                              "error": f"{type(ex).__name__}: {ex}",
                              "trace": traceback.format_exc(limit=4)})


def _ensure_kicad_on_path() -> None:
    """The legacy export_gerbers helper in kicad_pcb_writer.py uses
    shutil.which() with no fallback list. Prepend KiCad's bin dir to PATH
    so it finds the same kicad-cli our own _kicad_cli() helper resolves.
    """
    cli = _kicad_cli()
    if not cli:
        return
    bindir = str(Path(cli).parent)
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep)
    if bindir not in parts:
        os.environ["PATH"] = bindir + os.pathsep + cur


def main() -> None:
    recipe_db.init()
    _ensure_kicad_on_path()
    port = _port()
    print(f"aria_kicad_server: http://localhost:{port}/  "
          f"(kicad-cli={_kicad_cli() or 'NOT FOUND'})", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("aria_kicad_server: shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
