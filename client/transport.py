"""Transports: SSH (paramiko) and REST (requests) talking to the same agent.

``ClientSession`` opens whichever channels the profile allows and picks the
best one per operation: SFTP/SSH for control and bulk transfer, REST for
long-lived progress streams, with automatic fallback the other way.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import requests

from common.protocol import (
    ERR_UNAUTHORIZED,
    JOB_OPS,
    AgentError,
    JobSnapshot,
    ProtocolError,
    encode,
    make_request,
    parse_response,
    response_result,
)
from .profiles import Profile, config_dir

log = logging.getLogger("rodm.client.transport")

ERR_CONNECTION = "connection_error"
ERR_TIMEOUT = "timeout"
PROGRESS_CB = Callable[[int, int], None]


def _connection_error(message: str) -> AgentError:
    return AgentError(message, ERR_CONNECTION)


def _close_stdin(stdin: Any) -> None:
    """Signal EOF to the remote process (paramiko's ChannelStdinFile lacks
    shutdown_write(); fall back to the channel, then to close())."""
    stdin.flush()
    try:
        stdin.shutdown_write()
        return
    except AttributeError:
        pass
    channel = getattr(stdin, "channel", None)
    if channel is not None:
        try:
            channel.shutdown_write()
            return
        except Exception:
            pass
    try:
        stdin.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# SSH
# --------------------------------------------------------------------------- #


class SshTransport:
    kind = "ssh"

    def __init__(self, profile: Profile, timeout: float = 30.0) -> None:
        self.profile = profile
        self.timeout = timeout
        self._client: Any = None
        self._sftp: Any = None
        self.connected = False
        self._lock = threading.RLock()  # paramiko channels are not thread-safe

    # -- connection ----------------------------------------------------- #

    def connect(self) -> None:
        import paramiko

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict[str, Any] = {
            "hostname": self.profile.host,
            "port": int(self.profile.port or 22),
            "username": self.profile.user or None,
            "timeout": self.timeout,
            "banner_timeout": self.timeout,
            "auth_timeout": self.timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }
        key_path = self.profile.key_path_expanded
        if key_path:
            if not os.path.exists(key_path):
                raise AgentError(f"ssh key not found: {key_path}", ERR_CONNECTION)
            kwargs["key_filename"] = key_path
            kwargs["look_for_keys"] = False

        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            raise AgentError(
                f"SSH authentication failed for {self.profile.ssh_target}: {exc}",
                ERR_UNAUTHORIZED,
            ) from exc
        except (socket.error, paramiko.SSHException, OSError) as exc:
            raise _connection_error(
                f"cannot reach {self.profile.host}:{self.profile.port} - {exc}"
            ) from exc

        self._client = client
        self.connected = True
        log.info("ssh connected to %s", self.profile.ssh_target)

    def close(self) -> None:
        for handle in (self._sftp, self._client):
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # pragma: no cover - best effort
                    pass
        self._sftp = None
        self._client = None
        self.connected = False

    # -- control -------------------------------------------------------- #

    def call(
        self, op: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        if self._client is None:
            raise _connection_error("ssh transport is not connected")
        request = make_request(op, params)
        command = f"{self.profile.agent_cmd} rpc"
        with self._lock:
            return self._exec(op, command, request, timeout)

    def _exec(self, op: str, command: str, request: dict[str, Any], timeout: float) -> Any:
        try:
            stdin, stdout, stderr = self._client.exec_command(
                command, timeout=timeout, get_pty=False
            )
            stdin.write(encode(request))
            stdin.write("\n")
            _close_stdin(stdin)
            raw = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
        except (socket.timeout, TimeoutError) as exc:
            raise AgentError(f"'{op}' timed out after {timeout}s", ERR_TIMEOUT) from exc
        except Exception as exc:  # paramiko raises assorted errors
            self.connected = False
            raise _connection_error(f"ssh command failed: {exc}") from exc

        raw = raw.strip()
        if not raw:
            detail = err.strip().splitlines()[-1] if err.strip() else "no output"
            raise _connection_error(
                f"agent produced no response via '{command}' ({detail})"
            )
        # the agent writes exactly one JSON line; tolerate stray warnings
        line = raw.splitlines()[-1]
        try:
            response = parse_response(line)
        except ProtocolError as exc:
            raise _connection_error(
                f"unreadable agent response: {line[:200]}"
            ) from exc
        return response_result(response)

    def run_command(self, command: str, timeout: float = 15.0) -> str:
        """Execute a plain remote command (used for agent provisioning)."""
        with self._lock:
            if self._client is None:
                raise _connection_error("ssh transport is not connected")
            try:
                _stdin, stdout, stderr = self._client.exec_command(
                    command, timeout=timeout, get_pty=False
                )
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
            except (socket.timeout, TimeoutError) as exc:
                raise AgentError(
                    f"'{command}' timed out after {timeout}s", ERR_TIMEOUT
                ) from exc
            except Exception as exc:
                raise _connection_error(f"ssh command failed: {exc}") from exc
        if not out.strip() and err.strip():
            raise AgentError(err.strip().splitlines()[-1], "tool_failed")
        return out

    # -- transfer ------------------------------------------------------- #

    def _sftp_open(self):
        if self._sftp is None:
            if self._client is None:
                raise _connection_error("ssh transport is not connected")
            try:
                self._sftp = self._client.open_sftp()
            except Exception as exc:
                raise _connection_error(f"sftp channel failed: {exc}") from exc
        return self._sftp

    def download(
        self, remote_path: str, local_path: str | Path, progress: PROGRESS_CB | None = None
    ) -> int:
        with self._lock:
            sftp = self._sftp_open()
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                info = sftp.stat(remote_path)
            size = int(getattr(info, "st_size", 0) or 0)
        except OSError as exc:
            raise AgentError(f"cannot stat {remote_path}: {exc}", ERR_CONNECTION) from exc

        callback = None
        if progress:
            callback = lambda done, total: progress(done, total or size)  # noqa: E731
        try:
            with self._lock:
                sftp.get(remote_path, str(target), callback=callback)
        except OSError as exc:
            raise AgentError(f"download failed: {exc}", ERR_CONNECTION) from exc
        return target.stat().st_size

    def upload(
        self, local_path: str | Path, remote_path: str, progress: PROGRESS_CB | None = None
    ) -> str:
        with self._lock:
            sftp = self._sftp_open()
        source = Path(local_path)
        size = source.stat().st_size
        callback = None
        if progress:
            callback = lambda done, total: progress(done, total or size)  # noqa: E731
        try:
            with self._lock:
                sftp.put(str(source), remote_path, callback=callback)
        except OSError as exc:
            raise AgentError(f"upload failed: {exc}", ERR_CONNECTION) from exc
        return remote_path

    def listdir(self, remote_path: str) -> list[str]:
        with self._lock:
            sftp = self._sftp_open()
            try:
                return sorted(sftp.listdir(remote_path))
            except OSError as exc:
                raise AgentError(
                    f"cannot list {remote_path}: {exc}", ERR_CONNECTION
                ) from exc

    # -- job events ----------------------------------------------------- #

    def iter_job_updates(
        self, job_id: str, stop: threading.Event | None = None, poll: float = 0.5
    ) -> Iterator[dict[str, Any]]:
        cursor = 0
        while True:
            if stop is not None and stop.is_set():
                return
            data = self.call("job_events", {"job_id": job_id, "after": cursor})
            for event in data.get("events", []):
                cursor = int(event.get("seq", cursor))
                yield {"type": "event", "event": event}
            job = data.get("job") or {}
            yield {"type": "status", "job": job}
            if job.get("state") in ("done", "failed", "cancelled"):
                return
            time.sleep(poll)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #


class RestTransport:
    kind = "rest"

    def __init__(self, profile: Profile, timeout: float = 30.0) -> None:
        self.profile = profile
        self.timeout = timeout
        self.session = requests.Session()
        self.connected = False
        self.cert_path: str = ""

    # -- TLS trust ------------------------------------------------------ #

    def _fetch_server_cert(self) -> str:
        """Return the PEM certificate the agent is presenting (no trust yet)."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(
            (self.profile.host, int(self.profile.rest_port)), timeout=self.timeout
        ) as sock:
            with context.wrap_socket(sock, server_hostname=self.profile.host) as tls:
                return ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True))

    @staticmethod
    def fingerprint(pem: str) -> str:
        der = ssl.PEM_cert_to_DER_cert(pem)
        return hashlib.sha256(der).hexdigest()

    def _ensure_trusted_cert(self) -> str:
        profile = self.profile
        if profile.tls_cert_file and os.path.exists(profile.tls_cert_file):
            pem = Path(profile.tls_cert_file).read_text(encoding="utf-8")
            if not profile.tls_fingerprint:
                return profile.tls_cert_file
            if self.fingerprint(pem) != profile.tls_fingerprint:
                raise AgentError(
                    "stored TLS certificate no longer matches this host "
                    f"({profile.host}:{profile.rest_port})",
                    ERR_UNAUTHORIZED,
                )
            return profile.tls_cert_file

        try:
            pem = self._fetch_server_cert()
        except (OSError, ssl.SSLError) as exc:
            raise _connection_error(
                f"cannot fetch agent certificate from "
                f"{profile.host}:{profile.rest_port} - {exc}"
            ) from exc

        digest = self.fingerprint(pem)
        if profile.tls_fingerprint and digest != profile.tls_fingerprint:
            raise AgentError(
                f"TLS fingerprint mismatch for {profile.host}:{profile.rest_port}: "
                f"expected {profile.tls_fingerprint}, got {digest}",
                ERR_UNAUTHORIZED,
            )

        # trust on first use (or explicit confirmation) -> persist the PEM
        certs_dir = config_dir() / "certs"
        certs_dir.mkdir(parents=True, exist_ok=True)
        pem_path = certs_dir / f"{profile.host}-{profile.rest_port}.pem"
        pem_path.write_text(pem, encoding="utf-8")
        os.chmod(pem_path, 0o600)
        self.profile.tls_fingerprint = digest
        self.profile.tls_cert_file = str(pem_path)
        log.info("trusted agent certificate %s (sha256:%s...)", pem_path, digest[:12])
        return str(pem_path)

    # -- connection ----------------------------------------------------- #

    @property
    def _tls(self) -> bool:
        return (self.profile.rest_scheme or "https") == "https"

    def connect(self) -> None:
        if not self.profile.rest_token:
            raise AgentError(
                "profile has no REST token (run 'rodm-agent token' on the remote host)",
                ERR_UNAUTHORIZED,
            )
        self.cert_path = self._ensure_trusted_cert() if self._tls else ""
        base = self.profile.rest_base_url
        try:
            response = self.session.get(
                f"{base}/v1/health",
                timeout=self.timeout,
                verify=self.cert_path or True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise _connection_error(f"agent REST API unreachable: {exc}") from exc
        self.session.headers.update(
            {"Authorization": f"Bearer {self.profile.rest_token}"}
        )
        self.connected = True
        log.info("rest connected to %s", base)

    def close(self) -> None:
        self.session.close()
        self.connected = False

    # -- control -------------------------------------------------------- #

    def call(
        self, op: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        if not self.connected:
            raise _connection_error("rest transport is not connected")
        request = make_request(op, params)
        try:
            response = self.session.post(
                f"{self.profile.rest_base_url}/v1/rpc",
                data=encode(request),
                timeout=(self.timeout, timeout),
                verify=self.cert_path or True,
            )
        except requests.Timeout as exc:
            raise AgentError(f"'{op}' timed out after {timeout}s", ERR_TIMEOUT) from exc
        except requests.RequestException as exc:
            raise _connection_error(f"rest call failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise _connection_error(
                f"agent returned non-json response (http {response.status_code})"
            ) from exc
        return response_result(payload)

    # -- transfer ------------------------------------------------------- #

    def download(
        self, remote_path: str, local_path: str | Path, progress: PROGRESS_CB | None = None
    ) -> int:
        raise AgentError(
            "rest download requires device/path parameters", ERR_CONNECTION
        )

    def download_media(
        self,
        device: str,
        media_path: str,
        local_path: str | Path,
        progress: PROGRESS_CB | None = None,
    ) -> int:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.session.get(
                f"{self.profile.rest_base_url}/v1/file",
                params={"device": device, "path": media_path},
                stream=True,
                timeout=(self.timeout, 120),
                verify=self.cert_path or True,
            ) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total or done)
        except requests.RequestException as exc:
            target.unlink(missing_ok=True)
            raise AgentError(f"download failed: {exc}", ERR_CONNECTION) from exc
        return target.stat().st_size

    def upload(
        self, local_path: str | Path, remote_path: str, progress: PROGRESS_CB | None = None
    ) -> str:
        source = Path(local_path)
        size = source.stat().st_size
        if not self.connected:
            raise _connection_error("rest transport is not connected")
        try:
            with source.open("rb") as handle:
                response = self.session.post(
                    f"{self.profile.rest_base_url}/v1/upload",
                    params={"path": remote_path},
                    data=handle,
                    headers={"Content-Length": str(size)},
                    timeout=(self.timeout, 600),
                    verify=self.cert_path or True,
                )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise AgentError(f"upload failed: {exc}", ERR_CONNECTION) from exc
        if progress:
            progress(size, size)
        if not payload.get("ok"):
            error = payload.get("error") or {}
            raise AgentError(
                error.get("message", "upload rejected"), error.get("code", "internal_error")
            )
        return payload["result"]["path"]

    def _raw_stream(self, path: str, params: dict[str, str] | None = None):
        response = self.session.get(
            f"{self.profile.rest_base_url}{path}",
            params=params or {},
            stream=True,
            timeout=(self.timeout, 60),
            verify=self.cert_path or True,
        )
        response.raise_for_status()
        return response

    # -- job events ----------------------------------------------------- #

    def iter_job_updates(
        self, job_id: str, stop: threading.Event | None = None, poll: float = 0.4
    ) -> Iterator[dict[str, Any]]:
        cursor = 0
        try:
            with self._raw_stream(
                f"/v1/jobs/{job_id}/events", {"after": cursor}
            ) as response:
                for raw in response.iter_lines(decode_unicode=True):
                    if stop is not None and stop.is_set():
                        return
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue
        except requests.RequestException as exc:
            log.warning("event stream ended early (%s); falling back to polling", exc)
        # fallback / follow-up: poll until terminal
        cursor = 0
        while stop is None or not stop.is_set():
            try:
                data = self.call("job_events", {"job_id": job_id, "after": cursor})
            except AgentError:
                return
            for event in data.get("events", []):
                cursor = int(event.get("seq", cursor))
                yield {"type": "event", "event": event}
            job = data.get("job") or {}
            yield {"type": "status", "job": job}
            if job.get("state") in ("done", "failed", "cancelled"):
                return
            time.sleep(poll)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class ClientSession:
    """Combines SSH and REST transports for one host profile."""

    def __init__(self, profile: Profile, timeout: float = 30.0) -> None:
        self.profile = profile
        self.timeout = timeout
        self.ssh: SshTransport | None = None
        self.rest: RestTransport | None = None
        self.errors: list[str] = []
        self._xfer_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------ #

    def connect(self, *, use_ssh: bool = True, use_rest: bool | None = None) -> None:
        self.close()
        self.errors = []
        want_ssh = use_ssh and bool(self.profile.host)
        want_rest = self.profile.rest_enabled if use_rest is None else use_rest

        if want_ssh:
            ssh = SshTransport(self.profile, timeout=self.timeout)
            try:
                ssh.connect()
                self.ssh = ssh
            except AgentError as exc:
                self.errors.append(f"ssh: {exc.message}")
                log.warning("ssh unavailable: %s", exc.message)

        if want_rest and not (self.profile.rest_token or "").strip():
            # the token lives on the agent host; SSH is already authenticated
            if self.ssh is not None:
                try:
                    out = self.ssh.run_command(
                        f"{self.profile.agent_cmd} token", timeout=10
                    )
                    token = out.strip().splitlines()[-1].strip() if out.strip() else ""
                    if token:
                        self.profile.rest_token = token
                        log.info("fetched REST token over SSH for %s", self.profile.ssh_target)
                except Exception as exc:
                    log.info("could not fetch REST token over SSH: %s", exc)

        if want_rest:
            rest = RestTransport(self.profile, timeout=self.timeout)
            try:
                rest.connect()
                # persist trust-on-first-use material
                self.profile.tls_fingerprint = rest.profile.tls_fingerprint
                self.profile.tls_cert_file = rest.profile.tls_cert_file
                self.rest = rest
            except AgentError as exc:
                self.errors.append(f"rest: {exc.message}")
                log.warning("rest unavailable: %s", exc.message)

        if not self.connected:
            detail = "; ".join(self.errors) or "no transport configured"
            raise AgentError(detail, ERR_CONNECTION)

    def close(self) -> None:
        for transport in (self.ssh, self.rest):
            if transport is not None:
                transport.close()
        self.ssh = None
        self.rest = None

    # -- state ---------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return bool(
            (self.ssh and self.ssh.connected) or (self.rest and self.rest.connected)
        )

    @property
    def active(self) -> str:
        if self.ssh and self.ssh.connected:
            return "ssh"
        if self.rest and self.rest.connected:
            return "rest"
        return "offline"

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "transport": self.active,
            "ssh": bool(self.ssh and self.ssh.connected),
            "rest": bool(self.rest and self.rest.connected),
            "errors": list(self.errors),
        }

    # -- calls ---------------------------------------------------------- #

    def call(
        self, op: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        if op in JOB_OPS and not (self.rest and self.rest.connected):
            raise AgentError(
                f"'{op}' runs as a background job, which needs the REST API - "
                "start the agent service on the host "
                "(systemctl start rodm-agent) and give the profile its token",
                ERR_CONNECTION,
            )
        # REST is preferred: it talks to the one long-lived agent service
        # (job state lives there) which may run as root, while every SSH rpc
        # call spawns a fresh, unprivileged agent process.
        order = self._order(prefer="rest")
        if not order:
            raise AgentError("not connected", ERR_CONNECTION)
        last_error: AgentError | None = None
        for transport in order:
            try:
                return transport.call(op, params, timeout=timeout)
            except AgentError as exc:
                last_error = exc
                if exc.code in (ERR_CONNECTION, ERR_TIMEOUT, "job_not_found"):
                    log.info("%s transport failed for %s: %s", transport.kind, op, exc.message)
                    continue
                raise
        raise last_error or AgentError("all transports failed", ERR_CONNECTION)

    def _order(self, prefer: str = "ssh") -> list[Any]:
        available = [
            t
            for t in (self.ssh, self.rest)
            if t is not None and getattr(t, "connected", False)
        ]
        if not available:
            return []
        if prefer == "ssh":
            available.sort(key=lambda t: 0 if t.kind == "ssh" else 1)
        else:
            available.sort(key=lambda t: 0 if t.kind == "rest" else 1)
        return available

    # -- transfer ------------------------------------------------------- #

    def download(
        self,
        local_path: str | Path,
        *,
        remote_path: str | None = None,
        device: str | None = None,
        media_path: str | None = None,
        progress: PROGRESS_CB | None = None,
    ) -> int:
        """Pull a file: absolute agent path (SSH) or device+path (REST)."""
        with self._xfer_lock:
            return self._download_locked(
                local_path,
                remote_path=remote_path,
                device=device,
                media_path=media_path,
                progress=progress,
            )

    def _download_locked(
        self,
        local_path: str | Path,
        *,
        remote_path: str | None = None,
        device: str | None = None,
        media_path: str | None = None,
        progress: PROGRESS_CB | None = None,
    ) -> int:
        if self.ssh and self.ssh.connected and remote_path:
            return self.ssh.download(remote_path, local_path, progress)
        if self.rest and self.rest.connected and device is not None:
            return self.rest.download_media(
                device, media_path or "/", local_path, progress
            )
        if self.rest and self.rest.connected and remote_path:
            # REST fallback needs device/path semantics; only media paths work
            raise AgentError(
                "rest transport requires device+path for downloads", ERR_CONNECTION
            )
        raise AgentError("no transport available for download", ERR_CONNECTION)

    def upload(
        self, local_path: str | Path, remote_relative: str, progress: PROGRESS_CB | None = None
    ) -> str:
        """Upload one file - or a whole directory tree - to the agent."""
        source = Path(local_path)
        relative = remote_relative.lstrip("/")
        if source.is_dir():
            total_files = sum(1 for _ in source.rglob("*") if _.is_file())
            done = 0

            def bump(_result: Any = None, _size: int = 0) -> None:
                nonlocal done
                done += 1
                if progress:
                    progress(done, max(done, total_files))

            for root, _dirs, files in os.walk(source):
                for filename in files:
                    local_file = Path(root) / filename
                    rel = local_file.relative_to(source)
                    target = f"{relative}/{rel.as_posix()}"
                    self.upload(local_file, target, None)
                    bump(None, local_file.stat().st_size)
            if progress:
                progress(total_files, total_files)
            return relative

        with self._xfer_lock:
            if self.ssh and self.ssh.connected:
                remote_abs = self.profile.agent_data_root.rstrip("/") + "/" + relative
                return self.ssh.upload(local_path, remote_abs, progress)
            if self.rest and self.rest.connected:
                return self.rest.upload(local_path, relative, progress)
        raise AgentError("no transport available for upload", ERR_CONNECTION)

    # -- jobs ----------------------------------------------------------- #

    def iter_job_updates(
        self, job_id: str, stop: threading.Event | None = None
    ) -> Iterator[dict[str, Any]]:
        if self.rest and self.rest.connected:
            yield from self.rest.iter_job_updates(job_id, stop=stop)
            return
        if self.ssh and self.ssh.connected:
            yield from self.ssh.iter_job_updates(job_id, stop=stop)
            return
        raise AgentError("not connected", ERR_CONNECTION)

    def job_snapshot(self, job_id: str) -> JobSnapshot:
        return JobSnapshot.from_dict(self.call("job_status", {"job_id": job_id}))

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return JobSnapshot.from_dict(self.call("job_cancel", {"job_id": job_id}))
