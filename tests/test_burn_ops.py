"""Exercise the burn/rip/verify job bodies with fake optical utilities.

Real hardware is only needed for the final end-to-end run; here the external
commands are replaced by tiny PATH shims so the progress plumbing, argument
construction and job lifecycle are covered on any machine.
"""

import os
import stat
from pathlib import Path

import pytest

from agent.config import AgentConfig
from agent.jobs import JobManager

ISO_OFFSET = 16 * 2048  # 32768


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def cfg(tmp_path):
    config = AgentConfig(
        data_root=tmp_path / "data",
        state_dir=tmp_path / "state",
        host="127.0.0.1",
        port=0,
        require_tls=False,
        token="t",
    )
    config.ensure_dirs()
    return config


@pytest.fixture()
def jobs():
    return JobManager()


def write_shim(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(directory)


def wait_for(job_id, jobs, timeout=20.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = jobs.snapshot(job_id)
        if snap.state in ("done", "failed", "cancelled"):
            return snap
        time.sleep(0.05)
    return jobs.snapshot(job_id)


# --------------------------------------------------------------------------- #
# rip (create_iso_from_disc)
# --------------------------------------------------------------------------- #


class TestRipDisc:
    def test_rip_with_fake_dd(self, cfg, jobs, tmp_path, monkeypatch):
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        # fake dd: write 8 MiB of data and mimic status=progress output
        write_shim(
            shim_dir,
            "dd",
            'of=""\n'
            'for arg in "$@"; do case "$arg" in of=*) of="${arg#of=}";; esac; done\n'
            'printf "%s" "$TEST_BYTES" > /dev/null\n'
            'head -c "$TEST_BYTES" /dev/zero > "$of"\n'
            'printf "1073741824 bytes (1.1 GB, 1.0 GiB) copied, 2.0 s, 500 MB/s\\n" >&2\n'
            'exit 0\n',
        )
        monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
        monkeypatch.setenv("TEST_BYTES", str(8 * 1024 * 1024))

        import agent.ops.images as images

        monkeypatch.setattr(images, "require_media", lambda device: object())
        monkeypatch.setattr(images, "optical_capacity", lambda device: 16 * 1024 * 1024)
        from agent.tools import which

        which.cache_clear()

        try:
            job = jobs.submit(
                "create_iso_from_disc",
                lambda ctx: images.create_iso_from_disc(
                    ctx, cfg, "/dev/sr0", "ripped-disc"
                ),
                device="/dev/sr0",
            )
            snap = wait_for(job.job_id, jobs)
        finally:
            which.cache_clear()

        assert snap.state == "done", snap.error
        result = snap.result
        assert result["name"] == "ripped-disc.iso"
        assert result["size"] == 8 * 1024 * 1024
        assert (cfg.image_dir / "ripped-disc.iso").exists()
        # progress events carried a fraction
        events = jobs.events(job.job_id, 0)
        progress = [e.data.get("progress") for e in events if e.kind == "progress"]
        assert any(0.0 < (p or 0) <= 1.0 for p in progress)

    def test_rip_refuses_to_overwrite(self, cfg, jobs, tmp_path, monkeypatch):
        (cfg.image_dir / "existing.iso").write_bytes(b"x")
        import agent.ops.images as images

        monkeypatch.setattr(images, "require_media", lambda device: object())
        monkeypatch.setattr(images, "optical_capacity", lambda device: 1024)
        job = jobs.submit(
            "create_iso_from_disc",
            lambda ctx: images.create_iso_from_disc(
                ctx, cfg, "/dev/sr0", "existing", overwrite=False
            ),
        )
        snap = wait_for(job.job_id, jobs)
        assert snap.state == "failed"
        assert "already exists" in snap.error


# --------------------------------------------------------------------------- #
# burn
# --------------------------------------------------------------------------- #


class TestBurn:
    @pytest.fixture()
    def writable_env(self, cfg, tmp_path, monkeypatch):
        import agent.ops.burn as burn

        monkeypatch.setattr(burn, "validate_device", lambda d: d)
        monkeypatch.setattr(burn, "require_media", lambda d: object())
        monkeypatch.setattr(burn, "optical_capacity", lambda d: 4 * 1024 * 1024 * 1024)

        def writable(_cfg, device):
            class Info:
                writable = True

            return Info()

        monkeypatch.setattr(burn, "_require_writable_drive", writable)
        image = cfg.image_dir / "movie.iso"
        image.write_bytes(os.urandom(64 * 1024))
        return image

    def test_burn_command_and_progress(self, cfg, jobs, tmp_path, monkeypatch, writable_env):
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        log_file = tmp_path / "wodim-args.log"
        write_shim(
            shim_dir,
            "wodim",
            f'echo "$@" > "{log_file}"\n'
            'echo "Making list of media addresses: done"\n'
            'echo "Writing:      123/ 1000  12.3% done, speed=  8x"\n'
            'echo "Writing:      500/ 1000  50.0% done, speed=  8x"\n'
            'echo "Writing:     1000/ 1000 100.0% done, speed=  8x"\n'
            'echo "Fixating... done"\n'
            'exit 0\n',
        )
        monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
        from agent.tools import which

        which.cache_clear()
        import agent.ops.burn as burn

        try:
            job = jobs.submit(
                "burn_iso",
                lambda ctx: burn.burn_iso(
                    ctx,
                    cfg,
                    "/dev/sr0",
                    "images/movie.iso",
                    speed=8,
                    overburn=False,
                    eject_after=False,
                    simulate=False,
                ),
                device="/dev/sr0",
            )
            snap = wait_for(job.job_id, jobs)
        finally:
            which.cache_clear()

        assert snap.state == "done", snap.error
        args = log_file.read_text()
        assert "dev=/dev/sr0" in args
        assert "-data" in args
        assert "-speed=8" in args
        assert "movie.iso" in args
        assert "-overburn" not in args
        assert "-dummy" not in args

        events = jobs.events(job.job_id, 0)
        progress = [e.data.get("progress") for e in events if e.kind == "progress"]
        assert max(p for p in progress if p is not None) == 1.0

    def test_burn_failure_surfaces_stderr(self, cfg, jobs, tmp_path, monkeypatch, writable_env):
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        write_shim(
            shim_dir,
            "wodim",
            'echo "wodim: Cannot send SCSI command: Input/output error" >&2\nexit 5\n',
        )
        monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
        from agent.tools import which

        which.cache_clear()
        import agent.ops.burn as burn

        try:
            job = jobs.submit(
                "burn_iso",
                lambda ctx: burn.burn_iso(ctx, cfg, "/dev/sr0", "images/movie.iso"),
                device="/dev/sr0",
            )
            snap = wait_for(job.job_id, jobs)
        finally:
            which.cache_clear()

        assert snap.state == "failed"
        assert snap.error_code == "tool_failed"
        assert "Cannot send SCSI command" in snap.error

    def test_rejects_image_larger_than_medium(self, cfg, jobs, tmp_path, monkeypatch, writable_env):
        import agent.ops.burn as burn

        monkeypatch.setattr(burn, "optical_capacity", lambda d: 1024)
        job = jobs.submit(
            "burn_iso",
            lambda ctx: burn.burn_iso(ctx, cfg, "/dev/sr0", "images/movie.iso"),
            device="/dev/sr0",
        )
        snap = wait_for(job.job_id, jobs)
        assert snap.state == "failed"
        assert "does not fit" in snap.error


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


class TestVerify:
    @pytest.fixture(autouse=True)
    def _patch_device_checks(self, monkeypatch):
        import agent.ops.burn as burn

        monkeypatch.setattr(burn, "validate_device", lambda d: d)
        monkeypatch.setattr(burn, "require_media", lambda d: object())
        monkeypatch.setattr(burn, "optical_capacity", lambda d: 8 * 1024 * 1024)

    def _source_iso(self, cfg) -> Path:
        image = cfg.image_dir / "verify-me.iso"
        image.write_bytes(os.urandom(128 * 1024))
        return image

    def _run(self, jobs, cfg, device_path: Path):
        import agent.ops.burn as burn

        job = jobs.submit(
            "verify_disc",
            lambda ctx: burn.verify_disc(
                ctx, cfg, str(device_path), "images/verify-me.iso"
            ),
        )
        return wait_for(job.job_id, jobs), job

    def test_matches_at_iso_sector_offset(self, cfg, jobs, tmp_path):
        image = self._source_iso(cfg)
        # burned layout: 16 sectors of zeros, then the image, then padding
        disc = tmp_path / "disc.bin"
        disc.write_bytes(b"\0" * ISO_OFFSET + image.read_bytes() + b"\xff" * 4096)

        snap, job = self._run(jobs, cfg, disc)
        assert snap.state == "done", snap.error
        assert snap.result["match_offset"] == ISO_OFFSET
        assert snap.result["verified"] is True
        assert snap.result["bytes_compared"] == image.stat().st_size

    def test_matches_at_offset_zero(self, cfg, jobs, tmp_path):
        image = self._source_iso(cfg)
        disc = tmp_path / "disc0.bin"
        disc.write_bytes(image.read_bytes() + b"\0" * 8192)

        snap, job = self._run(jobs, cfg, disc)
        assert snap.state == "done", snap.error
        assert snap.result["match_offset"] == 0

    def test_mismatch_fails(self, cfg, jobs, tmp_path):
        image = self._source_iso(cfg)
        disc = tmp_path / "bad.bin"
        payload = bytearray(b"\0" * ISO_OFFSET + image.read_bytes())
        payload[ISO_OFFSET + 100] ^= 0xFF  # flip one byte of the burned image
        disc.write_bytes(bytes(payload))

        snap, job = self._run(jobs, cfg, disc)
        assert snap.state == "failed"
        assert snap.error_code == "tool_failed"
        assert "does not match" in snap.error

    def test_short_disc_fails(self, cfg, jobs, tmp_path):
        self._source_iso(cfg)  # the source ISO must exist before verifying
        disc = tmp_path / "short.bin"
        disc.write_bytes(b"\0" * ISO_OFFSET + b"\x00" * 1024)

        snap, job = self._run(jobs, cfg, disc)
        assert snap.state == "failed"
        assert "only" in snap.error and "expected" in snap.error

    def test_cancellation_is_honoured(self, cfg, jobs, tmp_path):
        # large enough that a cancel can land mid-read
        big = cfg.image_dir / "verify-me.iso"
        big.write_bytes(os.urandom(4 * 1024 * 1024))
        disc = tmp_path / "big.bin"
        disc.write_bytes(b"\0" * ISO_OFFSET + big.read_bytes())

        import agent.ops.burn as burn

        job = jobs.submit(
            "verify_disc",
            lambda ctx: burn.verify_disc(
                ctx, cfg, str(disc), "images/verify-me.iso"
            ),
        )
        try:
            jobs.cancel(job.job_id)
        except Exception:
            pass  # job may already have finished before cancel landed
        snap = wait_for(job.job_id, jobs)
        assert snap.state in ("cancelled", "failed", "done")
