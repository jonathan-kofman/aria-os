"""
aria_offline_mode.py — ARIA Offline Mode & Session Queue
=========================================================
Handles the case where Firebase is unavailable (no WiFi, bad signal,
firewall). Queues sessions locally and uploads them when connection
returns. Add this to aria_dashboard.py imports.

Usage:
    from aria_offline_mode import (
        OfflineQueue, check_connectivity, render_offline_status,
        push_with_fallback
    )

The session recorder in the Test Session tab should call
push_with_fallback() instead of directly calling Firebase.
This handles the queue automatically.
"""

import json
import os
import time
import threading
from datetime import datetime
from pathlib import Path

QUEUE_DIR  = Path("offline_queue")
QUEUE_FILE = QUEUE_DIR / "pending_sessions.json"
SYNC_LOG   = QUEUE_DIR / "sync_log.json"
RETRY_INTERVAL_S = 30  # retry every 30s when online


def _ensure_queue_dir():
    QUEUE_DIR.mkdir(exist_ok=True)


def _load_queue() -> list:
    _ensure_queue_dir()
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_queue(items: list):
    _ensure_queue_dir()
    QUEUE_FILE.write_text(json.dumps(items, indent=2))


def _append_sync_log(entry: dict):
    _ensure_queue_dir()
    log = []
    if SYNC_LOG.exists():
        try:
            log = json.loads(SYNC_LOG.read_text())
        except Exception:
            pass
    log.append(entry)
    log = log[-500:]  # keep last 500 entries
    SYNC_LOG.write_text(json.dumps(log, indent=2))


def check_connectivity(timeout_s: float = 2.0) -> bool:
    """
    Quick connectivity check. Tries to reach Firebase hostname.
    Returns True if online, False if offline.
    """
    import socket
    try:
        socket.setdefaulttimeout(timeout_s)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(
            ("firestore.googleapis.com", 443))
        return True
    except Exception:
        return False


class OfflineQueue:
    """
    Thread-safe session queue with automatic retry.
    Queues sessions to disk when offline, pushes when connectivity returns.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._running = False
        self._thread  = None

    def enqueue(self, session_doc: dict, gym_id: str = "gym_001") -> bool:
        """
        Add a session to the offline queue.
        Returns True if queued successfully.
        """
        entry = {
            'queued_at':  datetime.now().isoformat(),
            'gym_id':     gym_id,
            'session':    session_doc,
            'attempts':   0,
            'last_error': '',
        }
        with self._lock:
            queue = _load_queue()
            queue.append(entry)
            _save_queue(queue)
        return True

    def queue_size(self) -> int:
        return len(_load_queue())

    def push_now(self) -> dict:
        """
        Attempt to push all queued sessions to Firebase right now.
        Returns {'pushed': int, 'failed': int, 'remaining': int}
        """
        pushed = 0; failed = 0
        with self._lock:
            queue = _load_queue()
            remaining = []
            for entry in queue:
                success, error = self._push_one(entry)
                if success:
                    pushed += 1
                    _append_sync_log({
                        'time': datetime.now().isoformat(),
                        'action': 'pushed',
                        'session_name': entry['session'].get('name', '?'),
                    })
                else:
                    entry['attempts'] += 1
                    entry['last_error'] = error
                    if entry['attempts'] < 5:
                        remaining.append(entry)
                    else:
                        _append_sync_log({
                            'time': datetime.now().isoformat(),
                            'action': 'dropped_max_retries',
                            'session_name': entry['session'].get('name', '?'),
                            'error': error,
                        })
                        failed += 1
            _save_queue(remaining)
        return {'pushed': pushed, 'failed': failed, 'remaining': len(remaining)}

    def _push_one(self, entry: dict) -> tuple[bool, str]:
        """Push a single queued session to Firebase. Returns (success, error_msg)."""
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore as fb_firestore
            if not firebase_admin._apps:
                if os.path.exists("serviceAccountKey.json"):
                    cred = credentials.Certificate("serviceAccountKey.json")
                    firebase_admin.initialize_app(cred)
                else:
                    return False, "serviceAccountKey.json not found"
            db  = fb_firestore.client()
            gym = entry.get('gym_id', 'gym_001')
            ref = db.collection("gyms").document(gym).collection("sessions").document()
            ref.set({**entry['session'], 'sessionId': ref.id,
                     'offline_queued_at': entry['queued_at']})
            return True, ""
        except ImportError:
            return False, "firebase_admin not installed"
        except Exception as e:
            return False, str(e)

    def start_background_retry(self):
        """Start background thread that retries every RETRY_INTERVAL_S."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._retry_loop, daemon=True)
        self._thread.start()

    def stop_background_retry(self):
        self._running = False

    def _retry_loop(self):
        while self._running:
            time.sleep(RETRY_INTERVAL_S)
            if self.queue_size() > 0 and check_connectivity():
                self.push_now()


