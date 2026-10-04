"""Command line interface: ``rodm-agent`` / ``python3 -m agent``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from common.logging_setup import format_bytes, setup_logging
from common.protocol import AgentError

from .config import DEFAULT_PORT, AgentConfig, default_data_root
from .core import AGENT_VERSION, AgentCore

log = logging.getLogger("rodm.agent")


def _build_config(args: argparse.Namespace) -> AgentConfig:
    cfg = AgentConfig()
    if getattr(args, "data_root", None):
        cfg.data_root = Path(args.data_root).expanduser()
    if getattr(args, "state_dir", None):
        cfg.state_dir = Path(args.state_dir).expanduser()
    if getattr(args, "host", None):
        cfg.host = args.host
    if getattr(args, "port", None):
        cfg.port = int(args.port)
    if getattr(args, "insecure", False):
        cfg.require_tls = False
    if getattr(args, "log_level", None):
        cfg.log_level = str(args.log_level)
    return cfg


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-root",
        help=f"agent data directory (default: {default_data_root()})",
    )
    parser.add_argument("--state-dir", help="agent state/log directory")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = int(args.port)
    if args.insecure:
        cfg.require_tls = False

    cfg.ensure_dirs()
    cfg.load_or_create_token()
    setup_logging(
        "rodm.agent", log_dir=cfg.log_dir, level=_level(cfg.log_level), log_file="agent.log"
    )
    setup_logging(
        "rodm.agent.http", log_dir=cfg.log_dir, level=_level(cfg.log_level), log_file="agent.log"
    )

    from . import http_api

    core = AgentCore(cfg)
    log.info(
        "rodm-agent %s starting (data=%s, bind=%s:%s, tls=%s)",
        AGENT_VERSION,
        cfg.data_root,
        cfg.host,
        cfg.port,
        "on" if cfg.require_tls or cfg.cert_path.exists() else "OFF",
    )
    log.info("bearer token: %s", cfg.token)
    log.info("token file: %s", cfg.token_path)

    stopping = threading.Event()

    def _handle(signum, frame):  # pragma: no cover
        log.info("signal %s received, shutting down", signal.Signals(signum).name)
        stopping.set()
        core.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except ValueError:  # pragma: no cover - non-main thread
            pass

    try:
        http_api.serve(core, cfg, stopping)
    finally:
        core.shutdown()
    return 0


def cmd_rpc(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    cfg.ensure_dirs()
    setup_logging("rodm.agent", log_dir=None, level=logging.WARNING, console=False)
    from . import ssh_rpc

    return ssh_rpc.run(cfg)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    cfg.ensure_dirs()
    from .drives import list_optical_drives
    from .tools import available_tools

    drives = list_optical_drives()
    if not drives:
        print("no optical drives found")
    for drive in drives:
        print(f"{drive.device}")
        print(f"  model     : {drive.vendor} {drive.model}".rstrip())
        print(f"  media     : {'inserted' if drive.media_present else 'empty'}")
        if drive.media_present:
            print(f"  disc type : {drive.disc_type or 'unknown'}")
            print(f"  label     : {drive.label or '-'}")
            print(f"  fs        : {drive.fstype or '-'}")
            print(f"  capacity  : {format_bytes(int(drive.size)) if drive.size else '?'}")
        print(f"  writable  : {'yes' if drive.writable else 'no'}")
        print(f"  mounted   : {drive.mountpoint or 'no'}")
        print()

    if args.tools:
        print("tools:")
        for name, path in available_tools().items():
            print(f"  {name:<14} {path or 'MISSING'}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from common.protocol import PROTOCOL_VERSION

    print(f"rodm-agent {AGENT_VERSION} (protocol {PROTOCOL_VERSION})")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    token = cfg.load_or_create_token()
    print(token)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    cfg.ensure_dirs()
    from .tools import available_tools

    problems = 0
    print(f"data root   : {cfg.data_root} ({'ok' if cfg.data_root.exists() else 'MISSING'})")
    print(f"state dir   : {cfg.state_dir}")
    for name, path in available_tools().items():
        status = path or "MISSING"
        if path is None and name in ("lsblk", "udevadm", "eject", "dd"):
            problems += 1
        print(f"  {name:<14} {status}")

    token = cfg.read_token()
    print(f"token       : {'set' if token else 'NOT SET (run: rodm-agent token)'}")
    if not token:
        problems += 1

    try:
        from .drives import list_optical_drives

        drives = list_optical_drives()
        print(f"drives      : {[d.device for d in drives] or 'none found'}")
    except Exception as exc:  # pragma: no cover
        problems += 1
        print(f"drives      : error: {exc}")

    try:
        import os

        writable = os.access(cfg.data_root, os.W_OK)
        print(f"write access: {'ok' if writable else 'DENIED'}")
        if not writable:
            problems += 1
    except OSError as exc:  # pragma: no cover
        print(f"write access: error {exc}")
        problems += 1

    print(f"\n{'doctor: OK' if problems == 0 else f'doctor: {problems} problem(s)'}")
    return 0 if problems == 0 else 1


def _level(name: str) -> int:
    return getattr(logging, str(name).upper(), logging.INFO)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rodm-agent",
        description="Remote Optical Drive Manager - restricted agent service",
    )
    parser.add_argument("--version", action="version", version=f"rodm-agent {AGENT_VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_serve = sub.add_parser("serve", help="run the REST API service")
    _add_common(p_serve)
    p_serve.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument(
        "--insecure",
        action="store_true",
        help="allow plaintext HTTP (loopback testing only)",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_rpc = sub.add_parser("rpc", help="serve one JSON request from stdin (SSH mode)")
    _add_common(p_rpc)
    p_rpc.set_defaults(func=cmd_rpc)

    p_status = sub.add_parser("status", help="list optical drives on this machine")
    _add_common(p_status)
    p_status.add_argument("--tools", action="store_true", help="also list utility paths")
    p_status.set_defaults(func=cmd_status)

    p_token = sub.add_parser("token", help="print (or create) the REST bearer token")
    _add_common(p_token)
    p_token.set_defaults(func=cmd_token)

    p_doctor = sub.add_parser("doctor", help="check tools, paths and permissions")
    _add_common(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_ver = sub.add_parser("version", help="print version")
    p_ver.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except AgentError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
