"""Load and parse all context/ .md files into a dict. Keys = filename stem, value = raw text or parsed structure."""
from pathlib import Path
import re


def load_context(repo_root: Path | None = None) -> dict[str, str]:
    """Load all .md files from context/ into a dict. Key = filename without .md, value = file content."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    context_dir = repo_root / "context"
    if not context_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(context_dir.glob("*.md")):
        try:
            out[p.stem] = p.read_text(encoding="utf-8")
        except Exception:
            out[p.stem] = ""
    return out


def parse_tables(text: str) -> dict[str, list[dict[str, str]]]:
    """Extract markdown tables from text. Returns dict section_name -> list of row dicts."""
    tables: dict[str, list[dict[str, str]]] = {}
    current_section = ""
    in_table = False
    headers: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped[3:].strip()
            in_table = False
        if "|" in stripped and not stripped.startswith("|---"):
            parts = [p.strip() for p in stripped.split("|") if p.strip()]
            if not parts:
                continue
            if not in_table:
                headers = parts
                in_table = True
                tables[current_section] = []
            else:
                row = dict(zip(headers, parts + [""] * (len(headers) - len(parts))))
                tables[current_section].append(row)
        else:
            in_table = False
    return tables


def get_mechanical_constants(context: dict[str, str]) -> dict[str, float]:
    """Parse aria_mechanical.md for numeric constants (mm). Returns name -> value in mm."""
    raw = context.get("aria_mechanical", "")
    constants: dict[str, float] = {}
    for line in raw.splitlines():
        # Match "| Name | 700.0 mm |" or "| Value | 47.2 mm |"
        m = re.search(r"\|\s*([^|]+)\s*\|\s*([\d.]+)\s*mm\s*\|", line, re.I)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            try:
                constants[key] = float(m.group(2))
            except ValueError:
                pass
    # Map common names to canonical keys
    aliases = {
        "width": "housing_width",
        "height": "housing_height",
        "depth": "housing_depth",
        "wall_thickness": "wall_thickness",
        "spool_center_x": "spool_center_x",
        "spool_center_y": "spool_center_y",
        "bearing_od": "bearing_od",
        "ratchet_pocket_dia": "ratchet_pocket_dia",
        "ratchet_pocket_depth": "ratchet_pocket_depth",
        "rope_slot_width": "rope_slot_width",
        "rope_slot_length": "rope_slot_length",
    }
    result: dict[str, float] = {}
    for k, v in constants.items():
        result[aliases.get(k, k)] = v
    return result


def load_materials(context: dict) -> list:
    """Parse material library directly from aria_materials.md."""
    from pathlib import Path
    import re
    from .material_study import Material

    md_path = None
    for p in [
        Path("context/aria_materials.md"),
        Path(__file__).parent.parent / "context/aria_materials.md",
    ]:
        if p.exists():
            md_path = p
            break
    if md_path is None:
        return []

    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Find header row starting with "| id" that contains yield_mpa
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("| id") and "yield_mpa" in line:
            header_idx = i
            break
    if header_idx is None:
        return []

    header = [h.strip() for h in lines[header_idx].split("|") if h.strip()]

    materials: list[Material] = []
    for line in lines[header_idx + 1:]:
        # Skip separator rows
        if re.match(r"^[\|\-\s]+$", line):
            continue
        if not line.strip().startswith("|"):
            break
        cols = [c.strip() for c in line.split("|") if c.strip()]
        if len(cols) < len(header):
            continue
        row = dict(zip(header, cols))
        try:
            mat = Material(
                id=row["id"],
                name=row["name"],
                yield_mpa=float(row["yield_mpa"]),
                ultimate_mpa=float(row["ultimate_mpa"]),
                density_gcc=float(row["density_gcc"]),
                relative_cost=float(row["relative_cost"]),
                machinability=float(row["machinability"]),
                processes=[p.strip() for p in row["processes"].split(",") if p.strip()],
            )
            materials.append(mat)
        except (KeyError, ValueError):
            continue

    return materials
