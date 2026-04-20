"""
aria_os.self_extend.sandbox — Guardrail 1: isolated execution environment.

Each Sandbox gets:
  - A git worktree on a fresh branch (candidate code lives there, not on main)
  - A scratch directory for artifacts (outputs/.sandbox/<name>/scratch/)
  - A subprocess execution harness with timeout and env constraints
  - An artifact whitelist check post-run

Network isolation is STUBBED. Real enforcement requires OS-level sandboxing
(Windows Firewall rules, Linux netns/cgroups, or Vercel Sandbox / Firecracker).
That is intentionally out-of-scope for the hackathon build; the interface is
designed so that Sandbox.open(...) can be replaced by a remote executor later.

Usage::

    from aria_os.self_extend.sandbox import Sandbox

    with Sandbox.open("candidate_v1") as sbx:
        sbx.write("aria_os/generators/_cq_novel_lattice.py", code)
        result = sbx.run_python_module(
            "aria_os.generators._cq_novel_lattice",
            timeout=60,
        )
        # result.ok, result.stdout, result.stderr, result.artifacts
    # __exit__: worktree removed, scratch_dir kept by default
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Extensions considered safe engineering outputs.  Anything else is flagged.
ARTIFACT_WHITELIST: frozenset[str] = frozenset(
    {
        ".step",
        ".stl",
        ".png",
        ".svg",
        ".json",
        ".md",
        ".pdf",
        ".nc",
        ".gcode",
        ".kicad_pcb",
        ".kicad_sch",
        ".kicad_mod",
        ".kicad_sym",
        ".dxf",
        ".gbr",
        ".ngc",
        ".tap",
    }
)

#: Root directory for all sandbox state, relative to repo root.
_SANDBOX_ROOT_RELATIVE = Path("outputs") / ".sandbox"

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Result of a single :meth:`Sandbox.run_python_module` call."""

    ok: bool
    stdout: str
    stderr: str
    #: Paths (relative to scratch_dir) of artifacts produced in scratch_dir.
    artifacts: List[Path] = field(default_factory=list)
    #: Extensions found in scratch_dir that are NOT in ARTIFACT_WHITELIST.
    unknown_extensions: List[str] = field(default_factory=list)
    #: Human-readable reason when ok is False.
    failure_reason: str = ""

    def __repr__(self) -> str:  # pragma: no cover
        status = "OK" if self.ok else f"FAIL({self.failure_reason!r})"
        return (
            f"RunResult({status}, artifacts={len(self.artifacts)}, "
            f"unknown_ext={self.unknown_extensions})"
        )


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


