import pytest

from agent.process import extract_dd_bytes, extract_percent, humanize
from common.protocol import AgentError


class TestPercentParser:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Writing: 123/1024  12.0% done", 0.12),
            ("100% done", 1.0),
            ("  0.5%  ", 0.005),
            ("no numbers here", None),
            ("250%", None),
            ("-5%", None),
            ("progress 42.42%", 0.4242),
            ("multiple 10% then 20%", 0.2),
        ],
    )
    def test_extract_percent(self, line, expected):
        assert extract_percent(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            (
                "1234567890 bytes (1.2 GB, 1.1 GiB) copied, 3.4 s, 3.5 MB/s",
                1234567890,
            ),
            ("8192 bytes copied, 0.1 s", 8192),
            ("nothing to see", None),
            ("999999999999999999999 bytes (", 999999999999999999999),
        ],
    )
    def test_extract_dd_bytes(self, line, expected):
        assert extract_dd_bytes(line) == expected


class TestHumanize:
    def test_joins_command(self):
        assert humanize(["wodim", "dev=/dev/sr0", "-data"]) == "wodim dev=/dev/sr0 -data"

    def test_stringifies(self):
        assert humanize(["dd", 42]) == "dd 42"


class TestStreamCommand:
    def test_success_and_line_callback(self, tmp_path):
        from agent.process import stream_command

        seen = []
        stream_command(["/bin/echo", "hello"], on_line=seen.append)
        assert "hello" in seen

    def test_failure_raises_agent_error(self):
        from agent.process import stream_command

        with pytest.raises(AgentError) as exc:
            stream_command(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"])
        assert exc.value.code == "tool_failed"
        assert "exit 3" in exc.value.message

    def test_missing_binary(self):
        from agent.process import stream_command

        with pytest.raises(AgentError) as exc:
            stream_command(["/definitely/not/here"])
        assert "not found" in exc.value.message

    def test_cancel_check_aborts(self):
        from agent.process import stream_command

        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("stop")

        script = "while :; do echo tick; sleep 0.05; done"
        with pytest.raises(RuntimeError):
            stream_command(["/bin/sh", "-c", script], cancel_check=cancel)
        assert calls["n"] > 2

    def test_run_checked_ok(self):
        from agent.process import run_checked

        proc = run_checked(["/bin/echo", "ok"])
        assert proc.stdout.strip() == "ok"

    def test_run_checked_timeout(self):
        from agent.process import run_checked

        with pytest.raises(AgentError) as exc:
            run_checked(["/bin/sleep", "5"], timeout=0.2)
        assert "timed out" in exc.value.message


class TestPrivilegeEscalation:
    """``escalate=True`` must use passwordless sudo when it exists and still
    work (unprivileged) when it does not."""

    @pytest.fixture(autouse=True)
    def _fresh_sudo_cache(self, monkeypatch):
        import agent.process as process

        monkeypatch.setattr(process, "_SUDO_OK", None)
        monkeypatch.setattr(process.os, "geteuid", lambda: 1000)
        yield

    @staticmethod
    def _fake_sudo(tmp_path, *, allow_cmd: bool, marker=None):

        sudo = tmp_path / "sudo"
        body = '#!/bin/sh\n[ "$1" = "-n" ] && shift\nif [ "$1" = "true" ]; then exit 0; fi\n'
        if marker is not None:
            body += f'echo used >> "{marker}"\n'
        body += 'exec "$@"\n' if allow_cmd else "exit 1\n"
        sudo.write_text(body)
        sudo.chmod(0o755)
        return sudo

    def test_escalate_runs_command_through_sudo(self, tmp_path, monkeypatch):
        import os
        import sys

        import agent.process as process

        marker = tmp_path / "sudo-used"
        self._fake_sudo(tmp_path, allow_cmd=True, marker=marker)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        proc = process.run_checked(
            [sys.executable, "-c", "print('ok')"], escalate=True
        )
        assert proc.stdout.strip() == "ok"
        assert marker.exists(), "sudo -n should have been used"

    def test_escalate_falls_back_to_plain_command(self, tmp_path, monkeypatch):
        import os
        import sys

        import agent.process as process

        self._fake_sudo(tmp_path, allow_cmd=False)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        proc = process.run_checked(
            [sys.executable, "-c", "print('plain')"], escalate=True
        )
        assert proc.stdout.strip() == "plain"

    def test_privileged_is_identity_without_sudo(self, monkeypatch):
        import agent.process as process

        monkeypatch.setattr(process, "_SUDO_OK", False)
        cmd = ["/bin/mount", "-o", "ro", "/dev/sr0", "/mnt/x"]
        assert process.privileged(cmd) == cmd

    def test_stream_command_escalates_before_start(self, tmp_path, monkeypatch):
        import os
        import sys

        import agent.process as process

        marker = tmp_path / "sudo-used"
        self._fake_sudo(tmp_path, allow_cmd=True, marker=marker)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        lines: list[str] = []
        rc = process.stream_command(
            [sys.executable, "-c", "print('burn')"],
            on_line=lines.append,
            escalate=True,
        )
        assert rc == 0
        assert lines == ["burn"]
        assert marker.exists()
