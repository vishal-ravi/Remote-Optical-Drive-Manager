"""Wire protocol shared by the agent and the client.

Both transports (SSH-RPC and REST) carry exactly the same JSON envelopes,
so validation, error codes and job semantics live in one place.
"""

from __future__ import annotations

import itertools
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
AGENT_NAME = "rodm-agent"

# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #

OP_PING = "ping"
OP_VERSION = "version"
OP_LIST_DRIVES = "list_drives"
OP_DRIVE_STATUS = "drive_status"
OP_MOUNT = "mount"
OP_UNMOUNT = "unmount"
OP_BROWSE = "browse"
OP_CREATE_ISO_FROM_DISC = "create_iso_from_disc"
OP_CREATE_ISO_FROM_FILES = "create_iso_from_files"
OP_BURN_ISO = "burn_iso"
OP_VERIFY_DISC = "verify_disc"
OP_EJECT = "eject"
OP_CLOSE_TRAY = "close_tray"
OP_JOB_STATUS = "job_status"
OP_JOB_EVENTS = "job_events"
OP_JOB_CANCEL = "job_cancel"
OP_JOB_LIST = "job_list"
OP_STAGE_LIST = "stage_list"

#: Operations answered immediately (no background job).
SIMPLE_OPS = frozenset(
    {
        OP_PING,
        OP_VERSION,
        OP_LIST_DRIVES,
        OP_DRIVE_STATUS,
        OP_MOUNT,
        OP_UNMOUNT,
        OP_BROWSE,
        OP_EJECT,
        OP_CLOSE_TRAY,
        OP_JOB_STATUS,
        OP_JOB_EVENTS,
        OP_JOB_CANCEL,
        OP_JOB_LIST,
        OP_STAGE_LIST,
    }
)

#: Operations that start a background job and return ``{"job_id": ...}``.
JOB_OPS = frozenset(
    {
        OP_CREATE_ISO_FROM_DISC,
        OP_CREATE_ISO_FROM_FILES,
        OP_BURN_ISO,
        OP_VERIFY_DISC,
    }
)

KNOWN_OPS = SIMPLE_OPS | JOB_OPS

# --------------------------------------------------------------------------- #
# Error codes
# --------------------------------------------------------------------------- #

ERR_INVALID_REQUEST = "invalid_request"
ERR_UNKNOWN_OP = "unknown_op"
ERR_VALIDATION = "validation_error"
ERR_INVALID_DEVICE = "invalid_device"
ERR_NO_MEDIA = "no_media"
ERR_NO_FILESYSTEM = "no_filesystem"
ERR_NOT_MOUNTED = "not_mounted"
ERR_TOOL_MISSING = "tool_missing"
ERR_TOOL_FAILED = "tool_failed"
ERR_JOB_NOT_FOUND = "job_not_found"
ERR_JOB_CANCELLED = "job_cancelled"
ERR_UNAUTHORIZED = "unauthorized"
ERR_CONFLICT = "conflict"
ERR_INTERNAL = "internal_error"


class ProtocolError(Exception):
    """Raised when a request or response cannot be encoded/decoded."""

    def __init__(self, message: str, code: str = ERR_INVALID_REQUEST) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AgentError(Exception):
    """Raised by agent operations; carries a stable machine-readable code."""

    def __init__(self, message: str, code: str = ERR_INTERNAL) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Envelopes
# --------------------------------------------------------------------------- #

