import json

import pytest

from common.protocol import (
    AgentError,
    DirEntry,
    DriveInfo,
    JobEvent,
    JobSnapshot,
    KNOWN_OPS,
    ProtocolError,
    decode,
    encode,
    make_error,
    make_request,
    make_result,
    parse_request,
    parse_response,
    response_result,
)


class TestEnvelopes:
    def test_request_roundtrip(self):
        req = make_request("list_drives", {"device": "/dev/sr0"})
        parsed = parse_request(encode(req))
        assert parsed["op"] == "list_drives"
        assert parsed["params"] == {"device": "/dev/sr0"}
        assert parsed["id"] == req["id"]

    def test_request_ids_unique(self):
        ids = {make_request("ping")["id"] for _ in range(50)}
        assert len(ids) == 50

    def test_missing_op(self):
        with pytest.raises(ProtocolError):
            parse_request('{"id":"1"}')

    def test_bad_params_type(self):
        with pytest.raises(ProtocolError):
            parse_request('{"id":"1","op":"ping","params":[1,2]}')

    def test_params_default_empty(self):
        assert parse_request('{"id":"1","op":"ping"}')["params"] == {}

    def test_non_object_payload(self):
        with pytest.raises(ProtocolError):
            parse_request("[1,2,3]")
        with pytest.raises(ProtocolError):
            decode("")
        with pytest.raises(ProtocolError):
            decode("{not json")
        with pytest.raises(ProtocolError):
            decode(b"\xff\xfe")

    def test_bytes_input(self):
        assert decode(b'{"a":1}') == {"a": 1}

    def test_make_request_rejects_bad_op(self):
        with pytest.raises(ProtocolError):
            make_request("")
        with pytest.raises(ProtocolError):
            make_request(None)


class TestResponses:
    def test_result_ok(self):
        resp = make_result("abc", {"drives": []})
        assert parse_response(encode(resp))["ok"] is True
        assert response_result(resp) == {"drives": []}

    def test_error_raises_agent_error(self):
        resp = make_error("abc", "invalid_device", "nope")
        with pytest.raises(AgentError) as exc:
            response_result(resp)
        assert exc.value.code == "invalid_device"
        assert exc.value.message == "nope"

    def test_parse_response_requires_ok(self):
        with pytest.raises(ProtocolError):
            parse_response('{"result":1}')

    def test_error_carries_extra(self):
        resp = make_error(None, "validation_error", "bad", field="speed")
        assert resp["error"]["field"] == "speed"
        assert resp["id"] is None


class TestOpsTable:
    def test_op_sets_disjoint(self):
        from common.protocol import JOB_OPS, SIMPLE_OPS

        assert not (SIMPLE_OPS & JOB_OPS)
        assert SIMPLE_OPS | JOB_OPS == KNOWN_OPS

    def test_all_ops_are_strings(self):
        assert all(isinstance(op, str) and op for op in KNOWN_OPS)


class TestModels:
    def test_drive_roundtrip(self):
        drive = DriveInfo(
            device="/dev/sr0",
            model="DVDW",
            writable=True,
            media_present=True,
            label="MOVIE",
            mountpoint="/media/x/DISC",
            mounted=True,
            disc_type="DVD-ROM",
        )
        assert DriveInfo.from_dict(json.loads(json.dumps(drive.to_dict()))) == drive

    def test_drive_defaults(self):
        assert DriveInfo.from_dict({}).device == ""
        assert DriveInfo.from_dict({}).writable is False

    def test_dir_entry_roundtrip(self):
        entry = DirEntry(name="a.txt", path="/a.txt", is_dir=False, size=10, mtime=1.5)
        assert DirEntry.from_dict(entry.to_dict()) == entry

    def test_job_event_roundtrip(self):
        ev = JobEvent(seq=3, ts=1.0, kind="progress", data={"percent": 42.5})
        assert JobEvent.from_dict(ev.to_dict()) == ev

    def test_job_snapshot_roundtrip(self):
        snap = JobSnapshot(
            job_id="j1",
            op="burn_iso",
            state="running",
            progress=0.25,
            message="burning",
            created=1.0,
            updated=2.0,
            result=None,
            error=None,
        )
        assert JobSnapshot.from_dict(snap.to_dict()) == snap
