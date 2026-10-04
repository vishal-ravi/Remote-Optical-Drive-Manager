# Remote Optical Drive Manager

Operate a CD/DVD writer attached to a remote Linux machine from a desktop GUI
on another computer — the drive behaves like a network peripheral.

```
 Ubuntu / Zorin                 Kali / Debian                  optical drive
┌──────────────────┐  SSH + HTTPS  ┌──────────────────┐        ┌────────────┐
│  PySide6 client  │──────────────▶│    rodm-agent    │───────▶│  /dev/sr0  │
│  browse · rip ·  │  control +    │  restricted ops  │ mount  │  DVD/CD RW │
│  burn · verify   │  file streams │  (systemd unit)  │ wodim  └────────────┘
└──────────────────┘  job progress └──────────────────┘
```

**Highlights**

- Remote drive discovery, media detection (label, filesystem, type, capacity)
- Mount / unmount / graphical browsing, file download to the local machine
- ISO rip from a physical disc, ISO build from local files, remote burning
  with live progress, simulate/overburn options and post-burn verification
- Sequential burn queue with "insert the next disc" prompts
- Two transports: token-authenticated HTTPS (preferred) with SSH fallback
- The agent has **no pip dependencies** — it runs with the stock `python3`
  of the remote host; every request maps to one validated, whitelisted
  operation (never a shell command)

---

## Features

| Area | What you get |
| --- | --- |
| Drives | auto-discovery of `/dev/sr*`, `/dev/cdrom*`; status: media, label, FS, capacity, recorder |
| Browse | mount, navigate directories on the disc, download selected files |
| Image | rip disc → ISO (streaming `dd`), build ISO from local files (auto-staged), list agent ISOs |
| Burn | speed / simulate / overburn / eject options, live progress, optional verify pass |
| Queue | several ISOs in sequence, prompts between discs, stops on first failure |
| Log | live operation log, also written to `~/.local/state/rodm/logs/` |
| Security | SSH keys + bearer token + TLS certificate pinning (trust on first use) |

## How it works

| Component | Runs on | Role |
| --- | --- | --- |
| `client/` | your desktop | PySide6 GUI, connection profiles, SSH/SFTP + HTTPS transports |
| `agent/` | the machine with the drive | restricted service: drive ops, jobs, REST API (`:8443`) |
| `common/` | both | protocol envelopes, validation, logging |

**Transports.** Every operation is available on both; the client prefers
**REST** (it talks to the one long-lived agent service, where job state lives
and which may run as root) and falls back to **SSH** (`rodm-agent rpc`, an
unprivileged per-call process) when REST is unavailable. Job-producing
operations (`burn_iso`, `create_iso_*`, `verify_disc`) *require* REST.

**Privilege.** Recommended: a systemd **system** service running the agent as
root (`--root`), so `mount`, `eject` and `wodim` work without sudoers
tweaks. An unprivileged user service also works: mounting then tries
`udisksctl` → `mount` → `sudo -n`. See [docs/installation.md](docs/installation.md).

## Requirements

- **Client:** Linux desktop, Python ≥ 3.10, X11/Wayland
  (`libxcb-cursor0` on Ubuntu 24.04+), internet on first run (pip bootstrap)
- **Drive host:** Linux with `python3` ≥ 3.10, SSH server, a `/dev/sr0`-style
  drive; agent tools: `mount`, `umount`, `eject`, `lsblk`, `blkid`, `udevadm`,
  `dd`, `openssl` (+ `wodim`, `genisoimage`, `udisksctl` for burning)
- Network: TCP `22` (SSH) and `8443` (HTTPS)

## Quick start

### 1. Client (this machine)

```bash
./scripts/run_client.sh
```

Creates `.venv`, installs PySide6/paramiko/requests, starts the GUI.
**Do not run the GUI with `sudo`** — it is a plain user application.

### 2. SSH key to the drive host

```bash
ssh-keygen -t ed25519                                  # if you have none yet
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@192.168.1.50
ssh user@192.168.1.50 'echo key ok'
```

### 3. Agent on the drive host

```bash
./scripts/install_agent.sh user@192.168.1.50 --install-packages --root
```

- `--install-packages` — apt-installs `wodim genisoimage eject udisks2`
- `--root` — systemd **system** service, agent runs as root (recommended)
- `--service` — unprivileged systemd *user* service instead
- no flag — sources installed, you start `~/rodm/bin/rodm-agent serve` yourself