class Sandbox:
    """
    Isolated execution environment for candidate engineering modules.

    Parameters
    ----------
    name:
        Human-readable label for this sandbox (e.g. ``"candidate_v1"``).
        Used as the sub-directory name under ``outputs/.sandbox/``.
    base_branch:
        Git branch to fork the worktree from.  Defaults to ``"main"``.
    keep_scratch:
        If True, the scratch directory is preserved after ``__exit__``.
        Defaults to True — callers usually want to inspect artifacts.
    block_network:
        Stub flag.  When True, a warning is logged that real network blocking
        is not implemented.  Production path: swap this class for a Vercel
        Sandbox / Firecracker executor.
    dry_run:
        If True, the sandbox is created (worktree + scratch) but
        :meth:`run_python_module` is a no-op that returns a successful empty
        result.  Useful in CI for structural tests that don't need execution.
    repo_root:
        Absolute path to the git repository root.  Auto-detected from this
        file's location if not provided.
    """

    def __init__(
        self,
        name: str,
        *,
        base_branch: str = "main",
        keep_scratch: bool = True,
        block_network: bool = True,
        dry_run: bool = False,
        repo_root: Optional[Path] = None,
    ) -> None:
        self.name = name
        self.base_branch = base_branch
        self.keep_scratch = keep_scratch
        self.block_network = block_network
        self.dry_run = dry_run

        # Resolve repo root: walk up from this file to find .git/
        if repo_root is not None:
            self.repo_root = Path(repo_root).resolve()
        else:
            self.repo_root = _find_repo_root(Path(__file__))

        # Unique identifier so concurrent sandboxes with the same name are safe
        self._uid = uuid.uuid4().hex[:8]
        self._branch_name = f"sandbox/{name}/{self._uid}"

        sandbox_base = self.repo_root / _SANDBOX_ROOT_RELATIVE / name
        self.worktree_dir: Path = sandbox_base / "worktree"
        self.scratch_dir: Path = sandbox_base / "scratch"

        self._entered = False

    # ------------------------------------------------------------------
    # Context manager interface
    # ------------------------------------------------------------------

    def __enter__(self) -> "Sandbox":
        self._setup()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._teardown()
        self._entered = False
        return False  # never suppress exceptions

    @classmethod
    @contextmanager
    def open(
        cls,
        name: str,
        *,
        base_branch: str = "main",
        keep_scratch: bool = True,
        block_network: bool = True,
        dry_run: bool = False,
        repo_root: Optional[Path] = None,
    ) -> Iterator["Sandbox"]:
        """
        Preferred entry point.  Equivalent to using the class as a context
        manager but more explicit::

            with Sandbox.open("candidate_v1") as sbx:
                ...
        """
        sbx = cls(
            name,
            base_branch=base_branch,
            keep_scratch=keep_scratch,
            block_network=block_network,
            dry_run=dry_run,
            repo_root=repo_root,
        )
        with sbx:
            yield sbx

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, relative_path: str, content: str) -> Path:
        """
        Write *content* to *relative_path* inside the worktree.

        The file is created with any necessary parent directories.

        Parameters
        ----------
        relative_path:
            Path relative to the worktree root, e.g.
            ``"aria_os/generators/_cq_novel_lattice.py"``.
        content:
            Text content to write.

        Returns
        -------
        Path
            Absolute path to the written file.
        """
        if not self._entered:
            raise RuntimeError("Sandbox.write() called outside context manager")
        dest = self.worktree_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return dest

    def run_python_module(
        self,
        module: str,
        *,
        timeout: int = 60,
        extra_env: Optional[dict] = None,
    ) -> RunResult:
        """
        Run ``python -m <module>`` inside the worktree with output captured.

        The process runs with:
        - ``PYTHONPATH`` set to the worktree root so the candidate module is
          importable.
        - ``ARIA_OUTPUT_DIR`` set to :attr:`scratch_dir` so generators write
          artifacts there and not into the live tree.
        - A minimal ``PATH`` (Python executable's directory + system bin).
        - ``PYTHONDONTWRITEBYTECODE=1`` to avoid bytecode pollution.

        Parameters
        ----------
        module:
            Dotted module path, e.g.
            ``"aria_os.generators._cq_novel_lattice"``.
        timeout:
            Wall-clock seconds before the subprocess is killed.
        extra_env:
            Additional environment variables merged on top of the constructed
            env.  Useful for passing generator parameters.

        Returns
        -------
        RunResult
            Contains ``ok``, ``stdout``, ``stderr``, ``artifacts``, and
            ``unknown_extensions``.
        """
        if not self._entered:
            raise RuntimeError(
                "Sandbox.run_python_module() called outside context manager"
            )

        # Dry-run: skip execution
        if self.dry_run:
            return RunResult(ok=True, stdout="", stderr="[dry-run: execution skipped]")

        # Network stub warning
        if self.block_network:
            # TODO(guardrail-1): implement real network isolation.
            # On Linux: unshare(CLONE_NEWNET) or nsjail, or a Firecracker microVM.
            # On Windows: Windows Filtering Platform (WFP) rules per process.
            # For the hackathon build, we document the intent and move on.
            _warn_network_not_blocked()

        env = _build_env(
            worktree_dir=self.worktree_dir,
            scratch_dir=self.scratch_dir,
            extra_env=extra_env or {},
        )

        try:
            proc = subprocess.run(
                [sys.executable, "-m", module],
                cwd=str(self.worktree_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            # Collect whatever partial output was captured
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return RunResult(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                failure_reason=f"timeout after {timeout}s",
            )
        except Exception as exc:  # noqa: BLE001
            return RunResult(
                ok=False,
                stdout="",
                stderr=str(exc),
                failure_reason=f"subprocess launch failed: {exc}",
            )

        ok = proc.returncode == 0
        artifacts, unknown = _scan_artifacts(self.scratch_dir)

        return RunResult(
            ok=ok,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            unknown_extensions=unknown,
            failure_reason="" if ok else f"exit code {proc.returncode}",
        )

    def run_python_file(
        self,
        relative_path: str,
        *,
        timeout: int = 60,
        extra_env: Optional[dict] = None,
    ) -> RunResult:
        """
        Run a Python *file* (not a module) inside the worktree.

        Convenience alternative to :meth:`run_python_module` when the
        candidate is a standalone script rather than a proper package module.
        """
        if not self._entered:
            raise RuntimeError(
                "Sandbox.run_python_file() called outside context manager"
            )

        if self.dry_run:
            return RunResult(ok=True, stdout="", stderr="[dry-run: execution skipped]")

        if self.block_network:
            _warn_network_not_blocked()

        env = _build_env(
            worktree_dir=self.worktree_dir,
            scratch_dir=self.scratch_dir,
            extra_env=extra_env or {},
        )
        script_path = self.worktree_dir / relative_path

        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(self.worktree_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return RunResult(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                failure_reason=f"timeout after {timeout}s",
            )
        except Exception as exc:  # noqa: BLE001
            return RunResult(
                ok=False,
                stdout="",
                stderr=str(exc),
                failure_reason=f"subprocess launch failed: {exc}",
            )

        ok = proc.returncode == 0
        artifacts, unknown = _scan_artifacts(self.scratch_dir)

        return RunResult(
            ok=ok,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            unknown_extensions=unknown,
            failure_reason="" if ok else f"exit code {proc.returncode}",
        )

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        """Create the worktree branch and scratch directory."""
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

        # Remove stale worktree directory if present (e.g. previous run crashed)
        if self.worktree_dir.exists():
            _git(
                ["worktree", "remove", "--force", str(self.worktree_dir)],
                cwd=self.repo_root,
                check=False,
            )
            if self.worktree_dir.exists():
                shutil.rmtree(self.worktree_dir)

        # git worktree add -b <branch> <path> <base_branch>
        _git(
            [
                "worktree",
                "add",
                "-b",
                self._branch_name,
                str(self.worktree_dir),
                self.base_branch,
            ],
            cwd=self.repo_root,
        )

    def _teardown(self) -> None:
        """Remove the worktree and its branch; optionally preserve scratch."""
        # Remove worktree (removes the working tree + .git files inside it)
        if self.worktree_dir.exists():
            _git(
                ["worktree", "remove", "--force", str(self.worktree_dir)],
                cwd=self.repo_root,
                check=False,
            )
            # Belt-and-suspenders: rmtree if git didn't fully clean up
            if self.worktree_dir.exists():
                shutil.rmtree(self.worktree_dir, ignore_errors=True)

        # Delete the ephemeral branch
        _git(
            ["branch", "-D", self._branch_name],
            cwd=self.repo_root,
            check=False,
        )

        # Optionally clean up scratch
        if not self.keep_scratch and self.scratch_dir.exists():
            shutil.rmtree(self.scratch_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Walk upward from *start* until we find a directory containing ``.git``."""
    current = start.resolve()
    for _ in range(20):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        f"Could not find git repository root starting from {start}. "
        "Pass repo_root= explicitly."
    )


def _git(
    args: list,
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command, optionally raising on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _build_env(
    *,
    worktree_dir: Path,
    scratch_dir: Path,
    extra_env: dict,
) -> dict:
    """
    Build a constrained environment dict for the subprocess.

    Key choices:
    - ``ARIA_OUTPUT_DIR``: generators that honour this env var will write to
      scratch_dir.  Generators that ignore it may still write elsewhere — this
      is a convention, not a hard sandbox.  Future work: run under nsjail or
      Firecracker for hard filesystem isolation.
    - ``PYTHONPATH``: worktree root is prepended so the candidate module is
      importable without installation.
    - ``PATH``: kept minimal.  On Windows this still includes system32 because
      Python subprocess needs it to find DLLs.
    - ``HOME``/``USERPROFILE``: overridden to scratch_dir so any tool that
      auto-writes to ~ ends up in the sandbox.
    """
    python_bin_dir = str(Path(sys.executable).parent)

    # Minimal PATH: Python's bin dir + OS essentials
    if sys.platform == "win32":
        system32 = os.environ.get("SystemRoot", "C:\\Windows") + "\\System32"
        minimal_path = f"{python_bin_dir};{system32}"
    else:
        minimal_path = f"{python_bin_dir}:/usr/bin:/bin"

    # PYTHONPATH: worktree root prepended to existing path so candidate imports work
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    new_pythonpath = (
        str(worktree_dir) + os.pathsep + existing_pythonpath
        if existing_pythonpath
        else str(worktree_dir)
    )

    env = {
        # Core Python
        "PYTHONPATH": new_pythonpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        # Aria convention: all generators write artifacts here
        "ARIA_OUTPUT_DIR": str(scratch_dir),
        # Constrain home-like dirs to scratch so tools don't pollute user home
        "HOME": str(scratch_dir),
        "USERPROFILE": str(scratch_dir),
        "TEMP": str(scratch_dir),
        "TMP": str(scratch_dir),
        # Minimal path
        "PATH": minimal_path,
        # Preserve proxy/locale so network calls fail gracefully rather than crash
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        # Windows subprocess essentials
        "SYSTEMROOT": os.environ.get("SystemRoot", os.environ.get("SYSTEMROOT", "")),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        # Preserve ANTHROPIC_API_KEY etc. so generators can call LLMs if needed
        # (callers can override via extra_env to strip these)
        **{k: v for k, v in os.environ.items() if k.endswith("_API_KEY")},
    }

    env.update(extra_env)
    return env


def _scan_artifacts(scratch_dir: Path) -> tuple[list[Path], list[str]]:
    """
    Scan *scratch_dir* recursively.

    Returns
    -------
    artifacts:
        Relative paths of all files found.
    unknown_extensions:
        Deduplicated list of file extensions NOT in :data:`ARTIFACT_WHITELIST`.
    """
    artifacts: list[Path] = []
    unknown: set[str] = set()

    if not scratch_dir.exists():
        return artifacts, []

    for p in scratch_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(scratch_dir)
            artifacts.append(rel)
            ext = p.suffix.lower()
            if ext and ext not in ARTIFACT_WHITELIST:
                unknown.add(ext)

    return artifacts, sorted(unknown)


def _warn_network_not_blocked() -> None:
    """Emit a single warning that network blocking is not enforced."""
    import warnings

    warnings.warn(
        "Sandbox block_network=True is a STUB. "
        "Network access is NOT actually blocked in this build. "
        "For real isolation use Vercel Sandbox, Firecracker, or nsjail. "
        "See aria_os/self_extend/sandbox.py TODO comments.",
        stacklevel=3,
        category=UserWarning,
    )
