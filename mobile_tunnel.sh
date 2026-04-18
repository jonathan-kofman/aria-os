#!/usr/bin/env bash
# mobile_tunnel.sh
# ------------------------------------------------------------
# Start the ARIA-OS dashboard and expose it to a mobile phone.
#
# Detection order:
#   1. tailscale  (preferred — long-lived, tailnet-only, no public exposure)
#   2. cloudflared (quick tunnel, no login required)
#   3. ngrok      (requires auth token)
#   4. LAN fallback — prints your local IP for same-wifi browsing
#
# Works on Git Bash / MSYS2 on Windows, and on Linux/macOS.
# ------------------------------------------------------------
set -u

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PORT="${ARIA_PORT:-7861}"

# Path translator: when invoking a Windows .exe (Git Bash or WSL), arguments
# need to be Windows paths. WSL paths like /mnt/c/... or Git Bash paths like
# /c/... are not understood by Windows binaries.
to_winpath() {
    local p="$1"
    # WSL: use wslpath if present
    if command -v wslpath >/dev/null 2>&1; then
        wslpath -w "$p" 2>/dev/null && return
    fi
    # Git Bash / MSYS: cygpath if present
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$p" 2>/dev/null && return
    fi
    # Manual fallback: /mnt/c/foo -> C:\foo  ;  /c/foo -> C:\foo
    if [[ "$p" =~ ^/mnt/([a-z])/(.*) ]]; then
        echo "${BASH_REMATCH[1]^^}:\\${BASH_REMATCH[2]//\//\\}"
        return
    fi
    if [[ "$p" =~ ^/([a-z])/(.*) ]]; then
        echo "${BASH_REMATCH[1]^^}:\\${BASH_REMATCH[2]//\//\\}"
        return
    fi
    echo "$p"
}

# Prefer the Windows miniforge python when present (where fastapi/cadquery
# are installed). Test both Git Bash (/c/...) and WSL (/mnt/c/...) layouts.
PYTHON="${ARIA_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in \
        "/c/Users/jonko/miniforge3/python.exe" \
        "/mnt/c/Users/jonko/miniforge3/python.exe" \
        "$HOME/miniforge3/python.exe" \
        "$HOME/miniforge3/python" \
        "$HOME/anaconda3/python.exe" \
        "$HOME/anaconda3/python" ; do
        if [[ -x "$candidate" ]]; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [[ -z "$PYTHON" ]]; then
    if command -v python >/dev/null 2>&1; then
        PYTHON="$(command -v python)"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON="$(command -v python3)"
    else
        echo "ERROR: no python interpreter found" >&2
        exit 1
    fi
fi
# Verify the chosen interpreter has fastapi (the dashboard requires it).
if ! "$PYTHON" -c "import fastapi" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON has no fastapi module." >&2
    echo "  Install in this env:  $PYTHON -m pip install fastapi uvicorn[standard] python-multipart" >&2
    echo "  Or set ARIA_PYTHON to a Python that has it:" >&2
    echo "    ARIA_PYTHON=/path/to/python bash mobile_tunnel.sh" >&2
    if [[ -n "${ARIA_PYTHON:-}" ]] && [[ -x "${ARIA_PYTHON}" ]]; then
        PYTHON="${ARIA_PYTHON}"
    else
        exit 1
    fi
fi
echo "[tunnel] python: $PYTHON"

have() { command -v "$1" >/dev/null 2>&1; }

detect_tunnel() {
    if have tailscale; then echo "tailscale"; return; fi
    if have cloudflared; then echo "cloudflared"; return; fi
    if have ngrok; then echo "ngrok"; return; fi
    echo "lan"
}

render_qr() {
    local url="$1"
    # Try the `qrcode` python module first (bundled with many envs).
    # IMPORTANT: force UTF-8 stdout so Windows cp1252 console doesn't crash
    # on the Unicode block characters used by the ASCII QR.
    if "$PYTHON" -c "import qrcode" >/dev/null 2>&1; then
        PYTHONIOENCODING=utf-8 "$PYTHON" - <<PY
import sys, io
# Re-bind stdout to UTF-8 in case the parent terminal is cp1252 (Windows)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import qrcode
q = qrcode.QRCode(border=1)
q.add_data("$url")
q.make()
try:
    q.print_ascii(invert=True)
except UnicodeEncodeError:
    # Fallback: ASCII-only QR using two-character cell representation
    matrix = q.get_matrix()
    for row in matrix:
        print("".join("##" if cell else "  " for cell in row))
PY
        return
    fi
    # Try `segno` as a second option.
    if "$PYTHON" -c "import segno" >/dev/null 2>&1; then
        "$PYTHON" - <<PY
import segno
segno.make("$url").terminal(compact=True)
PY
        return
    fi
    echo "(install python qrcode module for a scannable QR: pip install qrcode)"
    echo
    echo "URL: $url"
}

