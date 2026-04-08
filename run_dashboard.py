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

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="ARIA-OS Dashboard")
    # Railway sets PORT; local dev uses ARIA_PORT or 7860
    default_port = int(os.environ.get("PORT", os.environ.get("ARIA_PORT", 7861)))
    p.add_argument("--port", type=int, default=default_port)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    url = f"http://localhost:{args.port}"
    print(f"ARIA-OS Dashboard  →  {url}")
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

    uvicorn.run(
        "dashboard.dashboard_server:app",
        host="0.0.0.0",
        port=args.port,
        reload=False,
    )