_counter = itertools.count(1)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def make_request(op: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(op, str) or not op:
        raise ProtocolError("op must be a non-empty string")
    return {
        "v": PROTOCOL_VERSION,
        "id": new_request_id(),
        "op": op,
        "params": params or {},
        "ts": time.time(),
    }


def make_result(request_id: str, result: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}


def make_error(
    request_id: str | None, code: str, message: str, **extra: Any
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    error.update(extra)
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": False, "error": error}


def encode(message: dict[str, Any]) -> str:
    try:
        return json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ProtocolError(f"cannot encode message: {exc}") from exc


def decode(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"payload is not valid utf-8: {exc}") from exc
    raw = raw.strip()
    if not raw:
        raise ProtocolError("empty payload")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"payload is not valid json: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("payload must be a json object")
    return message


def parse_request(raw: str | bytes) -> dict[str, Any]:
    message = decode(raw)
    op = message.get("op")
    if not isinstance(op, str) or not op:
        raise ProtocolError("missing 'op'")
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError("'params' must be an object")
    if not isinstance(message.get("id", ""), str):
        raise ProtocolError("'id' must be a string")
    return {
        "v": message.get("v", PROTOCOL_VERSION),
        "id": message.get("id", ""),
        "op": op,
        "params": params,
    }


def parse_response(raw: str | bytes) -> dict[str, Any]:
    message = decode(raw)
    if "ok" not in message:
        raise ProtocolError("response missing 'ok'")
    return message


def response_result(message: dict[str, Any]) -> Any:
    """Return ``result`` or raise :class:`AgentError` carrying remote details."""
    if message.get("ok"):
        return message.get("result")
    error = message.get("error") or {}
    code = error.get("code", ERR_INTERNAL)
    message_text = error.get("message", "remote error")
    raise AgentError(message_text, code=code)


# --------------------------------------------------------------------------- #
# Job model
# --------------------------------------------------------------------------- #

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_ACTIVE_STATES = (JOB_PENDING, JOB_RUNNING)


@dataclass
class JobEvent:
    """A single progress/log line emitted by a running job."""

    seq: int
    ts: float
    kind: str  # "progress" | "log" | "status" | "done" | "error"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind, "data": self.data}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JobEvent":
        return cls(
            seq=int(raw.get("seq", 0)),
            ts=float(raw.get("ts", 0.0)),
            kind=str(raw.get("kind", "log")),
            data=dict(raw.get("data") or {}),
        )


@dataclass
class JobSnapshot:
    job_id: str
    op: str
    state: str
    progress: float  # 0.0 - 1.0, negative when unknown
    message: str
    created: float
    updated: float
    result: Any = None
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "op": self.op,
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "created": self.created,
            "updated": self.updated,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JobSnapshot":
        return cls(
            job_id=str(raw.get("job_id", "")),
            op=str(raw.get("op", "")),
            state=str(raw.get("state", JOB_PENDING)),
            progress=float(raw.get("progress", -1.0)),
            message=str(raw.get("message", "")),
            created=float(raw.get("created", 0.0)),
            updated=float(raw.get("updated", 0.0)),
            result=raw.get("result"),
            error=raw.get("error"),
            error_code=raw.get("error_code"),
        )


# --------------------------------------------------------------------------- #
# Drive model
# --------------------------------------------------------------------------- #


@dataclass
class DriveInfo:
    device: str
    model: str = ""
    vendor: str = ""
    size: str = ""
    writable: bool = False
    hotpluggable: bool = False
    mountpoint: str | None = None
    mounted: bool = False
    media_present: bool = False
    label: str = ""
    fstype: str = ""
    disc_type: str = ""  # CD-ROM, DVD-ROM, DVD+R, ...
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "model": self.model,
            "vendor": self.vendor,
            "size": self.size,
            "writable": self.writable,
            "hotpluggable": self.hotpluggable,
            "mountpoint": self.mountpoint,
            "mounted": self.mounted,
            "media_present": self.media_present,
            "label": self.label,
            "fstype": self.fstype,
            "disc_type": self.disc_type,
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DriveInfo":
        return cls(
            device=str(raw.get("device", "")),
            model=str(raw.get("model", "")),
            vendor=str(raw.get("vendor", "")),
            size=str(raw.get("size", "")),
            writable=bool(raw.get("writable", False)),
            hotpluggable=bool(raw.get("hotpluggable", False)),
            mountpoint=raw.get("mountpoint"),
            mounted=bool(raw.get("mounted", False)),
            media_present=bool(raw.get("media_present", False)),
            label=str(raw.get("label", "")),
            fstype=str(raw.get("fstype", "")),
            disc_type=str(raw.get("disc_type", "")),
            read_only=bool(raw.get("read_only", True)),
        )


@dataclass
class DirEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "mtime": self.mtime,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DirEntry":
        return cls(
            name=str(raw.get("name", "")),
            path=str(raw.get("path", "")),
            is_dir=bool(raw.get("is_dir", False)),
            size=int(raw.get("size", 0)),
            mtime=float(raw.get("mtime", 0.0)),
        )
