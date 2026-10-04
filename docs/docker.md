# Running the agent in Docker

The **agent** is the piece that belongs on the drive host, so that is what
the `Dockerfile` builds (Python 3.12-slim + `wodim`, `genisoimage`,
`eject`, `udev`, `util-linux`, `openssl`, `iproute2`). The PySide6 **client**
runs natively on your desktop — a GUI image would only add X11/Wayland
plumbing for no benefit.

## Build

```bash
docker build -t rodm-agent .
```

## Run (recommended)

```bash
docker run -d --name rodm-agent \
  --network host \
  --privileged \
  -v rodm-data:/data \
  -v rodm-state:/state \
  --restart unless-stopped \
  rodm-agent
```

| Flag | Why |
| --- | --- |
| `--network host` | **Required.** The agent puts its own interface IPs into the TLS certificate; the GUI client verifies the hostname against that certificate. In bridge mode the container only knows `172.17.x`, so connecting to the host IP would fail verification. Host networking also exposes `8443` directly (skip `-p`). |
| `--privileged` | `mount`/`umount` and `wodim` on `/dev/sr0` need device access + `CAP_SYS_ADMIN` |
| `-v rodm-data:/data` | `images/`, `stage/`, `mnt/`, `tls/` and `agent.token` survive restarts |
| `-v rodm-state:/state` | rotating `agent.log` |

**Reduced-privilege variant** (if you do not want `--privileged`):

```bash
docker run -d --name rodm-agent --network host \
  --cap-add SYS_ADMIN --device /dev/sr0 \
  -v rodm-data:/data -v rodm-state:/state \
  rodm-agent
```

If `mount` is still refused, your AppArmor profile is in the way → add
`--security-opt apparmor:unconfined`. If `wodim` cannot see the drive, pass
its SCSI-generic node too: `--device /dev/sg0`.

If a **native** `rodm-agent` service is already running on the host, stop it
first (both want port 8443):

```bash
sudo systemctl disable --now rodm-agent
```

## Get the bearer token

There is no SSH server in the container, so "Fetch via SSH" in the client
does not apply — copy the token once:

```bash
docker exec rodm-agent python -m agent token
# or: docker exec rodm-agent cat /data/agent.token
```

## Day-to-day commands

```bash
docker logs -f rodm-agent                      # serve output (same as journalctl)
docker exec rodm-agent python -m agent status --tools   # drives + media + tools
docker exec rodm-agent python -m agent doctor           # environment check
docker restart rodm-agent                     # graceful stop cancels running jobs
docker stats rodm-agent                       # resource usage
```

Health (built-in `HEALTHCHECK`):

```bash
docker inspect -f '{{.State.Health.Status}}' rodm-agent
# manually:
curl -sk https://127.0.0.1:8443/v1/health
```

## Client profile

GUI → **Profiles…**:

| Field | Value |
| --- | --- |
| Host / IP | the drive-host machine IP |
| Use the REST API | ✓ (port `8443`) |
| Bearer token | paste the token from `docker exec … token` |
| SSH fields | optional — without an SSH server the client logs a warning and stays REST-only (job operations are REST-only anyway) |

## Notes

- On first contact the client pins the certificate (trust on first use);
  rebuilding the image with a **fresh `/data` volume** regenerates the
  token **and** certificate → re-fetch the token and press
  **Profiles… → Forget certificate** before reconnecting
- Plain-HTTP testing (`serve --insecure`) also needs a throwaway data dir:
  `docker run … -e RODM_DATA_ROOT=/tmp/x rodm-agent python -m agent serve
  --insecure`, and the healthcheck (HTTPS) will report unhealthy — override
  it with `--health-cmd` if you care
- The agent runs as root *inside* the container (it must); the container's
  root is still confined by the caps/devices you grant it above

See also: [installation.md](installation.md) (native install),
[security.md](security.md), [agent-cli.md](agent-cli.md).
