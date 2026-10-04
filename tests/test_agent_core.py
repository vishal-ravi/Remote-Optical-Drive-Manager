import json
import subprocess

import pytest

from common.protocol import make_request
from agent.config import AgentConfig
from agent.core import AgentCore


@pytest.fixture()
def core(tmp_path):
    cfg = AgentConfig(
        data_root=tmp_path / "data",
        state_dir=tmp_path / "state",
        host="127.0.0.1",
        port=0,
        require_tls=False,
        token="test-token",
    )
    return AgentCore(cfg)


def call(core, op, params=None, request_id="t1"):
    request = make_request(op, params)
    request["id"] = request_id
    response = core.dispatch(request)
    return response


def ok(core, op, params=None):
    response = call(core, op, params)
    assert response["ok"] is True, response
    return response["result"]


def err(core, op, params=None):
    response = call(core, op, params)
    assert response["ok"] is False, response
    return response["error"]


class TestDispatch:
    def test_ping(self, core):
        result = ok(core, "ping")
        assert result["pong"] is True

    def test_version(self, core):
        result = ok(core, "version")
        assert result["name"] == "rodm-agent"
        assert result["protocol"] == 1
        assert "tools" in result

    def test_unknown_op(self, core):
        assert err(core, "rm_rf")["code"] == "unknown_op"

    def test_malformed_params(self, core):
        response = core.dispatch({"id": "x", "op": "ping", "params": [1]})
        assert response["ok"] is False
        assert response["error"]["code"] == "validation_error"

    def test_error_id_echoed(self, core):
        response = call(core, "ping", request_id="abc123")
        assert response["id"] == "abc123"

    @pytest.mark.parametrize("op", ["drive_status", "mount", "unmount", "eject", "close_tray"])
    def test_device_ops_reject_system_disks(self, core, op):
        for bad in ("/dev/sda", "/dev/nvme0n1", "/dev/sr0/../sda"):
            error = err(core, op, {"device": bad})
            assert error["code"] == "invalid_device", (op, bad, error)

    @pytest.mark.parametrize("op", ["drive_status", "mount", "unmount", "eject"])
    def test_device_ops_require_device(self, core, op):
        assert err(core, op, {})["code"] == "invalid_device"
        assert err(core, op, {"device": None})["code"] == "invalid_device"

    def test_unknown_device_node(self, core):
        error = err(core, "drive_status", {"device": "/dev/sr7"})
        assert error["code"] == "invalid_device"

    def test_job_ops_require_names(self, core):
        # /dev/sr0 does not exist on this host, so device validation wins
        assert err(core, "burn_iso", {"device": "/dev/sr0"})["code"] == "invalid_device"
        assert err(core, "burn_iso", {"device": "/dev/sda", "iso": "x.iso"})["code"] == (
            "invalid_device"
        )
        assert err(core, "burn_iso", {"iso": "x.iso"})["code"] == "invalid_device"
        assert err(core, "create_iso_from_disc", {"device": "/dev/sr0"})["code"] in (
            "validation_error",
            "invalid_device",
        )
        assert err(core, "create_iso_from_files", {})["code"] == "validation_error"
        assert err(core, "verify_disc", {"device": "/dev/sr0", "iso": "x.iso"})[
            "code"
        ] == "invalid_device"


class TestBrowsingGuards:
    def test_browse_rejects_traversal(self, core):
        error = err(core, "browse", {"device": "/dev/sr0", "path": "../../etc"})
        assert error["code"] in ("validation_error", "invalid_device")

    def test_stage_list_outside_root(self, core):
        error = err(core, "stage_list", {"path": "../../etc"})
        assert error["code"] == "validation_error"


class TestJobQueries:
    def test_job_list_empty(self, core):
        assert ok(core, "job_list")["jobs"] == []

    def test_unknown_job(self, core):
        assert err(core, "job_status", {"job_id": "nope"})["code"] == "job_not_found"
        assert err(core, "job_events", {"job_id": "../../x"})["code"] == "validation_error"

    def test_job_cancel_unknown(self, core):
        assert err(core, "job_cancel", {"job_id": "nope"})["code"] == "job_not_found"


class TestFakeLsblk:
    def test_list_drives_parses_lsblk(self, core, tmp_path, monkeypatch):
        payload = {
            "blockdevices": [
                {
                    "name": "sr0",
                    "path": "/dev/sr0",
                    "type": "rom",
                    "size": "4.2G",
                    "model": "DVDW",
                    "vendor": "HL-DT-ST",
                    "tran": "sata",
                    "rm": False,
                },
                {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk"},
            ]
        }
        shim = tmp_path / "lsblk"
        shim.write_text(
            "#!/bin/sh\ncat <<'EOF'\n" + json.dumps(payload) + "\nEOF\n"
        )
        shim.chmod(0o755)

        sr0 = tmp_path / "sr0"
        sr0.write_bytes(b"\0" * 4096)
        monkeypatch.setenv("PATH", f"{tmp_path}:{subprocess.os.environ['PATH']}")

        import agent.drives as drives
        import agent.tools as tools

        tools.which.cache_clear()
        monkeypatch.setattr(drives, "tools", tools)
        monkeypatch.setattr(drives.os.path, "exists", lambda p: True)
        monkeypatch.setattr(drives, "_udev_properties", lambda d: {"ID_CDROM": "1"})
        monkeypatch.setattr(drives, "_blkid", lambda d: {"LABEL": "MOVIE", "TYPE": "udf"})
        monkeypatch.setattr(drives, "_mounted_map", lambda: {})
        monkeypatch.setattr(drives, "_media_present", lambda *a: True)

        try:
            result = ok(core, "list_drives")
        finally:
            tools.which.cache_clear()

        devices = [d["device"] for d in result["drives"]]
        assert "/dev/sr0" in devices
        assert "/dev/nvme0n1" not in devices
        assert result["count"] == len(result["drives"])
