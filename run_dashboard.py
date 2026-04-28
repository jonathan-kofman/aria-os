"""
run_dashboard.py
----------------
Start the ARIA-OS Dashboard UI.

Usage:
    python run_dashboard.py           # default port 7860
    python run_dashboard.py --port 8080
"""
import io
import os
import sys
import webbrowser

# Force UTF-8 output on Windows so Unicode prints don't crash
if sys.stdout and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def _load_repo_dotenv() -> None:
    """Load REPO_ROOT/.env into os.environ so MILLFORGE_* and keys apply without a shell export."""
    from pathlib import Path

    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        os.environ[key] = val


if __name__ == "__main__":
    _load_repo_dotenv()
    import argparse
    p = argparse.ArgumentParser(description="ARIA-OS Dashboard")
    # Railway sets PORT; local dev uses ARIA_PORT or 7860
    default_port = int(os.environ.get("PORT", os.environ.get("ARIA_PORT", 7861)))
    p.add_argument("--port", type=int, default=default_port)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--no-reload", action="store_true",
                    help="disable hot-reload (default: on for local dev, "
                         "off when PORT env is set i.e. on Railway)")
    args = p.parse_args()

    url = f"http://localhost:{args.port}"
    # Default reload behaviour: ON for local dev (no PORT env set), OFF
    # in production deployments where Railway/Heroku set PORT. Explicit
    # --no-reload always wins. This is the autonomy-first fix for the
    # "did you restart?" loop — source changes to aria_os/, dashboard/,
    # cad-plugins/ trigger an automatic uvicorn reload so SW addin
    # rebuilds + planner-side fixes land without a manual bounce.
    is_production = bool(os.environ.get("PORT"))
    reload_on = (not args.no_reload) and (not is_production)
    print(f"ARIA-OS Dashboard  →  {url}"
            f"{'  [hot-reload ON]' if reload_on else ''}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        import threading
        def _open():
            import time; time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: pip install uvicorn[standard] fastapi")
        sys.exit(1)

    # When reload=True uvicorn watches files for changes. We point it at
    # the directories that actually contain source we edit so a touch on
    # a Python file in any of them triggers a re-import + bounce.
    reload_dirs = None
    if reload_on:
        from pathlib import Path
        repo = Path(__file__).resolve().parent
        candidates = [repo / d for d in
                       ("aria_os", "dashboard", "cad-plugins")]
        reload_dirs = [str(d) for d in candidates if d.is_dir()]

    uvicorn.run(
        "dashboard.dashboard_server:app",
        host="0.0.0.0",
        port=args.port,
        reload=reload_on,
        reload_dirs=reload_dirs,
        # Keep noise down — without log_level set, uvicorn's reload
        # watcher prints every file scan.
        log_level="info",
    )
