"""SSH frontend: one JSON request on stdin, one JSON response on stdout.

Invoked by the client through ``paramiko`` as ``python3 -m agent rpc``.
Bulk transfers use SFTP instead of this channel.
"""

from __future__ import annotations

import sys

from common.protocol import (
    ERR_INTERNAL,
    ProtocolError,
    encode,
    make_error,
    parse_request,
)
from .core import AgentCore


def serve(core: AgentCore, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    raw = stdin.read()
    try:
        request = parse_request(raw)
    except ProtocolError as exc:
        stdout.write(encode(make_error(None, exc.code, exc.message)) + "\n")
        stdout.flush()
        return 0
    response = core.dispatch(request)
    stdout.write(encode(response) + "\n")
    stdout.flush()
    return 0 if response.get("ok") else 1


def run(config=None) -> int:
    from .config import AgentConfig

    core = AgentCore(config or AgentConfig())
    try:
        return serve(core)
    except Exception as exc:  # pragma: no cover - last resort
        sys.stdout.write(
            encode(make_error(None, ERR_INTERNAL, f"{type(exc).__name__}: {exc}")) + "\n"
        )
        sys.stdout.flush()
        return 1
    finally:
        core.shutdown()