# ── Global singleton ──────────────────────────────────────────────────────────
_queue = OfflineQueue()


def push_with_fallback(session_doc: dict, gym_id: str = "gym_001") -> dict:
    """
    Try to push session to Firebase immediately.
    If offline or Firebase unavailable, queue it for later.
    Returns {'status': 'pushed'|'queued'|'saved_local', 'message': str}
    """
    # Start background retry thread if not already running
    _queue.start_background_retry()

    # Try direct push first
    if check_connectivity(timeout_s=1.5):
        success, error = _queue._push_one({'gym_id': gym_id, 'session': session_doc})
        if success:
            return {'status': 'pushed', 'message': 'Session pushed to Firebase.'}
        else:
            # Connected but Firebase failed — queue it
            _queue.enqueue(session_doc, gym_id)
            return {'status': 'queued',
                    'message': f'Firebase error ({error}). Session queued for retry.'}
    else:
        # Offline — queue it
        _queue.enqueue(session_doc, gym_id)
        return {
            'status': 'queued',
            'message': f'Offline — session queued locally ({_queue.queue_size()} pending). '
                       'Will upload automatically when WiFi returns.'
        }


def render_offline_status():
    """
    Render a compact offline status widget in Streamlit.
    Call this from the Test Session tab sidebar.
    """
    import streamlit as st

    online = check_connectivity(timeout_s=1.0)
    q_size = _queue.queue_size()

    status_color = "🟢" if online else "🔴"
    status_text  = "Online" if online else "Offline"
    st.markdown(f"**Connectivity:** {status_color} {status_text}")

    if q_size > 0:
        st.warning(f"📦 {q_size} session(s) queued for upload")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Upload now", key="offline_push_now"):
                with st.spinner("Uploading queued sessions..."):
                    result = _queue.push_now()
                if result['pushed'] > 0:
                    st.success(f"Uploaded {result['pushed']} session(s).")
                if result['failed'] > 0:
                    st.error(f"{result['failed']} session(s) failed after max retries.")
                if result['remaining'] > 0:
                    st.info(f"{result['remaining']} session(s) still queued.")
        with col2:
            if st.button("View queue", key="offline_view_queue"):
                queue = _load_queue()
                for i, entry in enumerate(queue):
                    st.write(f"{i+1}. {entry['session'].get('name','?')} "
                             f"(queued {entry['queued_at'][:19]}, "
                             f"attempts={entry['attempts']})")
    elif online:
        st.caption("All sessions synced ✓")
    else:
        st.caption("No pending sessions — data will be saved locally.")

    # Show sync log
    if SYNC_LOG.exists():
        with st.expander("Sync history"):
            try:
                log = json.loads(SYNC_LOG.read_text())
                for entry in reversed(log[-10:]):
                    icon = "✅" if entry['action'] == 'pushed' else "⚠️"
                    st.write(f"{icon} {entry['time'][:19]}  {entry['action']}  "
                             f"{entry.get('session_name','?')}")
            except Exception:
                st.write("Could not read sync log.")
