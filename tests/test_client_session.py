"""ClientSession transport choice: REST preferred, SSH fallback, job gate."""

from __future__ import annotations

import pytest

from client.profiles import Profile
from client.transport import ERR_CONNECTION, ClientSession
from common.protocol import ERR_UNAUTHORIZED, AgentError


class FakeTransport:
    def __init__(self, kind, reply=None):
        self.kind = kind
        self.connected = True
        self.calls = []
        self.reply = reply if reply is not None else {"ok": True}

    def call(self, op, params=None, timeout=60.0):
        self.calls.append(op)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self):
        self.connected = False


def _session(*, ssh=True, rest=True, rest_reply=None):
    session = ClientSession(Profile(name="t", host="example", rest_token="tok"))
    if ssh:
        session.ssh = FakeTransport("ssh")
    if rest:
        session.rest = FakeTransport("rest", rest_reply)
    return session


class TestTransportPreference:
    def test_rpc_prefers_rest(self):
        session = _session()
        session.call("eject", {"device": "/dev/sr0"})
        assert session.rest.calls == ["eject"]
        assert session.ssh.calls == []

    def test_rpc_falls_back_to_ssh_when_rest_down(self):
        session = _session(rest=False)
        session.call("eject", {"device": "/dev/sr0"})
        assert session.ssh.calls == ["eject"]

    def test_connection_error_falls_back(self):
        session = _session(rest_reply=AgentError("network gone", ERR_CONNECTION))
        session.ssh.reply = {"recovered": True}
        assert session.call("eject", {"device": "/dev/sr0"}) == {"recovered": True}
        assert session.ssh.calls == ["eject"]

    def test_permission_error_does_not_fall_back_to_weaker_transport(self):
        session = _session(rest_reply=AgentError("must be superuser", "tool_failed"))
        with pytest.raises(AgentError):
            session.call("unmount", {"device": "/dev/sr0"})
        assert session.ssh.calls == []

    def test_auth_error_does_not_fall_back(self):
        session = _session(rest_reply=AgentError("bad token", ERR_UNAUTHORIZED))
        with pytest.raises(AgentError):
            session.call("drive_status", {"device": "/dev/sr0"})
        assert session.ssh.calls == []


class TestJobOpsNeedRest:
    def test_job_op_with_ssh_only_raises_clear_error(self):
        session = _session(rest=False)
        with pytest.raises(AgentError) as exc:
            session.call("burn_iso", {"device": "/dev/sr0", "name": "x.iso"})
        assert exc.value.code == ERR_CONNECTION
        assert "REST" in exc.value.message
        assert session.ssh.calls == []

    def test_job_op_goes_to_rest(self):
        session = _session(rest_reply={"job_id": "j-1"})
        result = session.call("burn_iso", {"device": "/dev/sr0", "name": "x.iso"})
        assert result == {"job_id": "j-1"}
        assert session.rest.calls == ["burn_iso"]


class TestTokenBootstrap:
    class _FakeSsh:
        def __init__(self, profile, timeout=None):
            self.profile = profile
            self.connected = False
            self.commands = []

        def connect(self):
            self.connected = True

        def run_command(self, cmd, timeout=None):
            self.commands.append(cmd)
            return "  dragged-token-value \n"

        def close(self):
            self.connected = False

    class _FakeRest:
        def __init__(self, profile, timeout=None):
            self.profile = profile
            self.connected = False

        def connect(self):
            self.connected = True

        def close(self):
            self.connected = False

    def test_missing_token_is_fetched_over_ssh(self, monkeypatch):
        monkeypatch.setattr("client.transport.SshTransport", self._FakeSsh)
        monkeypatch.setattr("client.transport.RestTransport", self._FakeRest)
        profile = Profile(name="t", host="example", rest_enabled=True, rest_token="")
        session = ClientSession(profile)
        session.connect()
        assert profile.rest_token == "dragged-token-value"
        assert session.rest is not None and session.rest.connected

    def test_existing_token_is_not_overwritten(self, monkeypatch):
        monkeypatch.setattr("client.transport.SshTransport", self._FakeSsh)
        monkeypatch.setattr("client.transport.RestTransport", self._FakeRest)
        profile = Profile(name="t", host="example", rest_enabled=True, rest_token="keep")
        session = ClientSession(profile)
        session.connect()
        assert profile.rest_token == "keep"
        assert session.ssh.commands == []
