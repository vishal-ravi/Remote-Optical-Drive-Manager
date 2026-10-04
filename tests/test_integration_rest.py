import json
"""End-to-end test: real agent HTTP server + real REST transport (loopback)."""

import os
import stat
import threading

import pytest

from agent import http_api
from agent.config import AgentConfig
from agent.core import AgentCore
from client.profiles import Profile, ProfileStore
from client.transport import ClientSession

TOKEN = "integration-test-token"


@pytest.fixture()
def agent(tmp_path):
    cfg = AgentConfig(
        data_root=tmp_path / "data",
        state_dir=tmp_path / "state",
        host="127.0.0.1",
        port=0,
        require_tls=False,
        token=TOKEN,
    )
    core = AgentCore(cfg)
    server = http_api.build_server(core, cfg)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield cfg, core, port
    finally:
        server.shutdown()
        server.server_close()
        core.shutdown()


@pytest.fixture()
def session(agent, tmp_path):
    cfg, core, port = agent
    profile = Profile(
        name="local",
        host="127.0.0.1",
        user=os.environ.get("USER", "tester"),
        rest_port=port,
        rest_scheme="http",
        rest_token=TOKEN,
        remote_data_root=str(cfg.data_root),
    )
    sess = ClientSession(profile, timeout=10)
    sess.connect(use_ssh=False)
    yield sess, cfg, core, profile
    sess.close()


class TestRestSession:
    def test_connect_and_ping(self, session):
        sess, cfg, core, profile = session
        assert sess.connected
        assert sess.active == "rest"
        result = sess.call("ping")
        assert result["pong"] is True

    def test_version_reports_tools(self, session):
        sess, cfg, core, profile = session
        version = sess.call("version")
        assert version["name"] == "rodm-agent"
        assert version["tools"]["wodim"]

    def test_list_drives(self, session):
        sess, cfg, core, profile = session
        result = sess.call("list_drives")
        assert "drives" in result

    @pytest.mark.parametrize(
        "op,params",
        [
            ("drive_status", {"device": "/dev/sda"}),
            ("eject", {"device": "/dev/nvme0n1"}),
            ("mount", {"device": "/dev/mmcblk0"}),
            ("burn_iso", {"device": "/dev/sda", "iso": "x.iso"}),
        ],
    )
    def test_system_disks_rejected_over_rest(self, session, op, params):
        sess, cfg, core, profile = session
        from common.protocol import AgentError

        with pytest.raises(AgentError) as exc:
            sess.call(op, params)
        assert exc.value.code == "invalid_device"

    def test_unknown_op_over_rest(self, session):
        sess, cfg, core, profile = session
        from common.protocol import AgentError

        with pytest.raises(AgentError) as exc:
            sess.call("definitely_not_an_op")
        assert exc.value.code == "unknown_op"


class TestIsoWorkflow:
    def test_upload_create_iso_and_watch_job(self, session, tmp_path):
        sess, cfg, core, profile = session
        payload_dir = tmp_path / "payload"
        payload_dir.mkdir()
        (payload_dir / "readme.txt").write_text("hello from the client\n" * 50)
        (payload_dir / "data.bin").write_bytes(os.urandom(64 * 1024))

        # stage one file at a time (upload endpoint is file based)
        for file in sorted(payload_dir.iterdir()):
            remote = sess.upload(file, f"stage/batch1/{file.name}")
            assert remote.endswith(file.name)

        staged = sess.call("stage_list", {"path": "stage/batch1"})
        names = {entry["name"] for entry in staged["entries"]}
        assert names == {"readme.txt", "data.bin"}

        job = sess.call(
            "create_iso_from_files",
            {"source": "stage/batch1", "name": "test-image", "label": "TESTVOL"},
        )
        job_id = job["job_id"]
        assert job_id

        states = []
        final = None
        for update in sess.iter_job_updates(job_id):
            if update["type"] == "status":
                states.append(update["job"]["state"])
                final = update["job"]
            assert update["type"] in ("event", "status")

        assert final is not None
        assert final["state"] == "done", final
        assert final["progress"] == 1.0
        result = final["result"]
        assert result["name"] == "test-image.iso"

        iso_path = cfg.image_dir / "test-image.iso"
        assert iso_path.exists()
        assert iso_path.stat().st_size > 0

        # image list endpoint

        listing = sess.call("stage_list", {"path": "images"})
        assert any(entry["name"] == "test-image.iso" for entry in listing["entries"])

        # the created file must be a real ISO9660 image
        with iso_path.open("rb") as handle:
            handle.seek(0x8001)
            assert handle.read(5) == b"CD001"

    def test_job_failure_is_reported(self, session, tmp_path):
        sess, cfg, core, profile = session
        job = sess.call(
            "create_iso_from_files",
            {"source": "stage/does-not-exist", "name": "nope"},
        )
        final = None
        for update in sess.iter_job_updates(job["job_id"]):
            if update["type"] == "status":
                final = update["job"]
        assert final["state"] == "failed"
        assert "does not exist" in final["error"]

    def test_cancel_unknown_job(self, session):
        sess, cfg, core, profile = session
        from common.protocol import AgentError

        with pytest.raises(AgentError) as exc:
            sess.cancel_job("missing")
        assert exc.value.code == "job_not_found"


class TestProfileStore:
    def test_roundtrip_and_permissions(self, tmp_path):
        store = ProfileStore(tmp_path / "profiles.json")
        profile = Profile(
            name="kali",
            host="192.168.1.50",
            user="spider",
            rest_token="secret-token",
            key_path="~/.ssh/id_ed25519",
        )
        store.save([profile])
        mode = stat.S_IMODE(store.path.stat().st_mode)
        assert mode == 0o600

        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].host == "192.168.1.50"
        assert loaded[0].rest_token == "secret-token"
        assert loaded[0].redacted()["rest_token"] == "***"

    def test_upsert_update_remove(self, tmp_path):
        store = ProfileStore(tmp_path / "profiles.json")
        first = Profile(name="a", host="10.0.0.1", user="u1")
        store.upsert(first)
        store.upsert(Profile(name="b", host="10.0.0.2", user="u2"))
        assert len(store.load()) == 2

        store.update_fields(first.id, port=2222, last_device="/dev/sr0")
        assert store.get(first.id).port == 2222
        assert store.get(first.id).last_device == "/dev/sr0"

        assert store.remove(first.id) is True
        assert store.remove(first.id) is False
        assert len(store.load()) == 1

    def test_get_by_name_or_id(self, tmp_path):
        store = ProfileStore(tmp_path / "profiles.json")
        profile = Profile(name="named", host="10.0.0.3")
        store.upsert(profile)
        assert store.get("named").host == "10.0.0.3"
        assert store.get(profile.id).name == "named"
        with pytest.raises(KeyError):
            store.get("ghost")

    def test_invalid_profiles_are_skipped(self, tmp_path):
        path = tmp_path / "profiles.json"
        path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {"name": "ok", "host": "1.2.3.4"},
                        {"name": "no-host"},
                        "not-a-dict",
                    ]
                }
            )
        )
        store = ProfileStore(path)
        assert [p.name for p in store.load()] == ["ok"]
