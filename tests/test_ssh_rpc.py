"""SSH transport tests: the rpc frontend plus a mocked paramiko channel."""

import io
import json

import pytest

from agent.config import AgentConfig
from agent.core import AgentCore
from agent.ssh_rpc import serve
from client.profiles import Profile
from client.transport import SshTransport
from common.protocol import AgentError, make_request


@pytest.fixture()
def core(tmp_path):
    cfg = AgentConfig(
        data_root=tmp_path / "data",
        state_dir=tmp_path / "state",
        host="127.0.0.1",
        port=0,
        require_tls=False,
        token="t",
    )
    return AgentCore(cfg)


class TestRpcFrontend:
    def _run(self, core, raw: str):
        stdin = io.StringIO(raw)
        stdout = io.StringIO()
        code = serve(core, stdin, stdout)
        return code, stdout.getvalue()

    def test_ping_roundtrip(self, core):
        code, out = self._run(core, json.dumps(make_request("ping")))
        assert code == 0
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["result"]["pong"] is True

    def test_single_line_output(self, core):
        code, out = self._run(core, json.dumps(make_request("version")))
        assert out.count("\n") == 1
        assert code == 0

    def test_malformed_json(self, core):
        code, out = self._run(core, "{not json")
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_request"

    def test_empty_stdin(self, core):
        code, out = self._run(core, "")
        payload = json.loads(out)
        assert payload["ok"] is False

    def test_unknown_op_exits_nonzero(self, core):
        code, out = self._run(core, json.dumps(make_request("drop_table")))
        assert code == 1
        assert json.loads(out)["error"]["code"] == "unknown_op"

    def test_device_rejection(self, core):
        code, out = self._run(
            core, json.dumps(make_request("eject", {"device": "/dev/sda"}))
        )
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_device"

    def test_bytes_stdin(self, core):
        stdin = io.BytesIO(json.dumps(make_request("ping")).encode())
        stdout = io.StringIO()
        assert serve(core, stdin, stdout) == 0
        assert json.loads(stdout.getvalue())["ok"] is True


# --------------------------------------------------------------------------- #
# mocked paramiko channel
# --------------------------------------------------------------------------- #


class FakeChannelFile(io.BytesIO):
    """paramiko stdout/stderr read() returns bytes."""

    def __init__(self, text: str) -> None:
        super().__init__(text.encode("utf-8"))


class FakeStdin(io.StringIO):
    def shutdown_write(self) -> None:
        pass


class FakeSSHClient:
    def __init__(self, response: str, stderr: str = ""):
        self.response = response
        self.stderr = stderr
        self.commands: list[str] = []

    def exec_command(self, command, timeout=None, get_pty=False):
        self.commands.append(command)
        return FakeStdin(""), FakeChannelFile(self.response), FakeChannelFile(self.stderr)


def make_transport(response: str, stderr: str = "") -> SshTransport:
    profile = Profile(name="t", host="10.0.0.9", user="tester", agent_cmd="rodm-agent")
    transport = SshTransport(profile, timeout=5)
    transport._client = FakeSSHClient(response, stderr)
    transport.connected = True
    return transport


class TestSshCall:
    def test_successful_call(self):
        response = json.dumps({"v": 1, "id": "x", "ok": True, "result": {"pong": True}})
        transport = make_transport(response)
        assert transport.call("ping") == {"pong": True}
        assert transport._client.commands == ["rodm-agent rpc"]

    def test_agent_error_code_is_preserved(self):
        response = json.dumps(
            {
                "v": 1,
                "id": "x",
                "ok": False,
                "error": {"code": "no_media", "message": "no disc"},
            }
        )
        transport = make_transport(response)
        with pytest.raises(AgentError) as exc:
            transport.call("eject", {"device": "/dev/sr0"})
        assert exc.value.code == "no_media"
        assert exc.value.message == "no disc"

    def test_stderr_only_is_a_connection_error(self):
        transport = make_transport("", stderr="bash: rodm-agent: command not found")
        with pytest.raises(AgentError) as exc:
            transport.call("ping")
        assert exc.value.code == "connection_error"
        assert "command not found" in exc.value.message

    def test_garbage_output_is_a_connection_error(self):
        transport = make_transport("Traceback (most recent call last): ...")
        with pytest.raises(AgentError) as exc:
            transport.call("ping")
        assert exc.value.code == "connection_error"

    def test_not_connected(self):
        profile = Profile(name="t", host="10.0.0.9", user="tester")
        transport = SshTransport(profile)
        with pytest.raises(AgentError) as exc:
            transport.call("ping")
        assert exc.value.code == "connection_error"

    def test_response_with_leading_noise_is_tolerated(self):
        line = json.dumps({"v": 1, "id": "x", "ok": True, "result": 42})
        transport = make_transport("Warning: something\n" + line + "\n")
        assert transport.call("ping") == 42

    def test_job_event_iteration_polls_until_done(self):
        events = [
            {
                "v": 1,
                "id": "x",
                "ok": True,
                "result": {
                    "job": {"state": "running", "progress": 0.5},
                    "events": [{"seq": 1, "kind": "progress", "data": {"progress": 0.5}}],
                },
            },
            {
                "v": 1,
                "id": "x",
                "ok": True,
                "result": {"job": {"state": "done", "progress": 1.0}, "events": []},
            },
        ]
        transport = make_transport("")
        transport._client = None

        # make call() return canned responses
        calls = {"n": 0}

        def fake_call(op, params=None, timeout=60.0):
            payload = events[min(calls["n"], len(events) - 1)]
            calls["n"] += 1
            return payload["result"]

        transport.call = fake_call  # type: ignore[method-assign]
        updates = list(transport.iter_job_updates("job1", poll=0))
        states = [u["job"]["state"] for u in updates if u["type"] == "status"]
        assert states[0] == "running"
        assert states[-1] == "done"
        assert any(u["type"] == "event" for u in updates)


class TestRunCommand:
    def test_stdout_returned(self):
        transport = make_transport("secret-token\n", stderr="some warning")
        assert transport.run_command("rodm-agent token").strip() == "secret-token"

    def test_stderr_only_raises(self):
        transport = make_transport("", stderr="nope")
        with pytest.raises(AgentError) as exc:
            transport.run_command("rodm-agent token")
        assert exc.value.message == "nope"