lan_ip() {
    # Windows Git Bash has ipconfig; Linux/macOS have ip/ifconfig.
    if have ipconfig.exe; then
        ipconfig.exe \
            | tr -d '\r' \
            | awk '/IPv4 Address/ { for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+\./) { print $i; exit } }'
        return
    fi
    if have hostname; then
        hostname -I 2>/dev/null | awk '{print $1; exit}' && return
    fi
    if have ifconfig; then
        ifconfig | awk '/inet / && $2 != "127.0.0.1" { print $2; exit }'
    fi
}

start_dashboard() {
    cd "$REPO_ROOT" || exit 1
    echo "[tunnel] starting ARIA-OS dashboard on :$PORT ..."
    local logfile="$REPO_ROOT/outputs/dashboard_server.log"
    mkdir -p "$REPO_ROOT/outputs"
    # Translate the script path to Windows form when running a .exe Python.
    # Without this, /mnt/c/... gets read as C:\mnt\c\... by Windows Python.
    local script_arg="$REPO_ROOT/dashboard/dashboard_server.py"
    if [[ "$PYTHON" == *.exe ]]; then
        script_arg="$(to_winpath "$script_arg")"
    fi
    ARIA_PORT="$PORT" "$PYTHON" "$script_arg" \
        > "$logfile" 2>&1 &
    DASH_PID=$!
    echo "[tunnel] dashboard PID=$DASH_PID  log=$logfile"
    # Wait up to 30s for the port to come up.
    for _ in $(seq 1 60); do
        if "$PYTHON" -c "import socket,sys; s=socket.socket(); \
            s.settimeout(0.4); \
            sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT)) == 0 else 1)" 2>/dev/null; then
            echo "[tunnel] dashboard listening on :$PORT"
            return 0
        fi
        sleep 0.5
    done
    echo "[tunnel] dashboard did NOT come up within 30s. See log: $logfile" >&2
    return 1
}

cleanup() {
    if [[ -n "${TUNNEL_PID:-}" ]]; then
        kill "$TUNNEL_PID" 2>/dev/null || true
    fi
    if [[ -n "${DASH_PID:-}" ]]; then
        kill "$DASH_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

main() {
    local tool
    tool="$(detect_tunnel)"
    echo "[tunnel] tool detected: $tool"

    start_dashboard || exit 1

    local url=""
    case "$tool" in
        tailscale)
            # Magic DNS / tailscale-serve covers the last mile.
            local ts_ip
            ts_ip="$(tailscale ip -4 2>/dev/null | head -n1)"
            if [[ -z "$ts_ip" ]]; then
                echo "[tunnel] tailscale installed but not logged in. Run: tailscale up"
                url="http://localhost:$PORT"
            else
                url="http://$ts_ip:$PORT"
            fi
            ;;
        cloudflared)
            echo "[tunnel] starting cloudflared quick tunnel ..."
            local cf_log="$REPO_ROOT/outputs/cloudflared.log"
            cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate \
                > "$cf_log" 2>&1 &
            TUNNEL_PID=$!
            # Parse the trycloudflare URL from the log (up to ~30s).
            for _ in $(seq 1 60); do
                url="$(grep -Eo 'https://[a-z0-9.-]+trycloudflare\.com' "$cf_log" \
                    | head -n1 || true)"
                [[ -n "$url" ]] && break
                sleep 0.5
            done
            [[ -z "$url" ]] && { echo "[tunnel] could not parse cloudflared URL"; cat "$cf_log" | tail -n 20; url="http://localhost:$PORT"; }
            ;;
        ngrok)
            echo "[tunnel] starting ngrok ..."
            ngrok http "$PORT" > /dev/null 2>&1 &
            TUNNEL_PID=$!
            # Probe ngrok's local API for the public URL.
            for _ in $(seq 1 20); do
                url="$("$PYTHON" -c "
import json, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=1) as r:
        data = json.load(r)
    for t in data.get('tunnels', []):
        if t.get('proto') == 'https':
            print(t['public_url']); break
except Exception:
    pass
" 2>/dev/null)"
                [[ -n "$url" ]] && break
                sleep 0.5
            done
            [[ -z "$url" ]] && { echo "[tunnel] could not read ngrok URL. Is ngrok authenticated?"; url="http://localhost:$PORT"; }
            ;;
        lan|*)
            local ip
            ip="$(lan_ip)"
            if [[ -n "$ip" ]]; then
                url="http://$ip:$PORT"
                echo "[tunnel] no tunnel tool found. Using LAN IP."
                echo "[tunnel] (install tailscale / cloudflared / ngrok for remote access)"
            else
                url="http://localhost:$PORT"
                echo "[tunnel] no tunnel tool AND no LAN IP detected. Phone must be on this machine."
            fi
            ;;
    esac

    echo
    echo "============================================================"
    echo " ARIA-OS Dashboard is live at:"
    echo "   $url"
    echo "============================================================"
    echo
    render_qr "$url"
    echo
    echo "[tunnel] Ctrl-C to stop. Dashboard log: outputs/dashboard_server.log"

    # Wait on the dashboard process so the trap can clean up the tunnel too.
    wait "$DASH_PID"
}

main "$@"
