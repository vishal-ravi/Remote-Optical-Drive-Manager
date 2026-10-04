"""Subprocess helpers with progress parsing, cancellation and timeouts."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import time
from typing import Any, Callable, Iterable

from common.protocol import ERR_TOOL_FAILED, AgentError

log = logging.getLogger("rodm.agent.process")

PERCENT_RE = re.compile(r"(?<![-\d.])(\d{1,3}(?:\.\d+)?)\s*%")
DD_BYTES_RE = re.compile(r"(\d+)\s+bytes")
TIMEOUT_DEFAULT = 60 * 60  # one hour: burns are slow but not that slow


def humanize(cmd: Iterable[str]) -> str:
    return " ".join(str(part) for part in cmd)


def extract_percent(line: str) -> float | None:
    matches = PERCENT_RE.findall(line)
    if not matches:
        return None
    try:
        value = float(matches[-1])
    except ValueError:  # pragma: no cover - regex guarantees numeric
        return None
    if value < 0 or value > 100:
        return None
    return value / 100.0


def extract_dd_bytes(line: str) -> int | None:
    match = DD_BYTES_RE.search(line.replace("\r", " "))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover
        return None


def kill_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:  # pragma: no cover
        return
    for _ in range(20):
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        proc.kill()
    except OSError:  # pragma: no cover
        pass


def _split_chunks(text: str) -> list[str]:
    """Tools rewrite progress with CR; treat both CR and LF as separators."""
    return [chunk for chunk in re.split(r"[\r\n]+", text) if chunk.strip()]


_SUDO_OK: bool | None = None


def _sudo_available() -> bool:
    """True when this user may run commands through ``sudo -n`` (cached)."""
    global _SUDO_OK
    if os.geteuid() == 0:
        return False
    if _SUDO_OK is None:
        sudo = shutil.which("sudo")
        if not sudo:
            _SUDO_OK = False
        else:
            try:
                probe = subprocess.run(
                    [sudo, "-n", "true"], capture_output=True, timeout=10, check=False
                )
                _SUDO_OK = probe.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _SUDO_OK = False
    return _SUDO_OK


def privileged(cmd: list[str]) -> list[str]:
    """Prefix ``cmd`` with ``sudo -n`` when passwordless sudo is configured.

    Without a sudoers rule the command is returned unchanged so it can still
    succeed on its own (e.g. ``eject`` works for group ``cdrom``).
    """
    if not _sudo_available():
        return list(cmd)
    sudo = shutil.which("sudo")
    return [sudo, "-n", *cmd] if sudo else list(cmd)


def run_checked(
    cmd: list[str],
    *,
    timeout: float = 120.0,
    cwd: str | os.PathLike[str] | None = None,
    input_text: str | None = None,
    error_hint: str = "",
    env: dict[str, str] | None = None,
    escalate: bool = False,
) -> subprocess.CompletedProcess:
    """Run a short command, raising :class:`AgentError` on failure.

    With ``escalate=True`` the command is attempted with ``sudo -n`` first
    (when configured) and retried unprivileged if that fails, so one code
    path works for both root and user service deployments.
    """
    attempts = [privileged(cmd)] if escalate else []
    if not attempts or attempts[0] != cmd:
        if escalate:
            attempts.append(list(cmd))
        else:
            attempts = [list(cmd)]
    last: AgentError | None = None
    for attempt in attempts:
        try:
            return _run_once(
                attempt,
                timeout=timeout,
                cwd=cwd,
                input_text=input_text,
                error_hint=error_hint,
                env=env,
            )
        except AgentError as exc:
            last = exc
    raise last if last is not None else AgentError("command failed", ERR_TOOL_FAILED)


def _run_once(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None,
    input_text: str | None,
    error_hint: str,
    env: dict[str, str] | None,
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            input=input_text,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise AgentError(f"utility not found: {cmd[0]}", ERR_TOOL_FAILED) from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError(
            f"'{humanize(cmd)}' timed out after {int(timeout)}s", ERR_TOOL_FAILED
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if len(detail) > 800:
            detail = detail[-800:]
        message = f"'{humanize(cmd)}' failed (exit {proc.returncode})"
        if detail:
            message += f": {detail}"
        if error_hint:
            message += f" ({error_hint})"
        raise AgentError(message, ERR_TOOL_FAILED)
    return proc


def stream_command(
    cmd: list[str],
    *,
    on_line: Callable[[str], None] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = TIMEOUT_DEFAULT,
    cancel_check: Callable[[], None] | None = None,
    kill_on_cancel: Any = None,
    env: dict[str, str] | None = None,
    escalate: bool = False,
) -> int:
    """Run a long command, streaming merged stdout+stderr line by line.

    Returns the exit status. ``kill_on_cancel`` (a :class:`Job`) gets its
    ``terminate`` hook installed so the client can abort mid-flight.
    ``escalate`` decides privilege *before* starting, so a long job is never
    restarted halfway through under a different user.
    """
    if escalate:
        cmd = privileged(cmd)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise AgentError(f"utility not found: {cmd[0]}", ERR_TOOL_FAILED) from exc

    if kill_on_cancel is not None:
        kill_on_cancel.terminate = lambda: kill_process(proc)  # type: ignore[attr-defined]

    started = time.monotonic()
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        buffer = ""
        while True:
            if cancel_check is not None:
                cancel_check()
            if time.monotonic() - started > timeout:
                kill_process(proc)
                raise AgentError(
                    f"'{humanize(cmd)}' exceeded {int(timeout)}s", ERR_TOOL_FAILED
                )
            chunk = proc.stdout.readline()
            if chunk == "":
                if proc.poll() is not None:
                    break
                continue
            buffer += chunk
            if "\n" in buffer or "\r" in buffer:
                for line in _split_chunks(buffer):
                    tail.append(line)
                    if len(tail) > 60:
                        tail.pop(0)
                    if on_line:
                        on_line(line)
                buffer = ""
        remainder = buffer.strip()
        if remainder:
            tail.append(remainder)
            if on_line:
                on_line(remainder)
    finally:
        if proc.poll() is None:
            kill_process(proc)

    status = proc.wait()
    if status != 0:
        detail = "\n".join(tail[-8:]).strip()
        if len(detail) > 800:
            detail = detail[-800:]
        message = f"'{humanize(cmd)}' failed (exit {status})"
        if detail:
            message += f": {detail}"
        raise AgentError(message, ERR_TOOL_FAILED)
    return status


def which_or_none(name: str) -> str | None:
    return shutil.which(name)


def sigterm_group(proc: subprocess.Popen) -> None:  # pragma: no cover - helper
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        proc.terminate()