The script prints the bearer token (you normally never have to copy it: the
client fetches it over SSH automatically).

Prefer containers? Build the agent image instead — see
[docs/docker.md](docs/docker.md): `docker build -t rodm-agent .`

### 4. Connect

GUI → **Profiles…** → host, SSH user, key path → **Test connection** →
**Save** → **Connect** → pick `/dev/sr0` → use the tabs.

## Using the GUI

| Tab | Purpose |
| --- | --- |
| **Browse** | Mount, walk the disc's directory tree, download files |
| **Image** | Rip the disc to an ISO, build an ISO from local files, list agent ISOs |
| **Burn** | Burn an agent ISO: speed, simulate, overburn, eject, verify |
| **Queue** | Burn several ISOs in sequence, swapping discs between them |
| **Log** | Live log with levels; same content in `~/.local/state/rodm/logs/` |

Full walkthrough: **[docs/user-guide.md](docs/user-guide.md)**.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/installation.md](docs/installation.md) | client setup, SSH keys, agent install modes, services, directories, upgrade/uninstall |
| [docs/docker.md](docs/docker.md) | containerized agent: build/run, volumes, token, TLS/networking |
| [docs/user-guide.md](docs/user-guide.md) | GUI tour, profiles, every tab, end-to-end workflows |
| [docs/agent-cli.md](docs/agent-cli.md) | `rodm-agent` subcommands, flags, config, logs, systemd |
| [docs/api.md](docs/api.md) | RPC envelope, every operation, REST endpoints, jobs, error codes |
| [docs/security.md](docs/security.md) | threat model, controls, privilege modes, hardening |
| [docs/troubleshooting.md](docs/troubleshooting.md) | error → fix table, diagnostics, known Qt pitfalls |
| [docs/development.md](docs/development.md) | layout, tests, lint, conventions, adding an operation |

## Agent at a glance

```bash
rodm-agent status --tools   # drives, media, utility paths
rodm-agent doctor           # environment / permission checks
rodm-agent token            # print the REST bearer token
rodm-agent serve            # HTTPS REST API on :8443
rodm-agent rpc              # one JSON request on stdin (SSH transport)
```

Both transports use the same JSON envelope:

```json
{"v":1,"id":"ab12","op":"burn_iso","params":{"device":"/dev/sr0","iso":"images/x.iso","speed":8}}
{"v":1,"id":"ab12","ok":true,"result":{"job_id":"…"}}
```

Long operations return a `job_id`; progress arrives as an NDJSON stream on
`GET /v1/jobs/<id>/events` (see [docs/api.md](docs/api.md)).

## Security in brief

- No arbitrary shell: fixed operation whitelist, typed validated parameters,
  argument-list subprocesses
- Optical devices only (`/dev/sr[0-9]+`, `/dev/cdrom*`) plus a system-disk
  blocklist — `/dev/sda` and friends are rejected structurally
- All file paths confined to the agent data root; browse paths are relative
  to the disc mountpoint
- SSH key for the control channel; 256-bit bearer token + TLS with SHA-256
  certificate pinning (trust on first use) for REST

Details and the threat model: **[docs/security.md](docs/security.md)**.

## Development

```bash
./scripts/run_client.sh                       # bootstrap .venv
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/   # 221 tests
.venv/bin/ruff check --select E9,F common agent client tests  # lint
```

Covered: protocol/validation, job manager, subprocess parsers, agent
dispatch, a loopback REST integration run (real HTTP server, real ISO
build), privilege-escalation helpers, TLS certificate logic and offscreen
GUI smoke tests. See [docs/development.md](docs/development.md).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ssh: Permission denied (publickey,password)` | `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host` |
| `has no filesystem - the disc is blank…` | insert a *written* data disc (blank media cannot be browsed) |
| `must be superuser to unmount/mount` | reinstall the agent with `--root`, or use REST so the root service handles it |
| `required utility 'wodim' is not installed` | `install_agent.sh … --install-packages` |
| `TLS fingerprint mismatch` | **Profiles… → Forget certificate → Test connection → Save** |
| Qt: `libxcb-cursor.so.0` / xcb plugin errors | `sudo apt install libxcb-cursor0` (client machine) |
| GUI dies with DBus/xkb warnings | you launched it with `sudo` — run it as your normal user |

Full table with diagnostics: **[docs/troubleshooting.md](docs/troubleshooting.md)**.
