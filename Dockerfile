# syntax=docker/dockerfile:1
# Remote Optical Drive Manager - agent image
#
# The agent itself is stdlib-only Python; this image adds the Python runtime
# and the whitelisted system utilities it is allowed to invoke (agent/tools.py).
#
#   docker build -t rodm-agent .
#   docker run -d --name rodm-agent --network host --privileged \
#       -v rodm-data:/data -v rodm-state:/state rodm-agent
#
# --network host matters: the agent bakes its own interface addresses into
# the TLS certificate, and the GUI client verifies the hostname against it.

FROM python:3.12-slim

# lsblk/blkid/mount/umount (util-linux), udevadm (udev), eject, dd (coreutils),
# openssl (TLS pair generation), wodim (burn), genisoimage (ISO build),
# iproute2 (`ip -o -4 addr` for certificate SANs)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        eject \
        genisoimage \
        iproute2 \
        openssl \
        udev \
        util-linux \
        wodim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/rodm

# same layout the tarball installer produces (~/rodm/{common,agent})
COPY common/ ./common/
COPY agent/  ./agent/

# PYTHONPATH mirrors the rodm-agent wrapper script
ENV PYTHONPATH=/opt/rodm \
    PYTHONUNBUFFERED=1 \
    RODM_DATA_ROOT=/data \
    RODM_STATE_DIR=/state

# /data   -> images/ stage/ mnt/ tls/ agent.token   (persist: ISOs + token + cert)
# /state  -> logs/agent.log (also visible via `docker logs`)
VOLUME ["/data", "/state"]

EXPOSE 8443

# /v1/health needs no auth; certificate is self-signed -> unverified context
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import ssl,urllib.request;urllib.request.urlopen('https://127.0.0.1:8443/v1/health', context=ssl._create_unverified_context(), timeout=4)"

# root inside the container: mount/umount/eject/wodim need it (see docs/docker.md
# for a reduced-capability alternative to --privileged)
CMD ["python", "-m", "agent", "serve"]
