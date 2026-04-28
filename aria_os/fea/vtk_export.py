r"""vtk_export.py — convert a CalculiX .frd into a single .vtu for vtk.js.

StructSight (visualize-it monorepo, 8th app) renders FEA results in the
browser via vtk.js. vtk.js consumes XML VTK formats — for an unstructured
tet mesh with per-node displacement + stress, we want a `.vtu`
(VTK_UNSTRUCTURED_GRID, version 1.0, ASCII for portability).

What we extract from the .frd:
    - node coordinates (block "    2C" header → "-1 <id> x y z")
    - tet4 elements      (block "    3C" header → element rows)
    - displacement (U)   (block "-4  DISP")
    - stress       (S)   (block "-4  STRESS")
    - per-node von Mises is computed in Python from the 6 stress components

The output VTU is self-contained — no external deps required (we don't
use python-vtk / meshio); we just write the XML by hand. Keeps the
runtime light for cloud deploys.
"""
from __future__ import annotations

from pathlib import Path
import math


def _parse_frd(frd_path: Path) -> dict:
    """One pass over the .frd file: nodes, elements, U, S.
    Returns {"nodes": {nid:(x,y,z)}, "tets": [(n1,n2,n3,n4),...],
              "U": {nid:(ux,uy,uz)}, "S": {nid:(sxx,syy,szz,sxy,syz,szx)}}.
    Missing blocks become empty dicts.
    """
    nodes: dict[int, tuple[float, float, float]] = {}
    tets: list[tuple[int, int, int, int]] = []
    U: dict[int, tuple[float, float, float]] = {}
    S: dict[int, tuple[float, float, float, float, float, float]] = {}

    text = frd_path.read_text(errors="replace")
    lines = text.splitlines()

    # State machine: `mode` is one of None | 'nodes' | 'elems' | 'u' | 's'
    mode: str | None = None
    elem_pending: list[int] = []  # carry over for multiline elements

    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue

        # Block headers
        # Nodes: " 2C" prefix often followed by number-of-nodes
        if line.startswith("    2C"):
            mode = "nodes"
            continue
        # Elements: " 3C"
        if line.startswith("    3C"):
            mode = "elems"
            elem_pending = []
            continue
        # Result block headers like " -4  DISP" or " -4  STRESS"
        if line.startswith(" -4"):
            up = line.upper()
            if "DISP" in up:
                mode = "u"
            elif "STRESS" in up:
                mode = "s"
            else:
                mode = None
            continue
        # End of a result block
        if line.startswith(" -3"):
            mode = None
            continue

        if mode == "nodes" and line.startswith(" -1"):
            # " -1<nid>   x   y   z"
            parts = line.split()
            if len(parts) >= 5:
                try:
                    nid = int(parts[1])
                    x = float(parts[2]); y = float(parts[3]); z = float(parts[4])
                    nodes[nid] = (x, y, z)
                except ValueError:
                    pass
            continue

        if mode == "elems":
            # CCX writes elements as:
            #   " -1 <eid>  <type>  <group>  <material>"
            #   " -2  n1 n2 n3 n4 ..."
            if line.startswith(" -1"):
                # Reset; element row about to begin
                elem_pending = []
                continue
            if line.startswith(" -2"):
                parts = line.split()
                # Skip leading "-2"
                ints = []
                for tok in parts[1:]:
                    try:
                        ints.append(int(tok))
                    except ValueError:
                        break
                elem_pending.extend(ints)
                # Tet4 has 4 node refs total
                if len(elem_pending) >= 4:
                    n1, n2, n3, n4 = elem_pending[:4]
                    tets.append((n1, n2, n3, n4))
                    elem_pending = elem_pending[4:]
                continue

        if mode in ("u", "s") and line.startswith(" -1"):
            parts = line.split()
            try:
                nid = int(parts[1])
                vals = [float(x) for x in parts[2:]]
            except (ValueError, IndexError):
                continue
            if mode == "u" and len(vals) >= 3:
                U[nid] = (vals[0], vals[1], vals[2])
            elif mode == "s" and len(vals) >= 6:
                S[nid] = (vals[0], vals[1], vals[2], vals[3], vals[4], vals[5])

    return {"nodes": nodes, "tets": tets, "U": U, "S": S}


def _von_mises(s: tuple[float, float, float, float, float, float]) -> float:
    sxx, syy, szz, sxy, syz, szx = s
    return math.sqrt(
        ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2
         + 6 * (sxy * sxy + syz * syz + szx * szx)) / 2.0)


