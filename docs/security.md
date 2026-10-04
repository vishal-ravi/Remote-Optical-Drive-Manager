# Security model

The agent turns a machine with an optical drive into a remote-controlled
peripheral. It therefore has to be safe against the two realistic threats on
a home/lab LAN:

1. **Anyone who can reach TCP 8443** trying to command the drive, read
   discs, or write files.
2. **A malicious/confused client** trying to escape its scope: writing to
   system disks, reading host files outside the agent's data root, or
   abusing shell execution.

Out of scope: someone who already has an SSH shell on the host (they can do
anything that user/root can do anyway), and physical attacks on the disc.

---

## Trust chain

```
GUI ── SSH (key auth) ──────────────▶ rodm-agent rpc / token fetch / SFTP
GUI ── HTTPS + pinned cert + token ─▶ rodm-agent serve (REST)
```

| Layer | Mechanism |
| --- | --- |
| Control channel | SSH public-key authentication (your existing keys) |
| REST authentication | bearer token: `secrets.token_urlsafe(32)` (256 bits) in `agent.token` (`0600`), compared with `hmac.compare_digest` |
| Transport confidentiality | self-signed TLS: RSA-2048, SHA-256, 825 days, SANs = hostname + `127.0.0.1` + every local IPv4 address |
| Client trust | **trust on first use**: the pinned SHA-256/PEM certificate lands in `~/.config/rodm/certs/<host>-<port>.pem`; any mismatch aborts with `unauthorized`/TLS error until you press **Forget certificate** |
| Operation surface | fixed whitelist (`KNOWN_OPS`), every parameter validated |

Notes:

- the agent re-generates its certificate only when it no longer covers any
  current interface address (so ordinary restarts do not invalidate pins);
  changing the host's IP does → the client reports a fingerprint mismatch →
  **Forget certificate** and reconnect
- `rodm-agent serve` logs the token once at startup — anyone who can read
  `journalctl -u rodm-agent` or `agent.log` effectively holds the token;
  treat both as secrets
- `--insecure` (plaintext) is for loopback testing only, and only applies
  while no certificate exists yet (see [agent-cli.md](agent-cli.md));
  binding it to a non-loopback address prints a loud warning

## Request handling

| Threat | Control |
| --- | --- |
| Arbitrary command execution | no shell anywhere: operations dispatch to fixed Python functions; subprocess calls are argument lists (`[wodim, "-data", iso, …]`) |
| Unknown/hostile operations | `op` must be in `KNOWN_OPS` (`unknown_op` otherwise) |
| Malformed parameters | typed validators (`validate_device`, `validate_relative_path`, `validate_speed`, `validate_bool`, `validate_job_id`, …) → `validation_error` |
| Writing to the system disk | `DEVICE_RE` only accepts `/dev/(sr[0-9]+\|cdrom[0-9]*\|scd[0-9]+\|optical[0-9]*)`, **and** `FORBIDDEN_DEVICE_RE` additionally rejects `/dev/sd*`, `/dev/nvme*`, `/dev/mmcblk*`, `/dev/vd*`, `/dev/md*`, `/dev/dm-*` |
| Reading host files | every path is resolved inside the data root (`resolve_in_root`) or inside the disc mountpoint (`resolve_media_path`); `..`, absolute paths and escaping symlinks are rejected |
| Path traversal via upload | `POST /v1/upload?path=` goes through the same resolution; writes to `*.part` then atomic `os.replace` |
| Oversized bodies | RPC body ≤ 16 MiB, upload ≤ `max_iso_bytes` (12 GiB, `413`), staged-path lists capped at 4096 entries |
| Token guessing | 256-bit random token, constant-time comparison, TLS in front of it (plaintext mode loudly warned) |
| Log injection | structured logging with `%s` formatting; job events are JSON-encoded |

## Privilege

Two deployment modes ([installation.md](installation.md)):

| Mode | Agent runs as | Mount/burn mechanism |
| --- | --- | --- |
| `--root` (recommended) | root, `HOME=<drive user>` | direct `mount`/`umount`/`eject`/`wodim` |
| `--service` (user) | your login user | `udisksctl` → `mount` → `sudo -n` fallback |

In user mode the agent probes `sudo -n true` once (cached) and prefixes only
the handful of operations that need it (`mount`, `umount`, `eject`,
`close_tray`, `wodim`) — **never** an unvalidated string, and never for file
handling. Without a sudoers rule the command runs unchanged, so group-based
access (`cdrom`, `cdrom_rw`) still works.

The GUI must **not** run as root (`run_client.sh` refuses): privilege is
decided on the agent, not in the client.

## Files & permissions

| Object | Mode |
| --- | --- |
| data root, `images/`, `stage/`, `mnt/`, `tls/` | `0700` |
| `agent.token`, `server.key` | `0600` |
| `profiles.json` | `0600` |
| uploaded files | written as `*.part`, renamed atomically |

## Hardening checklist (if the host is not on a trusted LAN)

- firewall port `8443` to your client addresses (`ufw allow from <lan> to
  any port 8443`)
- keep the SSH key passphrase-protected / use `ssh-agent`
- run the agent as the unprivileged user (`--service`) if you do not need
  mounting, and grant `udisks`/`cdrom` group access instead of sudo
- rotate the token by deleting `agent.token` and restarting the service,
  then **Fetch via SSH** again in the profile
- review `journalctl -u rodm-agent` / `agent.log` regularly (they contain
  the token line at startup)
- keep `python3` and the utilities patched (`doctor` shows what is used)

## Known limitations

- REST has no per-user authorization: possession of the token is full
  access to the operation whitelist (that is by design for a single-trust
  LAN tool)
- no request rate limiting or audit trail beyond the logs
- jobs are in-memory: a restart cancels burns (fail-safe for optical media —
  the drive stops writing)
- certificate pinning is per host:port; port changes look like a new server
