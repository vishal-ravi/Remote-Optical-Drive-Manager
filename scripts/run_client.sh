#!/usr/bin/env bash
# Bootstrap the client virtualenv and launch the PySide6 GUI.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "[rodm] refusing to start the GUI as root." >&2
    echo "       The client is an ordinary user application (SSH + REST client);" >&2
    echo "       running it as root only breaks Qt (no session DBus, no xkb data," >&2
    echo "       wrong HOME, so your profiles are invisible)." >&2
    echo "" >&2
    echo "       Run it as your desktop user:   ./scripts/run_client.sh" >&2
    echo "       sudo is only needed on the REMOTE host, e.g." >&2
    echo "           ./scripts/install_agent.sh user@host --install-packages --root" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
REQ="$ROOT/requirements-client.txt"

if [ ! -x "$VENV/bin/python" ]; then
    echo "[rodm] creating virtualenv at $VENV"
    python3 -m venv --without-pip --system-site-packages "$VENV"
fi

if ! "$VENV/bin/python" -c "import PySide6, paramiko, requests" 2>/dev/null; then
    echo "[rodm] installing client dependencies"
    if ! "$VENV/bin/pip" --version >/dev/null 2>&1; then
        TMP_GET_PIP="$(mktemp /tmp/get-pip.XXXXXX.py)"
        curl -sSL -o "$TMP_GET_PIP" https://bootstrap.pypa.io/get-pip.py
        "$VENV/bin/python" "$TMP_GET_PIP" --quiet
        rm -f "$TMP_GET_PIP"
    fi
    "$VENV/bin/pip" install --quiet -r "$REQ"
fi

exec "$VENV/bin/python" -m client "$@"