def frd_to_vtu(frd_path: str | Path,
                vtu_path: str | Path) -> str:
    """Convert .frd → .vtu (VTK_UNSTRUCTURED_GRID, ASCII).

    Returns the absolute string path of the .vtu on success;
    raises ValueError if the .frd had no nodes or no elements.
    """
    frd = Path(frd_path)
    out = Path(vtu_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    parsed = _parse_frd(frd)
    nodes = parsed["nodes"]
    tets = parsed["tets"]
    U = parsed["U"]
    S = parsed["S"]

    if not nodes:
        raise ValueError(f"frd had no nodes: {frd}")
    if not tets:
        raise ValueError(f"frd had no tet elements: {frd}")

    # VTU expects 0-based contiguous node indices. Build a remap.
    sorted_ids = sorted(nodes.keys())
    remap = {nid: i for i, nid in enumerate(sorted_ids)}
    n_pts = len(sorted_ids)
    n_cells = len(tets)

    # Points
    pt_lines = []
    for nid in sorted_ids:
        x, y, z = nodes[nid]
        pt_lines.append(f"{x:.6f} {y:.6f} {z:.6f}")

    # Cells
    conn_lines = []
    offsets = []
    types = []
    off = 0
    for t in tets:
        try:
            a, b, c, d = (remap[t[0]], remap[t[1]], remap[t[2]], remap[t[3]])
        except KeyError:
            # element refers to a node we don't have; skip
            continue
        conn_lines.append(f"{a} {b} {c} {d}")
        off += 4
        offsets.append(str(off))
        types.append("10")  # VTK_TETRA
    n_cells = len(conn_lines)

    # Point-data fields
    disp_lines = []
    vm_lines = []
    s_lines = []
    for nid in sorted_ids:
        u = U.get(nid, (0.0, 0.0, 0.0))
        disp_lines.append(f"{u[0]:.6e} {u[1]:.6e} {u[2]:.6e}")
        s = S.get(nid, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        vm_lines.append(f"{_von_mises(s):.6e}")
        s_lines.append(
            f"{s[0]:.6e} {s[1]:.6e} {s[2]:.6e} "
            f"{s[3]:.6e} {s[4]:.6e} {s[5]:.6e}")

    xml: list[str] = []
    xml.append('<?xml version="1.0"?>')
    xml.append(
        '<VTKFile type="UnstructuredGrid" version="1.0" byte_order="LittleEndian">')
    xml.append("  <UnstructuredGrid>")
    xml.append(
        f'    <Piece NumberOfPoints="{n_pts}" NumberOfCells="{n_cells}">')

    xml.append("      <Points>")
    xml.append('        <DataArray type="Float32" NumberOfComponents="3" '
               'format="ascii">')
    xml.append("          " + " ".join(pt_lines))
    xml.append("        </DataArray>")
    xml.append("      </Points>")

    xml.append("      <Cells>")
    xml.append('        <DataArray type="Int32" Name="connectivity" '
               'format="ascii">')
    xml.append("          " + " ".join(conn_lines))
    xml.append("        </DataArray>")
    xml.append('        <DataArray type="Int32" Name="offsets" format="ascii">')
    xml.append("          " + " ".join(offsets))
    xml.append("        </DataArray>")
    xml.append('        <DataArray type="UInt8" Name="types" format="ascii">')
    xml.append("          " + " ".join(types))
    xml.append("        </DataArray>")
    xml.append("      </Cells>")

    xml.append('      <PointData Vectors="displacement" Scalars="von_mises">')
    xml.append('        <DataArray type="Float32" Name="displacement" '
               'NumberOfComponents="3" format="ascii">')
    xml.append("          " + " ".join(disp_lines))
    xml.append("        </DataArray>")
    xml.append('        <DataArray type="Float32" Name="von_mises" '
               'format="ascii">')
    xml.append("          " + " ".join(vm_lines))
    xml.append("        </DataArray>")
    xml.append('        <DataArray type="Float32" Name="stress_tensor" '
               'NumberOfComponents="6" format="ascii">')
    xml.append("          " + " ".join(s_lines))
    xml.append("        </DataArray>")
    xml.append("      </PointData>")

    xml.append("    </Piece>")
    xml.append("  </UnstructuredGrid>")
    xml.append("</VTKFile>")

    out.write_text("\n".join(xml), encoding="utf-8")
    return str(out.resolve())


__all__ = ["frd_to_vtu"]
