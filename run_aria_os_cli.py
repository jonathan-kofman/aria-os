"""
ARIA-OS CLI wrapper.

Your repo already has the implementation modules (planner/generator/modifier/assembler/validator),
but the checked-in `run_aria_os.py` appears to contain null bytes that prevent Python from running it.

This wrapper provides the same workflow you listed:
  - Generate from description
  - Modify an existing generated part (.py in outputs/cad/generated_code/)
  - Assemble from assembly_configs/*.json
  - List/validate exported STEP files
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aria_os.orchestrator import run as orchestrator_run
from aria_os.assembler import Assembler, AssemblyPart
from aria_os.modifier import PartModifier
from aria_os.validator import validate_step_file


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def cmd_generate(goal: str, max_attempts: int) -> int:
    session = orchestrator_run(goal, repo_root=_repo_root(), max_attempts=max_attempts)
    # Keep output user-friendly: print key paths if present.
    step_path = session.get("step_path") if isinstance(session, dict) else None
    stl_path = session.get("stl_path") if isinstance(session, dict) else None
    if step_path:
        print(f"STEP: {step_path}")
    if stl_path:
        print(f"STL:  {stl_path}")
    return 0 if session.get("error", "") == "" else 1


def cmd_modify(base_part_py: str, modification: str, max_attempts: int) -> int:
    modifier = PartModifier(repo_root=_repo_root())
    result = modifier.modify(base_part_py, modification, max_attempts=max_attempts)
    if result.passed:
        print("Modify: PASSED")
        return 0
    print("Modify: FAILED")
    if result.error:
        print(f"Error: {result.error}")
    return 1


def cmd_assemble(assembly_config_path: str) -> int:
    cfg_path = Path(assembly_config_path)
    if not cfg_path.is_absolute():
        cfg_path = _repo_root() / cfg_path
    if not cfg_path.exists():
        print(f"Assembly config not found: {cfg_path}")
        return 1

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    name = cfg.get("name") or cfg_path.stem
    constraints = cfg.get("constraints", None)

    parts_in = cfg.get("parts", [])
    parts: list[AssemblyPart] = []
    for p in parts_in:
        step_path = p["step_path"]
        step_abs = Path(step_path)
        if not step_abs.is_absolute():
            step_abs = _repo_root() / step_abs

        parts.append(
            AssemblyPart(
                step_path=str(step_abs),
                position=(p["position"][0], p["position"][1], p["position"][2]),
                rotation=(p["rotation"][0], p["rotation"][1], p["rotation"][2]),
                name=p["name"],
            )
        )

    assembler = Assembler(repo_root=_repo_root())
    assembler.assemble(parts, name=name, constraints=constraints, context=None)
    print("Assemble: DONE (exports written under outputs/cad/step and outputs/cad/stl)")
    return 0


def _list_steps(only_failed: bool = False) -> int:
    step_dir = _repo_root() / "outputs" / "cad" / "step"
    if not step_dir.exists():
        print(f"No STEP directory: {step_dir}")
        return 1

    step_files = sorted(step_dir.glob("*.step"))
    if not step_files:
        print(f"No .step files found in {step_dir}")
        return 1

    any_failed = False
    for p in step_files:
        file_valid, solid_count, errors = validate_step_file(p, min_size_kb=1.0)
        size_kb = p.stat().st_size / 1024
        ok = file_valid and solid_count >= 1
        if only_failed and ok:
            continue
        any_failed = any_failed or (not ok)
        status = "OK" if ok else "FAIL"
        msg = errors[0] if errors else ""
        print(f"{status:4} {p.name:55} {size_kb:8.1f} KB  solids={solid_count}  {msg}")

    return 1 if any_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARIA-OS CLI wrapper")
    parser.add_argument(
        "goal",
        nargs="?",
        help='Generate: "describe the part you want" (e.g. aria housing shell) ',
    )
    parser.add_argument(
        "--modify",
        nargs=2,
        metavar=("BASE_PART_PY", "MODIFICATION"),
        help='Modify: --modify outputs/cad/generated_code/<file>.py "what to change"',
    )
    parser.add_argument(
        "--assemble",
        metavar="ASSEMBLY_CONFIG_JSON",
        help="Assemble: --assemble assembly_configs/<name>.json",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all exported STEP files with validation status.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Re-validate all exported STEP files. Exits 1 if any fail.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Retry count for generation/modification that uses LLMs.",
    )
    args = parser.parse_args(argv)

    # --list / --validate
    if args.list or args.validate:
        return _list_steps(only_failed=False if args.list else False)

    # --assemble
    if args.assemble:
        return cmd_assemble(args.assemble)

    # --modify
    if args.modify:
        base_part_py, modification = args.modify
        return cmd_modify(base_part_py, modification, max_attempts=args.max_attempts)

    # generate
    if not args.goal:
        parser.print_help()
        return 2
    return cmd_generate(args.goal, max_attempts=args.max_attempts)


if __name__ == "__main__":
    raise SystemExit(main())

