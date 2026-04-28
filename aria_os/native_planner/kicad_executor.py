"""KiCad server-side op executor.

Applies `native_op` events to a growing `pcbnew.BOARD` and writes the
intermediate `.kicad_pcb` to disk after each op. The user refreshes KiCad
to see live progress — there's no interactive WebView like Fusion/Rhino.

Graceful degrade: if `pcbnew` isn't importable (the dashboard's Python
env is rarely KiCad's bundled python), we maintain the same op state
in-memory and serialise via `aria_os.ecad.kicad_pcb_writer.write_kicad_pcb`
at every save. The user gets a real .kicad_pcb either way — the only
thing they lose without pcbnew is per-op auto-route hooks (routeBoard).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from aria_os.ecad import recipe_db


def _try_import_pcbnew():
    try:
        import pcbnew  # type: ignore
        return pcbnew
    except Exception:
        return None


def _kicad_python_paths() -> list[Path]:
    """Candidate locations for KiCad's bundled python.exe across versions
    and Windows install variants. Used so the user is told *exactly* which
    interpreter has pcbnew when fallback also fails."""
    user = os.environ.get("LOCALAPPDATA", "")
    return [
        Path(user) / "Programs" / "KiCad" / "10.0" / "bin" / "python.exe",
        Path(user) / "Programs" / "KiCad" / "9.0"  / "bin" / "python.exe",
        Path("C:/Program Files/KiCad/10.0/bin/python.exe"),
        Path("C:/Program Files/KiCad/9.0/bin/python.exe"),
        Path("C:/Program Files/KiCad/8.0/bin/python.exe"),
    ]


def _resolve_kicad_python() -> Optional[str]:
    for p in _kicad_python_paths():
        if p.is_file():
            return str(p)
    return None


class KicadExecutor:
    """One instance per KiCad run. Persists state (board, nets, refs)
    across ops so later ops can reference earlier placements."""

    def __init__(self, out_path: Path):
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._pcbnew = _try_import_pcbnew()
        self._board = None
        self._nets: dict[str, int] = {}        # name → netcode
        self._components: dict[str, Any] = {}  # ref → footprint object
        self._ops_applied = 0
        # Auto-learning recipe cache for native KiCad/pcbnew ops.
        recipe_db.init()

        # Fallback state — used when pcbnew can't be imported into the
        # caller's Python env. Mirrors the BoardState dataclass in
        # aria_kicad_server.py so the same write_kicad_pcb path can
        # serialise it at /save_pcb time.
        self._fallback = self._pcbnew is None
        self._fallback_state: dict[str, Any] = {
            "board_name":   self.out_path.stem or "aria_board",
            "board_w_mm":   60.0,
            "board_h_mm":   40.0,
            "n_layers":     2,
            "components":   [],     # list[dict]
            "extra_tracks": [],
            "extra_vias":   [],
            "extra_zones":  [],
            "nets":         set(),  # str
        }
        if self._fallback:
            print(f"[kicad-executor] pcbnew unavailable — using "
                  f"write_kicad_pcb fallback (KiCad python at "
                  f"{_resolve_kicad_python() or 'NOT FOUND'} when needed)",
                  flush=True)

    # ---- Public API ----------------------------------------------------

    def execute(self, kind: str, params: dict) -> dict:
        if self._fallback:
            handler = getattr(self, f"_fbop_{kind}", None)
            if handler is None:
                raise ValueError(f"Unknown KiCad op (fallback): {kind}")
            result = handler(params or {})
            self._ops_applied += 1
            self._save_fallback()
            return {"ok": True, "op": kind, "ops_applied": self._ops_applied,
                    "out": str(self.out_path), "via": "kicad_pcb_writer",
                    **result}
        handler = getattr(self, f"_op_{kind}", None)
        if handler is None:
            raise ValueError(f"Unknown KiCad op: {kind}")
        result = handler(params or {})
        self._ops_applied += 1
        self._save()
        return {"ok": True, "op": kind, "ops_applied": self._ops_applied,
                "out": str(self.out_path), "via": "pcbnew", **result}

    def _save(self):
        if self._pcbnew is None or self._board is None:
            return
        try:
            self._pcbnew.SaveBoard(str(self.out_path), self._board)
        except Exception:
            # If saving intermediately fails we still try to continue —
            # the next op may fix the state. Emit nothing; the executor
            # result already carries `out` path.
            pass

    def _save_fallback(self) -> None:
        """Materialise the in-memory BOM state to a real .kicad_pcb via
        the file-format writer. Runs after every op so the user can
        refresh KiCad and see live progress."""
        from aria_os.ecad.kicad_pcb_writer import write_kicad_pcb
        bom = {
            "board_name":   self._fallback_state["board_name"],
            "board_w_mm":   self._fallback_state["board_w_mm"],
            "board_h_mm":   self._fallback_state["board_h_mm"],
            "n_layers":     self._fallback_state["n_layers"],
            "components":   list(self._fallback_state["components"]),
            "extra_tracks": list(self._fallback_state["extra_tracks"]),
            "extra_vias":   list(self._fallback_state["extra_vias"]),
            "extra_zones":  list(self._fallback_state["extra_zones"]),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bom.json",
                                           encoding="utf-8",
                                           delete=False) as tmp:
            json.dump(bom, tmp, indent=2)
            bom_path = Path(tmp.name)
        try:
            write_kicad_pcb(
                bom_path,
                self.out_path,
                board_name=self._fallback_state["board_name"],
                n_layers=int(self._fallback_state["n_layers"]),
            )
        finally:
            try: bom_path.unlink()
            except Exception: pass

    # ---- Op handlers (pcbnew path) ------------------------------------

    def _op_beginBoard(self, p: dict) -> dict:
        # pcbnew is non-None here (fallback would have hijacked execute()).
        self._board = self._pcbnew.BOARD()
        # Set board outline rectangle
        w = float(p.get("width_mm", 30.0))
        h = float(p.get("height_mm", 20.0))
        self._draw_outline(0, 0, w, h)
        return {"board_size_mm": [w, h]}

    def _op_setStackup(self, p: dict) -> dict:
        # pcbnew allows setting copper count via SetCopperLayerCount.
        layers = p.get("layers") or ["F.Cu", "B.Cu"]
        n_cu = sum(1 for l in layers if ".Cu" in l)
        if self._board is not None:
            self._board.SetCopperLayerCount(n_cu)
        return {"copper_layers": n_cu}

    def _op_addNet(self, p: dict) -> dict:
        name = p.get("name")
        if not name:
            raise ValueError("addNet requires name")
        if self._board is None:
            raise RuntimeError("Call beginBoard first")
        net = self._pcbnew.NETINFO_ITEM(self._board, name)
        self._board.Add(net)
        self._nets[name] = net.GetNetCode()
        return {"name": name, "netcode": self._nets[name]}

    def _op_placeComponent(self, p: dict) -> dict:
        ref = p["ref"]
        fp_id = p["footprint"]
        x = float(p["x_mm"])
        y = float(p["y_mm"])
        rot = float(p.get("rot_deg", 0.0))
        layer_name = p.get("layer", "F.Cu")
        if self._board is None:
            raise RuntimeError("Call beginBoard first")
        # Load footprint from the active library resolution path
        try:
            lib_name, fp_name = fp_id.split(":", 1)
        except ValueError:
            raise ValueError(f"Footprint id must be 'lib:name' (got {fp_id!r})")
        # FootprintLoad signature has varied across KiCad versions.
        # Try the recipe-recommended form first, then fall back to the
        # other and persist the winner so subsequent calls hit the
        # right path on the first try.
        sig_recipe = recipe_db.lookup("place_component_signature") or {}
        preferred_form = sig_recipe.get("form", "lib_nickname")
        forms_to_try = (
            ["lib_nickname", "lib_path"]
            if preferred_form == "lib_nickname"
            else ["lib_path", "lib_nickname"])
        fp = None
        winning_form = None
        last_exc: Exception | None = None
        for form in forms_to_try:
            try:
                if form == "lib_nickname":
                    fp = self._pcbnew.FootprintLoad(lib_name, fp_name)
                else:
                    fp = self._pcbnew.FootprintLoad(
                        self._pcbnew.GetOSPath(lib_name), fp_name)
                if fp is not None:
                    winning_form = form
                    break
            except Exception as exc:
                last_exc = exc
        if fp is None:
            raise RuntimeError(
                f"FootprintLoad failed for {fp_id}: {last_exc}")
        # Persist the winning signature variant.
        try:
            recipe_db.record_success("place_component_signature",
                                      {"form": winning_form})
        except Exception:
            pass
        fp.SetReference(ref)
        fp.SetPosition(self._pcbnew.VECTOR2I_MM(x, y))
        if rot:
            fp.SetOrientationDegrees(rot)
        if layer_name == "B.Cu":
            fp.Flip(fp.GetCenter(), True)
        self._board.Add(fp)
        self._components[ref] = fp
        return {"ref": ref, "footprint": fp_id, "position_mm": [x, y]}

    def _op_addTrack(self, p: dict) -> dict:
        if self._board is None:
            raise RuntimeError("Call beginBoard first")
        net_name = p["net"]
        net_code = self._nets.get(net_name)
        if net_code is None:
            raise ValueError(f"Unknown net: {net_name}")
        x1 = float(p["x1_mm"]); y1 = float(p["y1_mm"])
        x2 = float(p["x2_mm"]); y2 = float(p["y2_mm"])
        width_mm = float(p.get("width_mm", 0.25))
        layer_name = p.get("layer", "F.Cu")
        track = self._pcbnew.PCB_TRACK(self._board)
        track.SetStart(self._pcbnew.VECTOR2I_MM(x1, y1))
        track.SetEnd(self._pcbnew.VECTOR2I_MM(x2, y2))
        track.SetWidth(self._pcbnew.FromMM(width_mm))
        track.SetLayer(self._resolve_layer(layer_name))
        track.SetNetCode(net_code)
        self._board.Add(track)
        return {"net": net_name, "width_mm": width_mm, "layer": layer_name}

    def _op_addVia(self, p: dict) -> dict:
        if self._board is None:
            raise RuntimeError("Call beginBoard first")
        net_name = p["net"]
        net_code = self._nets.get(net_name)
        if net_code is None:
            raise ValueError(f"Unknown net: {net_name}")
        via = self._pcbnew.PCB_VIA(self._board)
        via.SetPosition(self._pcbnew.VECTOR2I_MM(
            float(p["x_mm"]), float(p["y_mm"])))
        via.SetDrill(self._pcbnew.FromMM(float(p.get("drill_mm", 0.3))))
        via.SetWidth(self._pcbnew.FromMM(float(p.get("diameter_mm", 0.6))))
        via.SetNetCode(net_code)
        self._board.Add(via)
        return {"net": net_name}

    def _op_routeBoard(self, p: dict) -> dict:
        """Call Freerouting to auto-route all pending nets on the board.

        This exports the current .kicad_pcb to DSN, runs Freerouting to
        generate a SES session file, and imports the session back —
        which fills in the track geometry for every net. Needs Java +
        freerouting.jar (see aria_os/ecad/autoroute.py for install tips).
        """
        from aria_os.ecad.autoroute import run_autoroute
        # Save current state first so autoroute sees the latest placements
        if self._board is not None and self._pcbnew is not None:
            self._pcbnew.SaveBoard(str(self.out_path), self._board)
        result = run_autoroute(self.out_path,
                                max_seconds=int(p.get("timeout_s", 90)))
        if not result.get("ok"):
            raise RuntimeError(
                f"Freerouting failed: {result.get('error', 'unknown')}")
        # Reload the routed board so subsequent ops see the tracks
        try:
            self._board = self._pcbnew.LoadBoard(str(self.out_path))
        except Exception:
            pass
        return {"ok": True, "kind": "routed_board",
                "tracks_added": result.get("tracks_added", 0),
                "vias_added":   result.get("vias_added", 0)}

    def _op_addZone(self, p: dict) -> dict:
        if self._board is None:
            raise RuntimeError("Call beginBoard first")
        net_name = p["net"]
        net_code = self._nets.get(net_name, 0)
        layer = self._resolve_layer(p.get("layer", "B.Cu"))
        polygon = p.get("polygon") or []
        if len(polygon) < 3:
            raise ValueError("Zone polygon must have at least 3 points")
        zone = self._pcbnew.ZONE(self._board)
        outline = zone.Outline()
        outline.NewOutline()
        for (x, y) in polygon:
            outline.Append(
                self._pcbnew.VECTOR2I_MM(float(x), float(y)))
        zone.SetLayer(layer)
        zone.SetNetCode(net_code)
        self._board.Add(zone)
        return {"net": net_name, "layer": p.get("layer"),
                "vertices": len(polygon)}

    # ---- Helpers (pcbnew path) ----------------------------------------

    def _resolve_layer(self, name: str) -> int:
        # KiCad's layer names → integer IDs via GetLayerID
        return self._board.GetLayerID(name)

    def _draw_outline(self, x: float, y: float, w: float, h: float):
        if self._board is None or self._pcbnew is None:
            return
        edge = self._pcbnew.Edge_Cuts
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
        for (a, b) in zip(corners, corners[1:]):
            seg = self._pcbnew.PCB_SHAPE(self._board)
            seg.SetShape(self._pcbnew.SHAPE_T_SEGMENT)
            seg.SetStart(self._pcbnew.VECTOR2I_MM(a[0], a[1]))
            seg.SetEnd(self._pcbnew.VECTOR2I_MM(b[0], b[1]))
            seg.SetLayer(edge)
            seg.SetWidth(self._pcbnew.FromMM(0.15))
            self._board.Add(seg)

    # ---- Op handlers (fallback path: write_kicad_pcb-only) ------------
    # These mirror the BoardState ops in cad-plugins/kicad/aria_kicad_server.py.
    # No live pcbnew board — every op mutates self._fallback_state and the
    # serializer rebuilds the .kicad_pcb on every save.

    def _fbop_beginBoard(self, p: dict) -> dict:
        self._fallback_state["board_w_mm"] = float(
            p.get("width_mm", p.get("board_w_mm", 60.0)))
        self._fallback_state["board_h_mm"] = float(
            p.get("height_mm", p.get("board_h_mm", 40.0)))
        if "name" in p:
            self._fallback_state["board_name"] = str(p["name"])
        return {"board_size_mm": [self._fallback_state["board_w_mm"],
                                    self._fallback_state["board_h_mm"]]}

    def _fbop_setStackup(self, p: dict) -> dict:
        layers = p.get("layers") or ["F.Cu", "B.Cu"]
        n_cu = sum(1 for l in layers if ".Cu" in l)
        if n_cu not in (2, 4): n_cu = 2
        self._fallback_state["n_layers"] = n_cu
        return {"copper_layers": n_cu}

    def _fbop_addNet(self, p: dict) -> dict:
        name = p.get("name")
        if not name:
            raise ValueError("addNet requires name")
        self._fallback_state["nets"].add(str(name))
        return {"name": name}

    def _fbop_placeComponent(self, p: dict) -> dict:
        # Same shape as aria_kicad_server.py:_op_place_component, so the
        # writer (which reads BoardState dicts) handles it identically.
        ref = str(p.get("ref") or
                    f"U{len(self._fallback_state['components']) + 1}")
        value = str(p.get("value", ""))
        package = p.get("package")
        footprint = str(p.get("footprint", "")).strip()
        if not footprint or ":" not in footprint:
            cached = recipe_db.lookup_footprint_recipe(value, package)
            if cached and cached.get("lib") and cached.get("fp"):
                footprint = f"{cached['lib']}:{cached['fp']}"
            else:
                footprint = footprint or f"Generic:{value or 'unknown'}"
        comp = {
            "ref":           ref,
            "value":         value,
            "footprint":     footprint,
            "x_mm":          float(p.get("x_mm", 0.0)),
            "y_mm":          float(p.get("y_mm", 0.0)),
            "width_mm":      float(p.get("width_mm", 5.0)),
            "height_mm":     float(p.get("height_mm", 3.0)),
            "rotation_deg":  float(p.get("rotation_deg",
                                           p.get("rot_deg", 0.0))),
            "nets":          list(p.get("nets") or []),
            "net_map":       dict(p.get("net_map") or {}),
            "description":   str(p.get("description", "")),
        }
        self._fallback_state["components"].append(comp)
        for n in comp["nets"]:
            self._fallback_state["nets"].add(str(n))
        for n in comp["net_map"].values():
            self._fallback_state["nets"].add(str(n))
        return {"ref": ref, "footprint": footprint,
                "n_components": len(self._fallback_state["components"])}

    def _fbop_addTrack(self, p: dict) -> dict:
        net = str(p.get("net", p.get("net_name", "")))
        width = float(p.get("width_mm", 0.25))
        x1 = float(p.get("x1_mm", (p.get("start") or [0, 0])[0]))
        y1 = float(p.get("y1_mm", (p.get("start") or [0, 0])[1]))
        x2 = float(p.get("x2_mm", (p.get("end")   or [0, 0])[0]))
        y2 = float(p.get("y2_mm", (p.get("end")   or [0, 0])[1]))
        layer = str(p.get("layer", "F.Cu"))
        self._fallback_state["extra_tracks"].append({
            "net":      net,
            "start":    [x1, y1],
            "end":      [x2, y2],
            "width_mm": width,
            "layer":    layer,
        })
        if net:
            self._fallback_state["nets"].add(net)
        return {"net": net, "width_mm": width, "layer": layer}

    def _fbop_addVia(self, p: dict) -> dict:
        net = str(p.get("net", p.get("net_name", "")))
        self._fallback_state["extra_vias"].append({
            "net":          net,
            "at":           [float(p.get("x_mm", 0.0)),
                              float(p.get("y_mm", 0.0))],
            "drill_mm":     float(p.get("drill_mm", 0.3)),
            "diameter_mm":  float(p.get("diameter_mm", 0.6)),
        })
        if net:
            self._fallback_state["nets"].add(net)
        return {"net": net}

    def _fbop_addZone(self, p: dict) -> dict:
        net = str(p.get("net", p.get("net_name", "GND")))
        layer = str(p.get("layer", "B.Cu"))
        polygon = p.get("polygon") or p.get("points")
        if not polygon:
            w = self._fallback_state["board_w_mm"]
            h = self._fallback_state["board_h_mm"]
            polygon = [[0, 0], [w, 0], [w, h], [0, h]]
        if len(polygon) < 3:
            raise ValueError("Zone polygon must have at least 3 points")
        self._fallback_state["extra_zones"].append({
            "net":      net,
            "layer":    layer,
            "points":   [[float(pt[0]), float(pt[1])] for pt in polygon],
        })
        if net:
            self._fallback_state["nets"].add(net)
        return {"net": net, "layer": layer, "vertices": len(polygon)}

    def _fbop_routeBoard(self, p: dict) -> dict:
        """Auto-route via Freerouting on the in-memory BOM serialised so
        far. Same behaviour as the pcbnew path — just with one extra
        save round-trip since we don't keep a live BOARD."""
        from aria_os.ecad.autoroute import run_autoroute
        self._save_fallback()
        result = run_autoroute(self.out_path,
                                max_seconds=int(p.get("timeout_s", 90)))
        if not result.get("ok"):
            raise RuntimeError(
                f"Freerouting failed: {result.get('error', 'unknown')}")
        return {"ok": True, "kind": "routed_board",
                "tracks_added": result.get("tracks_added", 0),
                "vias_added":   result.get("vias_added", 0),
                "via":          "fallback+freerouting"}
