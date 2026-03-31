"""Write session logs to sessions/."""
from pathlib import Path
from datetime import datetime


def _sessions_dir(repo_root=None):
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "sessions"


def log(session):
    """Append session success log to sessions/YYYY-MM-DD_aria-os-setup.md or create it."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = _sessions_dir() / (date_str + "_aria-os-setup.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "## Session " + datetime.now().isoformat(),
        "",
        "**Status:** Success",
        "**Goal:** " + session.get("goal", ""),
        "**Attempts:** " + str(session.get("attempts", 1)),
        "**Output STEP:** " + session.get("step_path", ""),
        "**Output STL:** " + session.get("stl_path", ""),
        "",
    ]
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(content + "\n".join(lines), encoding="utf-8")


def log_failure(session, diagnosis=""):
    """Append failure + diagnosis to session log."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = _sessions_dir() / (date_str + "_aria-os-setup.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "## Session " + datetime.now().isoformat(),
        "",
        "**Status:** Failure",
        "**Goal:** " + session.get("goal", ""),
        "**Attempts:** " + str(session.get("attempts", 3)),
        "**Diagnosis:** " + diagnosis,
        "",
    ]
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(content + "\n".join(lines), encoding="utf-8")
