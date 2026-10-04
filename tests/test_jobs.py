import threading
import time

import pytest

from common.protocol import AgentError
from agent.jobs import JobManager


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def manager():
    return JobManager()


class TestLifecycle:
    def test_success(self, manager):
        job = manager.submit("ping", lambda ctx: {"value": 42})
        assert wait_until(lambda: manager.snapshot(job.job_id).state == "done")
        snap = manager.snapshot(job.job_id)
        assert snap.progress == 1.0
        assert snap.result == {"value": 42}
        assert snap.error is None

    def test_failure_with_agent_error(self, manager):
        def body(ctx):
            raise AgentError("boom", "tool_failed")

        job = manager.submit("ping", body)
        assert wait_until(lambda: manager.snapshot(job.job_id).state == "failed")
        snap = manager.snapshot(job.job_id)
        assert snap.error == "boom"
        assert snap.error_code == "tool_failed"

    def test_unexpected_exception(self, manager):
        def body(ctx):
            raise RuntimeError("kaboom")

        job = manager.submit("ping", body)
        assert wait_until(lambda: manager.snapshot(job.job_id).state == "failed")
        assert "kaboom" in manager.snapshot(job.job_id).error

    def test_progress_and_events(self, manager):
        def body(ctx):
            ctx.progress(0.5, "halfway")
            ctx.log("hello")
            ctx.check_cancelled()
            return {"ok": True}

        job = manager.submit("ping", body)
        assert wait_until(lambda: manager.snapshot(job.job_id).state == "done")
        events = manager.events(job.job_id, 0)
        kinds = [ev.kind for ev in events]
        assert "progress" in kinds
        assert "log" in kinds
        assert "done" in kinds
        progress_events = [ev for ev in events if ev.kind == "progress"]
        assert any(ev.data.get("progress") == 0.5 for ev in progress_events)

        last = events[-1].seq
        assert manager.events(job.job_id, last) == []
        assert len(manager.events(job.job_id, 0)) > 0

    def test_cancel_running_job(self, manager):
        started = threading.Event()
        release = threading.Event()

        def body(ctx):
            started.set()
            while not release.is_set():
                ctx.check_cancelled()
                time.sleep(0.02)
            return {"finished": True}

        job = manager.submit("ping", body)
        assert started.wait(5)
        manager.cancel(job.job_id)
        assert wait_until(
            lambda: manager.snapshot(job.job_id).state in ("cancelled", "failed")
        )
        release.set()
        snap = manager.snapshot(job.job_id)
        assert snap.state == "cancelled"
        assert snap.error_code == "job_cancelled"

    def test_cancel_finished_job_rejected(self, manager):
        job = manager.submit("ping", lambda ctx: 1)
        assert wait_until(lambda: manager.snapshot(job.job_id).state == "done")
        with pytest.raises(AgentError) as exc:
            manager.cancel(job.job_id)
        assert exc.value.code == "validation_error"

    def test_same_device_is_exclusive(self, manager):
        gate = threading.Event()

        def slow(ctx):
            gate.wait(3)
            return "ran"

        first = manager.submit("slow", slow, device="/dev/sr0")
        with pytest.raises(AgentError) as exc:
            manager.submit("slow", slow, device="/dev/sr0")
        assert exc.value.code == "conflict"

        manager.cancel(first.job_id)
        gate.set()
        assert wait_until(
            lambda: manager.snapshot(first.job_id).state in ("cancelled", "done")
        )
        # after the first job finished the drive is free again
        second = manager.submit("slow", slow, device="/dev/sr0")
        gate.set()
        assert wait_until(lambda: manager.snapshot(second.job_id).state == "done")

    def test_device_lock_released(self, manager):
        job = manager.submit("ping", lambda ctx: 1, device="/dev/sr0")
        assert wait_until(lambda: manager.snapshot(job.job_id).state == "done")
        job2 = manager.submit("ping", lambda ctx: 2, device="/dev/sr0")
        assert wait_until(lambda: manager.snapshot(job2.job_id).state == "done")
        assert manager.snapshot(job2.job_id).result == 2

    def test_job_list_sorted(self, manager):
        jobs = [manager.submit("ping", lambda ctx: i) for i in range(3)]
        for job in jobs:
            wait_until(lambda j=job: manager.snapshot(j.job_id).state == "done")
        listing = manager.list()
        assert len(listing) == 3
        assert listing[0].created >= listing[-1].created

    def test_unknown_job(self, manager):
        with pytest.raises(AgentError) as exc:
            manager.snapshot("missing")
        assert exc.value.code == "job_not_found"

    def test_events_unknown_job(self, manager):
        with pytest.raises(AgentError) as exc:
            manager.events("nope", 0)
        assert exc.value.code == "job_not_found"
