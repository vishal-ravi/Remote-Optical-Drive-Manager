# `rodm-agent` — command line reference

The agent is pure-stdlib Python 3 (`python3` ≥ 3.10) — no pip packages on
the drive host. The installer creates a wrapper at `~/rodm/bin/rodm-agent`;
everywhere below you can substitute `python3 -m agent` run from the install
directory (with `common/` next to `agent/`).

```bash
rodm-agent -h | --version
rodm-agent serve    [--data-root D] [--state-dir D] [--log-level L]
                    [--host H] [--port P] [--insecure]
rodm-agent rpc      [--data-root D] [--state-dir D] [--log-level L]
rodm-agent status   [--tools]
rodm-agent token
rodm-agent doctor
rodm-agent version
```

## Global options (all subcommands except `version`)

| Flag | Default | Meaning |
| --- | --- | --- |
| `--data-root` | `$RODM_DATA_ROOT` or `~/.local/share/rodm` | ISOs, staging, mounts, TLS, token |
| `--state-dir` | `$RODM_STATE_DIR` or `~/.local/state/rodm` | logs |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## `serve` — the HTTPS REST service

```bash
rodm-agent serve                                  # 0.0.0.0:8443, TLS on
rodm-agent serve --host 127.0.0.1 --port 9443     # bind/port override
rodm-agent serve --insecure                       # allow plaintext (see below)
```

- Creates the data directories (`images/`, `stage/`, `mnt/`, `tls/`) with
  owner-only permissions and generates/loads the bearer token
  (`agent.token`, `0600`)
- Generates a self-signed TLS pair if missing (RSA-2048, SHA-256, 825 days,
  SANs = hostname + loopback + all local IPv4 addresses) and re-generates it
  on start **only if it no longer covers any current interface address**
- `SIGINT`/`SIGTERM` → graceful shutdown: stops accepting, cancels all jobs,
  exit code 0
- TLS is used whenever it is required (the default) **or a certificate
  already exists** — so `--insecure` alone does not turn TLS off on a host
  that has already served once: remove `<data-root>/tls/` first (fresh data
  root is cleanest). Serving plaintext on a non-loopback bind logs a loud
  warning.

Runs under systemd in production (see below); logs go to
`<state-dir>/logs/agent.log` *and* stdout (→ `journalctl`).

## `rpc` — SSH transport

Reads **one** JSON request from stdin, writes **one** JSON response to
stdout, exits 0/1 (`1` when `ok:false`). No TLS, no token — the SSH login is
the authentication. Each invocation constructs a fresh `AgentCore`, so job
state does not persist across calls (the client therefore uses REST for jobs).

```bash
echo '{"v":1,"id":"1","op":"ping","params":{}}' | rodm-agent rpc
# {"v":1,"id":"1","ok":true,"result":{…}}
```

## `status` — drive inventory

```bash
rodm-agent status          # per drive: model, media, type, label, fs, capacity,
                           #            writable, mounted
rodm-agent status --tools  # + resolved path of every utility (or MISSING)
```

## `token` — bearer token

Prints the REST token, creating it (`secrets.token_urlsafe(32)`, mode
`0600`) if it does not exist. `RODM_TOKEN` in the environment overrides the
file.

## `doctor` — environment check

Reports: data root/state dir, every utility (`lsblk blkid udevadm mount
umount udisksctl eject wodim genisoimage dd openssl`), token, detected
drives, write access — and a final `doctor: OK` / `doctor: N problem(s)`.
Exit code 1 on problems (usable in scripts/CI).

Missing `lsblk`/`udevadm`/`eject`/`dd` and a missing token count as
problems; `wodim`/`genisoimage` are reported but only *burning* needs them.

## `version`

```
rodm-agent 0.1.0 (protocol 1)
```

`version` (and the `version` RPC op) also reports the Python version,
detected tools and the data root.

---

## Running as a service

**System service (root, recommended)** — exactly what
`install_agent.sh … --root` writes to
`/etc/systemd/system/rodm-agent.service`:

```ini
[Unit]
Description=Remote Optical Drive Manager agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/home/<drive-user>/rodm
# keep the data root in the normal user's home:
Environment=HOME=/home/<drive-user>
Environment=PYTHONPATH=/home/<drive-user>/rodm
ExecStart=/usr/bin/env python3 -m agent serve
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now rodm-agent
journalctl -u rodm-agent -n 50 --no-pager
```

**User service:** `~/.config/systemd/user/rodm-agent.service`
(`ExecStart=%h/rodm/bin/rodm-agent serve --host 0.0.0.0 --port 8443`,
`WantedBy=default.target`) + `systemctl --user enable --now rodm-agent`
(+ `sudo loginctl enable-linger $USER` to survive logout).
Job state lives in that process — restarting the service loses running jobs
(cancelled cleanly at shutdown).

## Environment variables

| Variable | Read by | Effect |
| --- | --- | --- |
| `RODM_DATA_ROOT` | agent | override the data root |
| `RODM_STATE_DIR` | agent | override the state/log dir |
| `RODM_TOKEN` | agent | override the bearer token (else `agent.token`) |
| `RODM_CONFIG_DIR` | client | override `~/.config/rodm` |

## Logs

| Where | What |
| --- | --- |
| `<state-dir>/logs/agent.log` | rotating log of the `serve` process |
| `journalctl -u rodm-agent` | same output when run under systemd |
| stderr | `status`/`doctor`/`token` output; `rpc` responses on stdout only |
