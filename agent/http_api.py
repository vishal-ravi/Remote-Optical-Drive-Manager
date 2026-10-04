"""REST frontend built on the Python standard library (no third-party deps).

Endpoints (all but ``/v1/health`` require ``Authorization: Bearer <token>``):

``GET  /v1/health``                      liveness probe
``POST /v1/rpc``                         the same JSON envelope as SSH-RPC
``GET  /v1/jobs/<id>/events?after=N``    NDJSON progress stream
``GET  /v1/file?device=..&path=..``      stream a file off the mounted disc
``GET  /v1/images``                      list ISOs in the images folder
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common.protocol import (
    ERR_UNAUTHORIZED,
    ERR_VALIDATION,
    AgentError,
    parse_request,
)
from common.validation import (
    resolve_in_root,
    validate_device,
    validate_job_id,
)
from . import tools
from .config import AgentConfig, is_loopback
from .core import AgentCore, AGENT_VERSION
from .ops.images import list_images
from .ops.media import resolve_media_path

log = logging.getLogger("rodm.agent.http")

IDLE_STREAM_TIMEOUT = 25.0  # seconds of silence before a stream ends
MAX_EVENT_BATCH = 500


def generate_tls_pair(cert: Path, key: Path) -> None:
    """Create a self-signed certificate for the agent's REST endpoint."""
    openssl = tools.require("openssl")
    hostname = socket.gethostname()
    addresses = {"DNS:" + hostname, "IP:127.0.0.1"}
    try:
        addresses.add("IP:" + socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    for target in _local_ips():
        addresses.add("IP:" + target)
    san = ",".join(sorted(addresses))
    cert.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        openssl,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-days",
        "825",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-subj",
        f"/CN={hostname}/O=Remote Optical Drive Manager",
        "-addext",
        f"subjectAltName={san}",
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    log.info("generated self-signed TLS certificate for %s", san)


def _local_ips() -> list[str]:
    """Best-effort list of this host's IPv4 addresses (for certificate SANs)."""
    found: list[str] = []

    def add(ip: str) -> None:
        if ip and ip not in found:
            found.append(ip)

    # 1. every address configured on an interface (iproute2)
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
        for line in out.splitlines():
            fields = line.split()
            if "inet" in fields:
                add(fields[fields.index("inet") + 1].split("/")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    # 2. the address an outbound connection would use (no packet is sent)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))  # TEST-NET-1, never routed
            add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    # 3. whatever the hostname resolves to
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    add("127.0.0.1")
    return [ip for ip in found if not ip.startswith("169.254.")]


def _cert_ip_sans(cert: Path) -> list[str]:
    """Extract the IP addresses listed in an existing certificate's SAN."""
    openssl = tools.require("openssl")
    try:
        out = subprocess.run(
            [openssl, "x509", "-in", str(cert), "-noout", "-text"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ips: list[str] = []
    for line in out.splitlines():
        # one SAN line may hold several "IP Address:a.b.c.d, IP Address:..." entries
        for segment in line.split("IP Address:")[1:]:
            ip = segment.split(",")[0].strip()
            if ip and ip not in ips:
                ips.append(ip)
    return ips


def _cert_needs_refresh(cert: Path) -> bool:
    """True when the certificate does not cover any current interface address."""
    if not cert.exists():
        return True
    covered = _cert_ip_sans(cert)
    current = [ip for ip in _local_ips() if ip != "127.0.0.1"]
    if not current:
        return not covered  # loopback-only host: any prior cert is fine
    return not any(ip in covered for ip in current)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"rodm-agent/{AGENT_VERSION}"
    sys_version = ""

    core: AgentCore  # set on the server instance
    config: AgentConfig

    # ------------------------------------------------------------------ #

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # helpers ------------------------------------------------------------ #

    @property
    def _token(self) -> str:
        return self.config.token or ""

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer ") :].strip()
        return bool(self._token) and hmac.compare_digest(supplied, self._token)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: str, message: str, status: int) -> None:
        self._send_json({"ok": False, "error": {"code": code, "message": message}}, status)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_error(ERR_UNAUTHORIZED, "missing or invalid bearer token", 401)
        return False

    def _read_body(self, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > max_bytes:
            raise AgentError("request body missing or too large", ERR_VALIDATION)
        return self.rfile.read(length)

    def _handle_upload(self, relative: str) -> None:
        """Raw-body upload into the agent data root (staging area)."""
        if not relative:
            self._send_error(ERR_VALIDATION, "missing ?path=", 400)
            return
        target = resolve_in_root(self.config.data_root, relative)
        if target.exists() and target.is_dir():
            target = target / "upload.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_error(ERR_VALIDATION, "missing Content-Length", 400)
            return
        if length > self.config.max_iso_bytes:
            self._send_error(ERR_VALIDATION, "upload too large", 413)
            return
        remaining = length
        tmp = target.with_name(target.name + ".part")
        with tmp.open("wb") as handle:
            while remaining > 0:
                chunk = self.rfile.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)
        if remaining:
            tmp.unlink(missing_ok=True)
            self._send_error(ERR_VALIDATION, "truncated upload", 400)
            return
        os.replace(tmp, target)
        log.info("uploaded %s (%d bytes)", target, length)
        self._send_json(
            {
                "ok": True,
                "result": {
                    "path": str(target.relative_to(self.config.data_root)),
                    "size": length,
                },
            }
        )

    # ------------------------------------------------------------------ #
    # GET
    # ------------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if route == "/v1/health":
                self._send_json(
                    {
                        "ok": True,
                        "service": "rodm-agent",
                        "version": AGENT_VERSION,
                        "time": time.time(),
                    }
                )
                return

            if not self._require_auth():
                return

            if route.startswith("/v1/jobs/") and route.endswith("/events"):
                job_id = validate_job_id(route[len("/v1/jobs/") : -len("/events")])
                after = _first_int(query, "after", 0)
                self._stream_events(job_id, after)
                return

            if route == "/v1/file":
                device = _first(query, "device")
                path = _first(query, "path", "/")
                self._stream_file(device, path)
                return

            if route == "/v1/images":
                self._send_json({"ok": True, "result": {"images": list_images(self.config)}})
                return

            self._send_error("not_found", f"no route for GET {route}", 404)
        except AgentError as exc:
            self._send_error(exc.code, exc.message, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover
            log.exception("GET %s failed", self.path)
            try:
                self._send_error("internal_error", str(exc), 500)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # POST
    # ------------------------------------------------------------------ #

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/")
            if route == "/v1/upload":
                if not self._require_auth():
                    return
                query = parse_qs(parsed.query)
                self._handle_upload(_first(query, "path"))
                return
            if route != "/v1/rpc":
                self._send_error("not_found", f"no route for POST {route}", 404)
                return
            if not self._require_auth():
                return
            raw = self._read_body(max_bytes=16 * 1024 * 1024)
            try:
                request = parse_request(raw)
            except Exception as exc:
                self._send_error(ERR_VALIDATION, str(exc), 400)
                return
            response = self.core.dispatch(request)
            status = 200 if response.get("ok") else _status_for(response)
            self._send_json(response, status)
        except AgentError as exc:
            self._send_error(exc.code, exc.message, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover
            log.exception("POST %s failed", self.path)
            try:
                self._send_error("internal_error", str(exc), 500)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # streams
    # ------------------------------------------------------------------ #

    def _stream_events(self, job_id: str, after: int) -> None:
        """Newline-delimited JSON: one event object per line.

        Ends when the job reaches a terminal state or the idle timeout hits.
        """
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        deadline = time.monotonic() + IDLE_STREAM_TIMEOUT
        cursor = after
        try:
            while time.monotonic() < deadline:
                snapshot = self.core.jobs.snapshot(job_id)
                events = self.core.jobs.events(job_id, cursor)
                if events:
                    for event in events:
                        line = json.dumps(
                            {"type": "event", "event": event.to_dict()},
                            separators=(",", ":"),
                        )
                        self.wfile.write(line.encode("utf-8") + b"\n")
                    cursor = events[-1].seq
                    deadline = time.monotonic() + IDLE_STREAM_TIMEOUT
                line = json.dumps(
                    {"type": "status", "job": snapshot.to_dict()},
                    separators=(",", ":"),
                )
                self.wfile.write(line.encode("utf-8") + b"\n")
                self.wfile.flush()
                if snapshot.state in ("done", "failed", "cancelled"):
                    return
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            return

    def _stream_file(self, device: str, path: str) -> None:
        target = resolve_media_path(self.config, validate_device(device), path)
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{target.name}"'.replace('"', ""),
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        self.wfile.flush()


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _first_int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(_first(query, key, str(default)))
    except ValueError:
        return default


def _status_for(response: dict) -> int:
    code = (response.get("error") or {}).get("code", "")
    return {
        "unauthorized": 401,
        "invalid_device": 400,
        "validation_error": 400,
        "not_found": 404,
        "job_not_found": 404,
        "conflict": 409,
        "unknown_op": 404,
    }.get(code, 200)


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    core: AgentCore
    config: AgentConfig


def build_server(core: AgentCore, config: AgentConfig) -> AgentHTTPServer:
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"core": core, "config": config},
    )
    server = AgentHTTPServer((config.host, config.port), handler)
    server.core = core
    server.config = config

    # TLS is used whenever it was explicitly required or a certificate is
    # already present; plaintext only happens with --insecure on loopback.
    use_tls = config.require_tls or config.cert_path.exists()
    if use_tls and _cert_needs_refresh(config.cert_path):
        generate_tls_pair(config.cert_path, config.key_path)
    if use_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(config.cert_path), str(config.key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    else:
        log.warning(
            "serving PLAINTEXT HTTP on %s:%s - reachable only via loopback "
            "unless you change --host",
            config.host,
            config.port,
        )
    return server


def serve(core: AgentCore, config: AgentConfig,
          stop_event: "threading.Event | None" = None) -> None:
    if not is_loopback(config.host) and not config.cert_path.exists():
        if not config.require_tls:
            log.warning(
                "serving PLAINTEXT HTTP on %s:%s - anyone on the network can "
                "issue commands if it guesses the token",
                config.host,
                config.port,
            )
    server = build_server(core, config)
    if stop_event is not None:
        # serve_forever() only returns when shutdown() is called from another
        # thread; a signal handler must not call it directly (deadlock).
        def _watch() -> None:
            stop_event.wait()
            server.shutdown()
        threading.Thread(target=_watch, daemon=True).start()
    scheme = "https" if isinstance(server.socket, ssl.SSLSocket) else "http"
    log.info(
        "REST API listening on %s://%s:%s (token file: %s)",
        scheme,
        config.host,
        config.port,
        config.token_path,
    )
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted")
    finally:
        server.shutdown()
        server.server_close()
