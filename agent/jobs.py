"""Background job manager: progress events, cancellation, per-device locking."""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from common.protocol import (
    ERR_CONFLICT,
    ERR_JOB_CANCELLED,
    ERR_JOB_NOT_FOUND,
    ERR_VALIDATION,
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    AgentError,
    JobEvent,
    JobSnapshot,
)

log = logging.getLogger("rodm.agent.jobs")

MAX_EVENTS_PER_JOB = 5000
FINISHED_JOB_TTL = 3600.0  # keep results around for an hour
MAX_FINISHED_JOBS = 50


class JobCancelled(Exception):
    """Raised inside a job body when cancellation was requested."""


class JobContext:
    """Handed to the job body: progress reporting and cancellation checks."""

    def __init__(self, job: "Job") -> None:
        self._job = job

    @property
    def job_id(self) -> str:
        return self._job.job_id

    @property
    def op(self) -> str:
        return self._job.op

    def progress(self, fraction: float | None, message: str = "") -> None:
        self._job.set_progress(fraction, message)

    def log(self, message: str) -> None:
        self._job.emit("log", {"message": message})

    def status(self, **data: Any) -> None:
        self._job.emit("status", data)

    def check_cancelled(self) -> None:
        if self._job.cancel_requested:
            raise JobCancelled()

    @property
    def cancelled(self) -> bool:
        return self._job.cancel_requested


