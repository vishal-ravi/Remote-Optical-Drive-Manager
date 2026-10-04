"""AgentCore: the single dispatch point for every permitted operation.

The SSH frontend and the REST frontend both call :meth:`AgentCore.dispatch`
with a parsed request envelope and receive a response envelope.  Nothing in
here ever passes user text to a shell.
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Callable

from common.protocol import (
    ERR_INTERNAL,
    ERR_UNKNOWN_OP,
    ERR_VALIDATION,
    KNOWN_OPS,
    OP_BROWSE,
    OP_BURN_ISO,
    OP_CLOSE_TRAY,
    OP_CREATE_ISO_FROM_DISC,
    OP_CREATE_ISO_FROM_FILES,
    OP_DRIVE_STATUS,
    OP_EJECT,
    OP_JOB_CANCEL,
    OP_JOB_EVENTS,
    OP_JOB_LIST,
    OP_JOB_STATUS,
    OP_LIST_DRIVES,
    OP_MOUNT,
    OP_PING,
    OP_STAGE_LIST,
    OP_UNMOUNT,
    OP_VERIFY_DISC,
    OP_VERSION,
    AgentError,
    make_error,
    make_result,
)
from common.validation import (
    validate_bool,
    validate_device,
    validate_device_exists,
    validate_job_id,
    validate_limit,
    validate_relative_path,
    validate_speed,
)

from . import ops
from .config import AgentConfig
from .drives import describe_device, list_optical_drives, optical_capacity
from .jobs import Job, JobManager
from .tools import available_tools

log = logging.getLogger("rodm.agent.core")

AGENT_VERSION = "0.1.0"


class AgentCore:
    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()
        self.config.ensure_dirs()
        self.jobs = JobManager()
        self.started = time.time()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            OP_PING: self._op_ping,
            OP_VERSION: self._op_version,
            OP_LIST_DRIVES: self._op_list_drives,
            OP_DRIVE_STATUS: self._op_drive_status,
            OP_MOUNT: self._op_mount,
            OP_UNMOUNT: self._op_unmount,
            OP_BROWSE: self._op_browse,
            OP_EJECT: self._op_eject,
            OP_CLOSE_TRAY: self._op_close_tray,
            OP_CREATE_ISO_FROM_DISC: self._op_create_iso_from_disc,
            OP_CREATE_ISO_FROM_FILES: self._op_create_iso_from_files,
            OP_BURN_ISO: self._op_burn_iso,
            OP_VERIFY_DISC: self._op_verify_disc,
            OP_JOB_STATUS: self._op_job_status,
            OP_JOB_EVENTS: self._op_job_events,
            OP_JOB_CANCEL: self._op_job_cancel,
            OP_JOB_LIST: self._op_job_list,
            OP_STAGE_LIST: self._op_stage_list,
        }

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id") or ""
        op = request.get("op", "")
        params = request.get("params") or {}

        if op not in KNOWN_OPS or op not in self._handlers:
            return make_error(request_id, ERR_UNKNOWN_OP, f"unknown operation '{op}'")
        if not isinstance(params, dict):
            return make_error(request_id, ERR_VALIDATION, "params must be an object")

        started = time.monotonic()
        try:
            result = self._handlers[op](params)
            if isinstance(result, Job):
                snapshot = result.snapshot()
                payload: dict[str, Any] = {
                    "job_id": snapshot.job_id,
                    "job": snapshot.to_dict(),
                }
                result = payload
        except AgentError as exc:
            log.info("op %s rejected: [%s] %s", op, exc.code, exc.message)
            return make_error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - safety net
            log.error("op %s crashed: %s\n%s", op, exc, traceback.format_exc())
            return make_error(
                request_id, ERR_INTERNAL, f"{type(exc).__name__}: {exc}"
            )
        finally:
            elapsed = time.monotonic() - started
            if elapsed > 2.0:
                log.info("op %s took %.2fs", op, elapsed)
        return make_result(request_id, result)

    # ------------------------------------------------------------------ #
    # Simple operations
    # ------------------------------------------------------------------ #

    @staticmethod
    def _device(params: dict[str, Any]) -> str:
        return validate_device(params.get("device"))

    def _op_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "time": time.time(), "uptime": time.time() - self.started}

    def _op_version(self, params: dict[str, Any]) -> dict[str, Any]:
        from common.protocol import PROTOCOL_VERSION

        return {
            "name": "rodm-agent",
            "version": AGENT_VERSION,
            "protocol": PROTOCOL_VERSION,
            "python": _python_version(),
            "tools": available_tools(),
            "data_root": str(self.config.data_root),
        }

    def _op_list_drives(self, params: dict[str, Any]) -> dict[str, Any]:
        drives = list_optical_drives()
        return {"drives": [drive.to_dict() for drive in drives], "count": len(drives)}

    def _op_drive_status(self, params: dict[str, Any]) -> dict[str, Any]:
        device = self._device(params)
        info = describe_device(device)
        payload = info.to_dict()
        payload["capacity_bytes"] = optical_capacity(device)
        payload["mount_root"] = str(self.config.mount_root)
        return payload

    def _op_mount(self, params: dict[str, Any]) -> dict[str, Any]:
        device = self._device(params)
        mountpoint = ops.mount_device(self.config, device)
        return {"device": device, "mountpoint": mountpoint, "mounted": True}

    def _op_unmount(self, params: dict[str, Any]) -> dict[str, Any]:
        device = self._device(params)
        ops.unmount_device(self.config, device)
        return {"device": device, "mounted": False}

    def _op_browse(self, params: dict[str, Any]) -> dict[str, Any]:
        device = self._device(params)
        path = params.get("path", "/")
        if not isinstance(path, str):
            raise AgentError("path must be a string", ERR_VALIDATION)
        mount = validate_bool(params.get("mount"), default=True, field="mount")
        return ops.browse(self.config, device, path, mount=mount)

    def _op_eject(self, params: dict[str, Any]) -> dict[str, Any]:
        device = self._device(params)
        return ops.eject(self.config, device)

    def _op_close_tray(self, params: dict[str, Any]) -> dict[str, Any]:
        device = self._device(params)
        return ops.close_tray(self.config, device)

    def _op_stage_list(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path", "stage")
        if not isinstance(path, str):
            raise AgentError("path must be a string", ERR_VALIDATION)
        relative = validate_relative_path(path) if path not in ("", "/") else "stage"
        return ops.stage_list(self.config, relative or "stage")

    # ------------------------------------------------------------------ #
    # Job-producing operations
    # ------------------------------------------------------------------ #

    def _op_create_iso_from_disc(self, params: dict[str, Any]) -> Job:
        device = validate_device_exists(self._device(params))
        name = params.get("name") or params.get("iso_name")
        overwrite = validate_bool(
            params.get("overwrite"), default=False, field="overwrite"
        )
        if not isinstance(name, str) or not name.strip():
            raise AgentError("name is required", ERR_VALIDATION)

        return self.jobs.submit(
            OP_CREATE_ISO_FROM_DISC,
            lambda ctx: ops.create_iso_from_disc(
                ctx, self.config, device, name, overwrite=overwrite
            ),
            device=device,
        )

    def _op_create_iso_from_files(self, params: dict[str, Any]) -> Job:
        source = params.get("source")
        name = params.get("name") or params.get("iso_name")
        label = params.get("label", "")
        overwrite = validate_bool(
            params.get("overwrite"), default=False, field="overwrite"
        )
        if not isinstance(source, str) or not source.strip():
            raise AgentError("source is required", ERR_VALIDATION)
        if not isinstance(name, str) or not name.strip():
            raise AgentError("name is required", ERR_VALIDATION)
        if label is None:
            label = ""
        if not isinstance(label, str):
            raise AgentError("label must be a string", ERR_VALIDATION)

        return self.jobs.submit(
            OP_CREATE_ISO_FROM_FILES,
            lambda ctx: ops.create_iso_from_files(
                ctx,
                self.config,
                source,
                name,
                label=label,
                overwrite=overwrite,
            ),
        )

    def _op_burn_iso(self, params: dict[str, Any]) -> Job:
        device = validate_device_exists(self._device(params))
        iso = params.get("iso") or params.get("iso_name")
        if not isinstance(iso, str) or not iso.strip():
            raise AgentError("iso is required", ERR_VALIDATION)
        speed = validate_speed(params.get("speed"))
        overburn = validate_bool(params.get("overburn"), default=False, field="overburn")
        eject_after = validate_bool(
            params.get("eject_after"), default=False, field="eject_after"
        )
        simulate = validate_bool(params.get("simulate"), default=False, field="simulate")

        return self.jobs.submit(
            OP_BURN_ISO,
            lambda ctx: ops.burn_iso(
                ctx,
                self.config,
                device,
                iso,
                speed=speed,
                overburn=overburn,
                eject_after=eject_after,
                simulate=simulate,
            ),
            device=device,
        )

    def _op_verify_disc(self, params: dict[str, Any]) -> Job:
        device = validate_device_exists(self._device(params))
        iso = params.get("iso") or params.get("iso_name")
        if not isinstance(iso, str) or not iso.strip():
            raise AgentError("iso is required", ERR_VALIDATION)
        return self.jobs.submit(
            OP_VERIFY_DISC,
            lambda ctx: ops.verify_disc(ctx, self.config, device, iso),
            device=device,
        )

    # ------------------------------------------------------------------ #
    # Job queries
    # ------------------------------------------------------------------ #

    def _op_job_status(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = validate_job_id(params.get("job_id"))
        return self.jobs.snapshot(job_id).to_dict()

    def _op_job_events(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = validate_job_id(params.get("job_id"))
        after = validate_limit(
            params.get("after"), field="after", default=0, minimum=0, maximum=10**9
        )
        snapshot = self.jobs.snapshot(job_id)
        events = self.jobs.events(job_id, after)
        return {
            "job": snapshot.to_dict(),
            "events": [event.to_dict() for event in events],
            "next_seq": events[-1].seq if events else after,
        }

    def _op_job_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = validate_job_id(params.get("job_id"))
        return self.jobs.cancel(job_id).to_dict()

    def _op_job_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"jobs": [job.to_dict() for job in self.jobs.list()]}

    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        log.info("shutting down: cancelling %d job(s)", len(self.jobs.list()))
        self.jobs.cancel_all()


def _python_version() -> str:
    import sys

    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
