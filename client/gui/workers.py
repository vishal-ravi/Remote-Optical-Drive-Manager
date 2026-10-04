"""Qt background tasks: every blocking session call runs off the UI thread."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from common.protocol import AgentError

log = logging.getLogger("rodm.client.workers")


class TaskHost:
    """Mixin for widgets that start background QThreads.

    PySide6 deletes the C++ QThread when the Python wrapper is garbage
    collected, and Qt aborts the process if that happens while the thread is
    still running.  A bare local ``task`` variable therefore crashes the app
    the moment the creating function returns.  Hosts keep a strong reference
    to every task they start, parent the task to themselves, and wait for the
    still-running ones before they are allowed to close.
    """

    def start_task(self, task: QThread) -> QThread:
        task.setParent(self)  # type: ignore[arg-type]
        tasks: list[QThread] = self.__dict__.setdefault("_bg_tasks", [])
        # finished tasks can be released safely; never touch running ones
        self.__dict__["_bg_tasks"] = [t for t in tasks if t.isRunning()]
        self.__dict__["_bg_tasks"].append(task)
        task.start()
        return task

    def stop_tasks(self, timeout_ms: int = 3000) -> bool:
        """Ask every tracked task to stop and wait for them.

        Returns True when no tracked task is running any more, i.e. when it
        is safe to destroy the host widget.
        """
        tasks: list[QThread] = self.__dict__.get("_bg_tasks", [])
        for task in tasks:
            stop = getattr(task, "stop", None)
            if callable(stop):
                stop()
        still_running: list[QThread] = []
        for task in tasks:
            if task.isRunning() and not task.wait(timeout_ms):
                still_running.append(task)
        self.__dict__["_bg_tasks"] = still_running
        return not still_running

    @property
    def tasks_busy(self) -> bool:
        return any(
            task.isRunning() for task in self.__dict__.get("_bg_tasks", [])
        )


class FnTask(QThread):
    """Run one callable in the background and report back through signals."""

    done = Signal(object)  # arbitrary result
    failed = Signal(str, str)  # (code, message)

    def __init__(self, fn: Callable[[], Any], parent: Any = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D102
        try:
            result = self._fn()
        except AgentError as exc:
            self.failed.emit(exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("background task crashed")
            self.failed.emit("internal_error", f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class JobTask(QThread):
    """Watch a remote job until it reaches a terminal state."""

    updated = Signal(dict)  # {"type": "event"|"status", ...}
    failed = Signal(str, str)
    finished_state = Signal(dict)  # final job snapshot dict

    def __init__(self, session: Any, job_id: str, parent: Any = None) -> None:
        super().__init__(parent)
        self.session = session
        self.job_id = job_id
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # noqa: D102
        last: dict | None = None
        try:
            for update in self.session.iter_job_updates(self.job_id, stop=self._stop):
                if self._stop.is_set():
                    return
                self.updated.emit(update)
                if update.get("type") == "status":
                    last = update.get("job") or last
        except AgentError as exc:
            self.failed.emit(exc.code, exc.message)
            return
        except Exception as exc:  # pragma: no cover
            self.failed.emit("internal_error", f"{type(exc).__name__}: {exc}")
            return
        if last and not self._stop.is_set():
            self.finished_state.emit(last)


class TransferTask(QThread):
    """Download or upload a single file with byte-level progress."""

    progressed = Signal(int, int)  # done, total
    done = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        kind: str,
        session: Any,
        *,
        local_path: str | Path | None = None,
        remote_path: str | None = None,
        device: str | None = None,
        media_path: str | None = None,
        relative_path: str | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind  # "download" | "upload"
        self.session = session
        self.local_path = local_path
        self.remote_path = remote_path
        self.device = device
        self.media_path = media_path
        self.relative_path = relative_path
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _progress(self, done: int, total: int) -> None:
        if not self._stop.is_set():
            self.progressed.emit(int(done), int(total))

    def run(self) -> None:  # noqa: D102
        try:
            if self.kind == "download":
                size = self.session.download(
                    self.local_path,
                    remote_path=self.remote_path,
                    device=self.device,
                    media_path=self.media_path,
                    progress=self._progress,
                )
                result: Any = {"path": str(self.local_path), "size": size}
            else:
                remote = self.session.upload(
                    self.local_path, self.relative_path or "", progress=self._progress
                )
                result = {"remote": remote, "local": str(self.local_path)}
        except AgentError as exc:
            self.failed.emit(exc.code, exc.message)
            return
        except Exception as exc:  # pragma: no cover
            log.exception("transfer failed")
            self.failed.emit("internal_error", f"{type(exc).__name__}: {exc}")
            return
        self.done.emit(result)


class QueueTask(QThread):
    """Sequential burn queue: run jobs one after another on one drive.

    ``runner`` is called for each item and must return a job id; the task
    then watches it, so queue progression is strictly serial.
    """

    item_started = Signal(dict)
    item_progress = Signal(dict)  # {"index", "job"}
    item_finished = Signal(dict)  # {"item", "job"}
    item_failed = Signal(dict)
    waiting_for_media = Signal(dict)
    queue_done = Signal(list)
    failed = Signal(str, str)

    def __init__(
        self,
        session: Any,
        items: list[dict[str, Any]],
        runner: Callable[[dict[str, Any]], str],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.items = list(items)
        self.runner = runner
        self._stop = threading.Event()
        self._gate = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._gate.set()  # release any "waiting for media" block

    def proceed(self) -> None:
        """Called by the GUI when the operator confirms the next disc is in."""
        self._gate.set()

    def _wait_for_media(self, payload: dict[str, Any], device: str, timeout: float = 900.0) -> bool:
        """Return True when media is present (waiting first if necessary)."""
        if not device:
            return True
        try:
            status = self.session.call("drive_status", {"device": device})
        except AgentError:
            status = {}
        if status.get("media_present"):
            return True
        self._gate.clear()
        self.waiting_for_media.emit(payload)
        if not self._gate.wait(timeout):
            return False
        try:
            status = self.session.call("drive_status", {"device": device})
        except AgentError:
            status = {}
        return bool(status.get("media_present"))

    def run(self) -> None:  # noqa: D102
        results: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(self.items):
                if self._stop.is_set():
                    break
                device = str(item.get("device") or "")
                if item.get("check_media", True) and device:
                    payload = {"index": index, "item": item, "reason": "insert_disc"}
                    if not self._wait_for_media(payload, device):
                        results.append(
                            {
                                "index": index,
                                "item": item,
                                "error": "timed out waiting for disc",
                            }
                        )
                        self.item_failed.emit(results[-1])
                        break
                    if self._stop.is_set():
                        break

                self.item_started.emit({"index": index, "item": item})
                job_id = self.runner(item)
                final: dict | None = None
                for update in self.session.iter_job_updates(job_id, stop=self._stop):
                    if self._stop.is_set():
                        break
                    if update.get("type") == "status":
                        job = update.get("job") or {}
                        final = job
                        self.item_progress.emit(
                            {"index": index, "job": job, "item": item}
                        )
                        if job.get("state") in ("done", "failed", "cancelled"):
                            break
                if self._stop.is_set():
                    break
                final = final or self.session.job_snapshot(job_id).to_dict()
                record = {"index": index, "item": item, "job": final}
                if final.get("state") == "done":
                    results.append(record)
                    self.item_finished.emit(record)
                else:
                    record["error"] = final.get("error") or final.get("state")
                    self.item_failed.emit(record)
                    results.append(record)
                    break
        except AgentError as exc:
            self.failed.emit(exc.code, exc.message)
            return
        except Exception as exc:  # pragma: no cover
            self.failed.emit("internal_error", f"{type(exc).__name__}: {exc}")
            return
        self.queue_done.emit(results)
