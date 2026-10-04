"""Media operations: blank-disc handling and privilege escalation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.protocol import ERR_NO_FILESYSTEM, AgentError


def _cfg(tmp_path):
    from agent.config import AgentConfig

    return AgentConfig(data_root=tmp_path / "data")


class _FakeTools:
    def __init__(self, udisks: str | None = None) -> None:
        self._udisks = udisks

    def which(self, name: str) -> str | None:
        return self._udisks if name == "udisksctl" else None

    def require(self, name: str) -> str:
        return f"/usr/bin/{name}"


class TestBlankDisc:
    def test_mount_blank_disc_raises_clear_error(self, tmp_path, monkeypatch):
        from agent.ops import media

        monkeypatch.setattr(
            media,
            "require_media",
            lambda device: SimpleNamespace(
                mounted=False, mountpoint=None, fstype="", label=""
            ),
        )
        with pytest.raises(AgentError) as exc:
            media.mount_device(_cfg(tmp_path), "/dev/sr0")
        assert exc.value.code == ERR_NO_FILESYSTEM
        assert "blank" in exc.value.message
        assert "/dev/sr0" in exc.value.message

    def test_mount_with_filesystem_escalates(self, tmp_path, monkeypatch):
        from agent.ops import media

        calls: list[dict] = []
        monkeypatch.setattr(
            media,
            "require_media",
            lambda device: SimpleNamespace(
                mounted=False, mountpoint=None, fstype="iso9660", label="DATA"
            ),
        )
        monkeypatch.setattr(media, "tools", _FakeTools(udisks=None))
        monkeypatch.setattr(
            media,
            "run_checked",
            lambda cmd, **kw: calls.append({"cmd": cmd, **kw})
            or SimpleNamespace(stdout="", returncode=0),
        )
        target = media.mount_device(_cfg(tmp_path), "/dev/sr0")
        assert len(calls) == 1
        assert calls[0]["escalate"] is True
        assert "/dev/sr0" in calls[0]["cmd"]
        assert "mnt" in target

    def test_already_mounted_short_circuits(self, tmp_path, monkeypatch):
        from agent.ops import media

        monkeypatch.setattr(
            media,
            "require_media",
            lambda device: SimpleNamespace(
                mounted=True, mountpoint="/media/x", fstype="", label=""
            ),
        )
        assert media.mount_device(_cfg(tmp_path), "/dev/sr0") == "/media/x"


class TestEjectAndUnmount:
    def test_eject_escalates(self, tmp_path, monkeypatch):
        from agent.ops import media

        calls: list[dict] = []
        monkeypatch.setattr(
            media, "describe_device", lambda device: SimpleNamespace(mounted=False)
        )
        monkeypatch.setattr(media, "tools", _FakeTools())
        monkeypatch.setattr(
            media,
            "run_checked",
            lambda cmd, **kw: calls.append({"cmd": cmd, **kw})
            or SimpleNamespace(stdout="", returncode=0),
        )
        result = media.eject(_cfg(tmp_path), "/dev/sr0")
        assert result["action"] == "ejected"
        assert calls[0]["escalate"] is True
        assert calls[0]["cmd"][-1] == "/dev/sr0"

    def test_close_tray_escalates(self, tmp_path, monkeypatch):
        from agent.ops import media

        calls: list[dict] = []
        monkeypatch.setattr(media, "tools", _FakeTools())
        monkeypatch.setattr(
            media,
            "run_checked",
            lambda cmd, **kw: calls.append({"cmd": cmd, **kw})
            or SimpleNamespace(stdout="", returncode=0),
        )
        media.close_tray(_cfg(tmp_path), "/dev/sr0")
        assert calls[0]["escalate"] is True
        assert "-t" in calls[0]["cmd"]

    def test_unmount_falls_back_to_umount_with_escalation(self, tmp_path, monkeypatch):
        from agent.ops import media

        calls: list[dict] = []
        monkeypatch.setattr(
            media,
            "_mountpoint_for",
            lambda cfg, device: str(tmp_path / "mnt" / "sr0"),
        )
        monkeypatch.setattr(media, "tools", _FakeTools(udisks=None))
        monkeypatch.setattr(
            media,
            "run_checked",
            lambda cmd, **kw: calls.append({"cmd": cmd, **kw})
            or SimpleNamespace(stdout="", returncode=0),
        )
        media.unmount_device(_cfg(tmp_path), "/dev/sr0")
        assert len(calls) == 1
        assert calls[0]["escalate"] is True
        assert calls[0]["cmd"][0] == "/usr/bin/umount"
