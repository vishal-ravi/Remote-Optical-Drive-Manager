# Installation

Two pieces: the **client** on your desktop, the **agent** on the machine with
the optical drive. They talk over SSH and HTTPS, so both need network reach
(TCP `22` and `8443`).

---

## 1. Client (desktop)

```bash
cd disc                  # this repository
./scripts/run_client.sh
```

The script creates `.venv/` with `--system-site-packages --without-pip`; if
`PySide6`, `paramiko` and `requests` are not already importable it
bootstraps pip (`get-pip.py`, needs `curl` + internet) and installs
`requirements-client.txt`. Later runs reuse the cached venv. Equivalent
entry point: `.venv/bin/python -m client`.

Manual equivalent:

```bash
python3 -m venv .venv --system-site-packages
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install PySide6 paramiko requests
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/   # sanity check
.venv/bin/python -m client                                     # or use run_client.sh
```

### Client requirements

- Python ≥ 3.10 (3.11/3.12 fine), a graphical session (X11 or Wayland)
- Qt platform plugins: on Ubuntu 24.04+ `sudo apt install libxcb-cursor0`
  (older releases: `libxcb-xinerama0 libxkbcommon-x11-0`)
- An SSH key pair (`ssh-keygen -t ed25519` if you have none)

> **Never run the GUI as root.** It breaks Qt (DBus/xkb under a different
> HOME) and is unnecessary: privilege is handled on the agent side.
> `scripts/run_client.sh` refuses to start under root on purpose.

### Client files

| Path | Contents |
| --- | --- |
| `~/.config/rodm/profiles.json` | connection profiles (mode `0600`) |
| `~/.config/rodm/certs/` | pinned TLS certificates, one per host:port |
| `~/.local/state/rodm/logs/rodm.log` | GUI log file (name comes from logger `rodm`) |
| `RODM_CONFIG_DIR` (env) | overrides the config directory |

## 2. SSH key to the drive host

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -C rodm
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@192.168.1.50
ssh user@192.168.1.50 'echo key ok'    # must print "key ok" without a password
```

The key is used for two things: running the agent CLI remotely
(`ssh … rodm-agent rpc/status`) and reading the REST token from the agent
data root when the profile has none (`Fetch via SSH` in the GUI).

## 3. Agent (drive host)

### Option A — system service as root (recommended)

```bash
./scripts/install_agent.sh user@192.168.1.50 --install-packages --root
```

- uploads `common/` + `agent/` to `~/rodm/`, creates the `rodm-agent`
  wrapper, runs `doctor`, and prints the bearer token
- `--install-packages` runs `sudo apt-get update && sudo apt-get install -y
  wodim genisoimage eject udisks2` (only when they are missing)
- `--root` writes `/etc/systemd/system/rodm-agent.service` with
  `User=root`, `WorkingDirectory=<your home>/rodm`,
  `Environment=HOME=<your home>` and `ExecStart=python3 -m agent serve` —
  so the data root stays in your home while `mount`, `eject`, `close_tray`
  and `wodim` run with full privileges and no sudoers configuration is
  needed (the existing *user* unit is disabled first)

### Option B — systemd *user* service (unprivileged)

```bash
./scripts/install_agent.sh user@192.168.1.50 --service
```

Installs `~/.config/systemd/user/rodm-agent.service`
(`ExecStart=%h/rodm/bin/rodm-agent serve --host 0.0.0.0 --port 8443`) and
runs `systemctl --user enable --now rodm-agent`. The root unit is disabled
if present. For the service to keep running while you are logged out, also
run `sudo loginctl enable-linger $USER` (the script does not do this).

Mounting then degrades gracefully: `udisksctl` → `mount` → `sudo -n` (each
requires your group membership / passwordless sudo; `doctor` tells you which
is available).

### Option C — sources only

```bash
./scripts/install_agent.sh user@192.168.1.50
~/rodm/bin/rodm-agent serve            # foreground; or run via systemd yourself
```

### After install

```bash
ssh user@host '~/rodm/bin/rodm-agent doctor'      # ends with "doctor: OK"
ssh user@host '~/rodm/bin/rodm-agent status --tools'
ssh user@host '~/rodm/bin/rodm-agent token'       # REST bearer token
sudo systemctl status rodm-agent                  # root service (Option A)
sudo journalctl -u rodm-agent -n 50 --no-pager
```

The token is stored in the agent data root as `agent.token` (mode `0600`).
The client fetches it over SSH automatically, so you rarely handle it.

### Upgrade / uninstall

```bash
./scripts/install_agent.sh user@host --root          # re-upload + restart

# full removal on the drive host:
ssh user@host 'sudo systemctl disable --now rodm-agent || true
               sudo rm -f /etc/systemd/system/rodm-agent.service
               sudo systemctl daemon-reload
               rm -rf ~/rodm ~/.local/share/rodm ~/.local/state/rodm'

## 4. Directory layout on the agent host

| Path | Purpose |
| --- | --- |
| `~/rodm/` | agent sources (`common/`, `agent/`) and `bin/rodm-agent` |
| `~/.local/share/rodm/` | data root: `images/`, `stage/`, `mnt/`, `tls/`, `agent.token` |
| `~/.local/state/rodm/logs/` | `agent.log` |
| `~/rodm-agent.service` | unit file the script generates (then installed to…) |
| `/etc/systemd/system/rodm-agent.service` | root service (Option A) |
| `~/.config/systemd/user/rodm-agent.service` | user service (Option B) |

Environment overrides: `RODM_DATA_ROOT`, `RODM_STATE_DIR`, `RODM_TOKEN`
(see [agent-cli.md](agent-cli.md)).

## 5. Network

| Port | Proto | Used for |
| --- | --- | --- |
| 22 | SSH | control RPC (`rodm-agent rpc`), token fetch, SFTP downloads |
| 8443 | HTTPS (TLS) | REST: RPC, job event streams, file/image/upload |

The agent binds `0.0.0.0:8443` by default; there is **no IP allowlist layer**
— access control is SSH + bearer token + certificate pinning
([security.md](security.md)). If the drive host is reachable from untrusted
networks, firewall port 8443 to your LAN.

Next: [user-guide.md](user-guide.md) or [agent-cli.md](agent-cli.md).
