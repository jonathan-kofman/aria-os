"""
Modify existing generated CadQuery part via natural language.
Loads code from generated_code/, asks LLM to apply modification, validates and exports with _modN suffix.
"""
import re
from pathlib import Path
from typing import Optional

from .context_loader import load_context
from .validator import validate, validate_step_file, ValidationResult
from .exporter import get_output_paths
from . import llm_generator


def _next_mod_index(generated_code_dir: Path, base_stem: str) -> int:
    """Return next mod index (1, 2, ...) based on existing base_stem_modN.py files."""
    existing = list(generated_code_dir.glob(f"{base_stem}_mod*.py"))
    indices = []
    for p in existing:
        m = re.search(r"_mod(\d+)\.py$", p.name)
        if m:
            indices.append(int(m.group(1)))
    return max(indices, default=0) + 1


def _base_export_name_from_stem(stem: str) -> str:
    """Derive export base name from generated code filename stem (e.g. for get_output_paths)."""
    # Stem like "2026-03-09_21-03_generate_the_ARIA_ratchet_ring__outer_diameter_213"
    # Use same logic as exporter._goal_to_part_name for the descriptive part
    g = stem.lower()
    # Strip leading date/time prefix if present
    if re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_", g):
        g = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_", "", g)
    g = re.sub(r"^generate\s+(?:the\s+)?", "", g)
    words = re.sub(r"[^\w\s]", " ", g).split()
    stop = {"the", "a", "an", "for", "with", "mm", "diameter", "long", "wide", "thick", "from", "has", "all", "centered"}
    words = [w for w in words if len(w) > 0 and w not in stop and not w.isdigit() and not w.endswith("mm")]
    name = "_".join(words[:6]) if words else "part"
    name = name[:40]
    return f"llm_{name}" if not name.startswith("llm_") else name