@dataclass
class Job:
    job_id: str
    op: str
    device: str = ""
    state: str = JOB_PENDING
    progress: float = -1.0
    message: str = "queued"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    cancel_requested: bool = False
    #: set by long-running job bodies so cancellation can kill the child process
    terminate: Any = None

    events: deque[JobEvent] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_JOB))
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _done: threading.Event = field(default_factory=threading.Event)

    # ------------------------------------------------------------------ #

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self.events.append(
                JobEvent(seq=self._seq, ts=time.time(), kind=kind, data=data)
            )
            self.updated = time.time()

    def set_progress(self, fraction: float | None, message: str = "") -> None:
        if fraction is not None:
            try:
                self.progress = max(0.0, min(1.0, float(fraction)))
            except (TypeError, ValueError):
                fraction = None
        if message:
            self.message = message
        self.updated = time.time()
        data: dict[str, Any] = {"progress": self.progress}
        if message:
            data["message"] = message
        self.emit("progress", data)

    def events_after(self, seq: int) -> list[JobEvent]:
        with self._lock:
            return [ev for ev in self.events if ev.seq > seq]

    def snapshot(self) -> JobSnapshot:
        with self._lock:
            return JobSnapshot(
                job_id=self.job_id,
                op=self.op,
                state=self.state,
                progress=self.progress,
                message=self.message,
                created=self.created,
                updated=self.updated,
                result=self.result,
                error=self.error,
                error_code=self.error_code,
            )


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._device_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------ #

    def submit(
        self,
        op: str,
        body: Callable[[JobContext], Any],
        *,
        device: str = "",
        job_id: str | None = None,
    ) -> Job:
        job = Job(
            job_id=job_id or uuid.uuid4().hex[:16],
            op=op,
            device=device,
            state=JOB_PENDING,
            message="queued",
        )
        device_lock = self._device_lock_for(device) if device else None
        with self._lock:
            if device and device_lock is not None and device_lock.locked():
                # another job is already working on this drive
                active = self._active_on_device(device)
                if active:
                    raise AgentError(
                        f"{device} is busy with job {active} ({op} rejected)",
                        ERR_CONFLICT,
                    )
            self._jobs[job.job_id] = job
            self._prune_locked()

        thread = threading.Thread(
            target=self._run,
            args=(job, body, device_lock),
            name=f"job-{job.op}-{job.job_id}",
            daemon=True,
        )
        job._thread = thread  # type: ignore[attr-defined]
        thread.start()
        return job

    def _device_lock_for(self, device: str) -> threading.Lock | None:
        if not device:
            return None
        with self._lock:
            return self._device_locks.setdefault(device, threading.Lock())

    def _active_on_device(self, device: str) -> str | None:
        for job in self._jobs.values():
            if job.device == device and job.state in (JOB_PENDING, JOB_RUNNING):
                return job.job_id
        return None

    def _run(
        self, job: Job, body: Callable[[JobContext], Any], device_lock: threading.Lock | None
    ) -> None:
        ctx = JobContext(job)
        acquired = True
        if device_lock is not None:
            acquired = device_lock.acquire(timeout=60)
        try:
            if not acquired:
                raise AgentError(
                    f"timed out waiting for {job.device} to become free", ERR_CONFLICT
                )
            with job._lock:
                if job.cancel_requested:
                    raise JobCancelled()
                job.state = JOB_RUNNING
                job.message = "running"
                job.updated = time.time()
            job.emit("status", {"state": JOB_RUNNING})
            result = body(ctx)
            with job._lock:
                if job.cancel_requested:
                    job.state = JOB_CANCELLED
                    job.message = "cancelled"
                    job.error = "cancelled by client"
                    job.error_code = ERR_JOB_CANCELLED
                else:
                    job.state = JOB_DONE
                    job.progress = 1.0
                    job.message = "done"
                    job.result = result
                job.updated = time.time()
            job.emit(
                "done",
                {
                    "state": job.state,
                    "result": job.result if job.state == JOB_DONE else None,
                },
            )
        except JobCancelled:
            with job._lock:
                job.state = JOB_CANCELLED
                job.message = "cancelled"
                job.error = "cancelled by client"
                job.error_code = ERR_JOB_CANCELLED
                job.updated = time.time()
            job.emit("done", {"state": JOB_CANCELLED})
            log.info("job %s (%s) cancelled", job.job_id, job.op)
        except AgentError as exc:
            with job._lock:
                job.state = JOB_FAILED
                job.error = exc.message
                job.error_code = exc.code
                job.message = "failed"
                job.updated = time.time()
            job.emit("error", {"code": exc.code, "message": exc.message})
            log.warning("job %s (%s) failed: %s", job.job_id, job.op, exc.message)
        except Exception as exc:  # pragma: no cover - safety net
            with job._lock:
                job.state = JOB_FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                job.error_code = "internal_error"
                job.message = "failed"
                job.updated = time.time()
            job.emit("error", {"code": "internal_error", "message": job.error})
            log.error(
                "job %s (%s) crashed: %s\n%s",
                job.job_id,
                job.op,
                exc,
                traceback.format_exc(),
            )
        finally:
            if device_lock is not None and acquired:
                try:
                    device_lock.release()
                except RuntimeError:  # pragma: no cover
                    pass
            job._done.set()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise AgentError(f"unknown job '{job_id}'", ERR_JOB_NOT_FOUND)
        return job

    def snapshot(self, job_id: str) -> JobSnapshot:
        return self.get(job_id).snapshot()

    def events(self, job_id: str, after: int = 0) -> list[JobEvent]:
        return self.get(job_id).events_after(after)

    def list(self) -> list[JobSnapshot]:
        with self._lock:
            jobs = list(self._jobs.values())
        snaps = [job.snapshot() for job in jobs]
        snaps.sort(key=lambda s: s.created, reverse=True)
        return snaps

    def cancel(self, job_id: str) -> JobSnapshot:
        job = self.get(job_id)
        if job.state not in (JOB_PENDING, JOB_RUNNING):
            raise AgentError(
                f"job {job_id} is already {job.state}", ERR_VALIDATION
            )
        job.cancel_requested = True
        if job.terminate is not None:
            try:
                job.terminate()
            except Exception:  # pragma: no cover - best effort
                log.debug("terminate callback failed for %s", job_id, exc_info=True)
        job.emit("status", {"state": "cancelling"})
        return job.snapshot()

    def wait(self, job_id: str, timeout: float | None = None) -> JobSnapshot:
        job = self.get(job_id)
        job._done.wait(timeout)  # type: ignore[attr-defined]
        return job.snapshot()

    def cancel_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.state in (JOB_PENDING, JOB_RUNNING):
                job.cancel_requested = True
                if job.terminate is not None:
                    try:
                        job.terminate()
                    except Exception:  # pragma: no cover
                        pass

    # ------------------------------------------------------------------ #

    def _prune_locked(self) -> None:
        now = time.time()
        finished = [
            job
            for job in self._jobs.values()
            if job.state in (JOB_DONE, JOB_FAILED, JOB_CANCELLED)
        ]
        finished.sort(key=lambda j: j.updated)
        removed = 0
        for job in finished:
            expired = now - job.updated > FINISHED_JOB_TTL
            over_budget = len(finished) - removed > MAX_FINISHED_JOBS
            if expired or over_budget:
                removed += 1
                self._jobs.pop(job.job_id, None)
                if job.device and not any(
                    j.device == job.device for j in self._jobs.values()
                ):
                    self._device_locks.pop(job.device, None)
