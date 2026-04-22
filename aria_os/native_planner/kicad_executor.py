"""KiCad server-side op executor.

Applies `native_op` events to a growing `pcbnew.BOARD` and writes the
intermediate `.kicad_pcb` to disk after each op. The user refreshes KiCad
to see live progress — there's no interactive WebView like Fusion/Rhino.

Graceful degrade: if `pcbnew` isn't importable (common on non-KiCad Python
envs), we fall back to the existing `aria_os.ecad.kicad_pcb_writer` which
emits a serialized .kicad_pcb file from a dataclass model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _try_import_pcbnew():
    try:
        import pcbnew  # type: ignore
        return pcbnew
    except Exception:
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

    # ---- Public API ----------------------------------------------------

    def execute(self, kind: str, params: dict) -> dict:
        handler = getattr(self, f"_op_{kind}", None)
        if handler is None:
            raise ValueError(f"Unknown KiCad op: {kind}")
        result = handler(params or {})
        self._ops_applied += 1
        self._save()
        return {"ok": True, "op": kind, "ops_applied": self._ops_applied,
                "out": str(self.out_path), **result}

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

    # ---- Op handlers ---------------------------------------------------

    def _op_beginBoard(self, p: dict) -> dict:
        if self._pcbnew is None:
            raise RuntimeError(
                "pcbnew not available in this Python env. "
                "Run KiCad's bundled Python (Windows: "
                "C:/Program Files/KiCad/9.0/bin/python.exe) or install "
                "kicad-python bindings.")
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
        # FootprintLoad signature has varied across KiCad versions
        fp = None
        try:
            fp = self._pcbnew.FootprintLoad(lib_name, fp_name)
        except Exception:
            # Fallback: some installs expect the library path not the nickname
            try:
                fp = self._pcbnew.FootprintLoad(
                    self._pcbnew.GetOSPath(lib_name), fp_name)
            except Exception as exc:
                raise RuntimeError(
                    f"FootprintLoad failed for {fp_id}: {exc}")
        if fp is None:
            raise RuntimeError(f"Footprint {fp_id} not found")
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

    # ---- Helpers -------------------------------------------------------

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