class PartModifier:
    """Modify an existing generated part via natural language; validate and export with _modN suffix."""

    def __init__(self, repo_root: Optional[Path] = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = Path(repo_root)
        self.generated_code_dir = self.repo_root / "outputs" / "cad" / "generated_code"
        self.generated_code_dir.mkdir(parents=True, exist_ok=True)

    def find_latest_script(self, keyword: Optional[str] = None) -> Optional[Path]:
        """Return the most recently modified .py in generated_code/, optionally filtered by keyword.

        Excludes _modN files so we always target an original generated script.
        """
        candidates = [
            p for p in self.generated_code_dir.glob("*.py")
            if not re.search(r"_mod\d+\.py$", p.name)
        ]
        if keyword:
            kw = keyword.lower()
            candidates = [p for p in candidates if kw in p.name.lower()] or candidates
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def extract_goal(self, script_path: Path) -> str:
        """Read the ``# ARIA-GOAL:`` comment from the first line of a script, if present."""
        try:
            first = script_path.read_text(encoding="utf-8").splitlines()[0]
            if first.startswith("# ARIA-GOAL:"):
                return first[len("# ARIA-GOAL:"):].strip()
        except Exception:
            pass
        return ""

    def modify(
        self,
        base_part_path: str,
        modification: str,
        context: Optional[dict] = None,
        max_attempts: int = 3,
    ) -> ValidationResult:
        """
        base_part_path: path to existing .py file in outputs/cad/generated_code/ (or absolute)
        modification: natural language description of what to change
        context: loaded context dict (if None, load_context(repo_root) is used)
        Returns: ValidationResult; on success, modified code and STEP/STL are written.
        """
        if context is None:
            context = load_context(self.repo_root)

        path = Path(base_part_path)
        if not path.is_absolute():
            # Try under repo root then under generated_code
            for base in [self.repo_root, self.generated_code_dir]:
                candidate = (base / path).resolve()
                if candidate.exists():
                    path = candidate
                    break
            else:
                path = (self.repo_root / base_part_path).resolve()
        if not path.exists():
            return ValidationResult(
                passed=False,
                geometry=None,
                error=f"Base part file not found: {path}",
                errors=[f"File not found: {path}"],
            )

        try:
            existing_code = path.read_text(encoding="utf-8")
        except Exception as e:
            return ValidationResult(
                passed=False,
                geometry=None,
                error=str(e),
                errors=[str(e)],
            )

        base_stem = path.stem
        base_export_name = _base_export_name_from_stem(base_stem)
        mod_index = _next_mod_index(self.generated_code_dir, base_stem)
        export_name = f"{base_export_name}_mod{mod_index}"
        paths = get_output_paths(export_name, self.repo_root)
        step_path = Path(paths["step_path"])
        stl_path = Path(paths["stl_path"])
        inject = {"STEP_PATH": str(step_path), "STL_PATH": str(stl_path)}
        step_path.parent.mkdir(parents=True, exist_ok=True)
        stl_path.parent.mkdir(parents=True, exist_ok=True)

        # Build modification prompt and call LLM
        system = self._build_mod_system(context)
        user = self._build_mod_user(existing_code, modification)
        last_error = ""
        last_code = ""

        for attempt in range(1, max_attempts + 1):
            user_msg = self._build_mod_user(existing_code, modification, previous_code=last_code or None, previous_error=last_error or None)
            try:
                code = self._call_llm(system, user_msg, existing_code=existing_code)
            except RuntimeError as e:
                last_error = str(e)
                if attempt == max_attempts:
                    return ValidationResult(
                        passed=False,
                        geometry=None,
                        error=last_error,
                        errors=[last_error],
                    )
                continue
            last_code = code

            result = validate(code, expected_bbox=None, inject_namespace=inject, min_step_size_kb=1.0)
            if not result.passed:
                last_error = result.error or "; ".join(result.errors)
                continue

            # Save modified code: original_stem_modN.py
            mod_stem = f"{base_stem}_mod{mod_index}"
            out_py = self.generated_code_dir / f"{mod_stem}.py"
            out_py.write_text(code, encoding="utf-8")

            # Code already wrote STEP/STL via inject_namespace
            file_valid, solid_count, file_errors = validate_step_file(step_path, min_size_kb=1.0)
            if not file_valid and file_errors and solid_count < 1:
                last_error = "; ".join(file_errors)
                continue
            return result

        return ValidationResult(
            passed=False,
            geometry=None,
            error=last_error,
            errors=[last_error],
        )

    def _build_mod_system(self, context: dict) -> str:
        from .context_loader import get_mechanical_constants
        constants = get_mechanical_constants(context)
        constants_block = "\n".join(f"#   {k}: {v}" for k, v in sorted(constants.items()))
        return f"""You are a CadQuery expert. You will receive existing CadQuery code and a modification request.
Output ONLY a complete Python code block. No explanation, no markdown outside the block.

Rules:
- Preserve all existing geometry unless explicitly told to change it.
- Only add, remove, or resize the specific feature mentioned in the modification request.
- Keep the same ending: BBOX print and exporters.export for STEP and STL. Do not define STEP_PATH or STL_PATH.
- Use the same imports: import cadquery as cq, from cadquery import exporters.
- All dimensions in mm.

Mechanical constants (when relevant):
{constants_block}

Output the full modified script so it can be executed as-is. Variable 'result' must be the final solid/workplane."""

    def _build_mod_user(self, existing_code: str, modification: str,
                        previous_code: Optional[str] = None,
                        previous_error: Optional[str] = None) -> str:
        lines = [
            "Here is existing CadQuery code:",
            "```",
            existing_code[:6000] if len(existing_code) > 6000 else existing_code,
            "```",
            "",
            "Modify it to: " + modification,
            "",
            "Output modified code only. Keep the same BBOX print and export lines at the end.",
        ]
        if previous_error and previous_code:
            lines.append("")
            lines.append(f"Previous attempt failed: {previous_error}")
            lines.append("Previous code (excerpt):")
            lines.append("```")
            lines.append(previous_code[:3000] if len(previous_code) > 3000 else previous_code)
            lines.append("```")
        return "\n".join(lines)

    def _call_llm(self, system: str, user: str, existing_code: str = "") -> str:
        """Call LLM to apply modification.

        Uses Anthropic prompt caching on the existing_code block (when provided)
        so multi-attempt edits cost ~10% of normal input-token price on cache hits.
        Cache requires >= 1024 tokens; most generated scripts qualify.
        """
        api_key = llm_generator._get_api_key(self.repo_root)
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic package required. pip install anthropic") from None
        client = anthropic.Anthropic(api_key=api_key)
        model = "claude-sonnet-4-6"

        # Build user content — cache the code block to save credits on retries
        if existing_code and len(existing_code) > 500:
            code_block = f"Here is existing CadQuery code:\n```\n{existing_code[:6000]}\n```\n"
            modification_text = user.split("Modify it to:", 1)[-1].strip() if "Modify it to:" in user else user
            user_content = [
                {
                    "type": "text",
                    "text": code_block,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"Modify it to: {modification_text}\n\nOutput modified code only. Keep the same BBOX print and export lines at the end.",
                },
            ]
        else:
            user_content = user

        try:
            msg = client.messages.create(
                model=model,
                max_tokens=4000,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
        except Exception as e:
            # Retry without caching header on older SDK or model errors
            msg = client.messages.create(
                model=model,
                max_tokens=4000,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        text = ""
        for b in msg.content:
            if hasattr(b, "text"):
                text += b.text
        code = llm_generator._extract_code(text)
        if not code:
            raise RuntimeError("LLM did not return valid CadQuery code.")
        return code
