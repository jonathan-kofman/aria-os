#!/usr/bin/env python3
"""Run the aria_os pipeline directly (no subprocess) and capture all print output."""
import sys
import io
import time
import traceback
from pathlib import Path

# Redirect stdout/stderr to file AND terminal
out_path = Path(__file__).parent / "aria_run_output.txt"
_orig_stdout = sys.stdout
_orig_stderr = sys.stderr

class Tee:
    def __init__(self, fh, orig):
        self.fh = fh
        self.orig = orig
    def write(self, data):
        self.fh.write(data)
        self.fh.flush()
        self.orig.write(data)
        self.orig.flush()
    def flush(self):
        self.fh.flush()
        self.orig.flush()
    def isatty(self):
        return False
    def fileno(self):
        return self.orig.fileno()

fh = open(out_path, "w", encoding="utf-8", errors="replace")
sys.stdout = Tee(fh, _orig_stdout)
sys.stderr = Tee(fh, _orig_stderr)

try:
    sys.argv = [
        "run_aria_os.py",
        "aluminium flanged pipe coupling 60mm OD 40mm bore 4 bolt holes on 80mm PCD"
    ]
    ROOT = Path(__file__).resolve().parent
    sys.path.insert(0, str(ROOT))
    from aria_os import run
    from aria_os.context_loader import load_context

    goal = sys.argv[1]
    print(f"Starting pipeline for: {goal}")
    print(f"Time: {time.strftime('%H:%M:%S')}")

    session = run(goal, repo_root=ROOT, preview=False, agent_mode=None, max_agent_iterations=5)

    print(f"\nDone at: {time.strftime('%H:%M:%S')}")
    print(f"Session keys: {list(session.keys()) if isinstance(session, dict) else 'not a dict'}")

except SystemExit as e:
    print(f"\n--- SystemExit: {e} ---")
except KeyboardInterrupt:
    print("\n--- KeyboardInterrupt ---")
except Exception as e:
    print(f"\n--- ERROR: {type(e).__name__}: {e} ---")
    traceback.print_exc()
finally:
    sys.stdout = _orig_stdout
    sys.stderr = _orig_stderr
    fh.close()
    print(f"Output written to: {out_path}")
