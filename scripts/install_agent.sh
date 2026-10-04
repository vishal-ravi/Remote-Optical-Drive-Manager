#!/usr/bin/env bash
# Install (or update) the rodm agent on a remote Linux host over SSH.
#
# Usage:
#   ./scripts/install_agent.sh user@host [--service | --root] [--install-packages]
#
#   --service           systemd *user* service (unprivileged; mount/burn need
#                       extra sudo rules)
#   --root              system service running as root - mount, eject and
#                       wodim all work without sudoers configuration
#   --install-packages  apt-get install wodim genisoimage eject udisks2
#
# Prerequisites:
#   - key-based SSH access to the host (ssh-copy-id user@host)
#   - python3 on the host (stdlib only - the agent has no pip dependencies)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-}"
WITH_SERVICE=0
AS_ROOT=0
INSTALL_PKGS=0
shift || true
for arg in "$@"; do
    case "$arg" in
        --service) WITH_SERVICE=1 ;;
        --root) WITH_SERVICE=1; AS_ROOT=1 ;;
        --install-packages) INSTALL_PKGS=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ -z "$TARGET" ]; then
    echo "usage: $0 user@host [--service | --root] [--install-packages]" >&2
    exit 2
fi

# fail early (and clearly) if key auth is not set up
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$TARGET" 'true' 2>/dev/null; then
    echo "error: cannot reach ${TARGET} with key authentication." >&2
    echo "       run: ssh-copy-id -i ~/.ssh/id_ed25519.pub ${TARGET}" >&2
    exit 1
fi

echo "[rodm] copying agent sources to ${TARGET}:~/rodm"
ssh "$TARGET" 'mkdir -p "$HOME/rodm/bin"'
tar -C "$ROOT" -cf - common agent | ssh "$TARGET" 'tar -C "$HOME/rodm" -xf -'

echo "[rodm] installing wrapper ~/rodm/bin/rodm-agent"
ssh "$TARGET" 'cat > "$HOME/rodm/bin/rodm-agent"; chmod +x "$HOME/rodm/bin/rodm-agent"' <<'EOF'
#!/bin/sh
# Remote Optical Drive Manager - agent launcher
RODM_HOME="${RODM_HOME:-$HOME/rodm}"
export PYTHONPATH="$RODM_HOME${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m agent "$@"
EOF

echo "[rodm] checking remote tooling"
ssh "$TARGET" '"$HOME/rodm/bin/rodm-agent" doctor' || true

if [ "$INSTALL_PKGS" = "1" ]; then
    if ! ssh "$TARGET" 'command -v wodim >/dev/null && command -v genisoimage >/dev/null'; then
        echo "[rodm] installing optical utilities (sudo may prompt for a password)"
        ssh -t "$TARGET" 'sudo apt-get update -qq && sudo apt-get install -y wodim genisoimage eject udisks2' || true
        echo "[rodm] re-checking remote tooling"
        ssh "$TARGET" '"$HOME/rodm/bin/rodm-agent" doctor' || true
    fi
fi

if [ "$WITH_SERVICE" = "1" ] && [ "$AS_ROOT" = "0" ]; then
    echo "[rodm] installing systemd *user* service (unprivileged)"
    ssh "$TARGET" 'mkdir -p "$HOME/.config/systemd/user"'
    scp -q "$ROOT/scripts/rodm-agent.service" \
        "$TARGET:.config/systemd/user/rodm-agent.service"
    ssh -t "$TARGET" 'sudo systemctl disable --now rodm-agent.service 2>/dev/null || true
        systemctl --user daemon-reload
        systemctl --user enable --now rodm-agent.service || true
        systemctl --user status rodm-agent.service --no-pager || true' || true
fi

if [ "$AS_ROOT" = "1" ]; then
    echo "[rodm] installing system service (agent runs as root)"
    REMOTE_HOME="$(ssh "$TARGET" 'printf %s "$HOME"')"
    ssh "$TARGET" 'cat > "$HOME/rodm-agent.service"' <<EOF
[Unit]
Description=Remote Optical Drive Manager agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${REMOTE_HOME}/rodm
Environment=HOME=${REMOTE_HOME}
Environment=PYTHONPATH=${REMOTE_HOME}/rodm
ExecStart=/usr/bin/env python3 -m agent serve
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    ssh -t "$TARGET" 'systemctl --user disable --now rodm-agent.service 2>/dev/null || true
        sudo install -m 644 "$HOME/rodm-agent.service" /etc/systemd/system/rodm-agent.service
        sudo systemctl daemon-reload
        sudo systemctl enable --now rodm-agent.service
        sudo systemctl restart rodm-agent.service
        echo "--- service status:"
        sudo systemctl status rodm-agent.service --no-pager -n 5 || true' || true
fi

if [ "$WITH_SERVICE" = "1" ]; then
    sleep 1
    echo "[rodm] agent status:"
    ssh "$TARGET" '"$HOME/rodm/bin/rodm-agent" status' || true
fi

TOKEN="$(ssh "$TARGET" '"$HOME/rodm/bin/rodm-agent" token')"

if [ "$AS_ROOT" = "1" ]; then
    START_HINT="already running via systemd (system service, root)"
else
    START_HINT="~/rodm/bin/rodm-agent serve"
fi

cat <<EOF

[rodm] agent installed.

  Start the REST API on the remote host:
      ${START_HINT}

  Bearer token (paste into the client profile):
      ${TOKEN}

  REST port: 8443 (HTTPS, self-signed; the client pins its fingerprint)

  Client setup: Profiles... -> host=${TARGET}, SSH key ~/.ssh/id_ed25519,
  REST enabled + token above -> Test connection -> Save -> Connect
EOF
