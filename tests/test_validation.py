import pytest

from common.protocol import AgentError
from common.validation import (
    DEVICE_RE,
    ensure_within,
    is_optical_udev,
    sanitize_name,
    validate_bool,
    validate_choice,
    validate_device,
    validate_iso_name,
    validate_job_id,
    validate_page_limit,
    validate_relative_path,
    validate_speed,
    validate_staged_paths,
)


class TestDeviceValidation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/dev/sr0", "/dev/sr0"),
            ("/dev/sr1", "/dev/sr1"),
            ("sr0", "/dev/sr0"),
            ("/dev/cdrom", "/dev/cdrom"),
            ("/dev/cdrom0", "/dev/cdrom0"),
            ("/dev/optical0", "/dev/optical0"),
            ("/dev//sr0", "/dev/sr0"),
            ("/dev/sr0/", "/dev/sr0"),
        ],
    )
    def test_accepts_optical_devices(self, raw, expected):
        assert validate_device(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "/dev/sda",
            "/dev/sda1",
            "/dev/nvme0n1",
            "/dev/nvme0n1p1",
            "/dev/mmcblk0",
            "/dev/vda",
            "/dev/dm-0",
            "/dev/zero",
            "/dev/sr0/../sda",
            "sda",
            "",
            None,
            42,
            "/tmp/sr0",
            "/dev/srq",
        ],
    )
    def test_rejects_non_optical_devices(self, raw):
        with pytest.raises(AgentError) as exc:
            validate_device(raw)
        assert exc.value.code == "invalid_device"

    def test_device_regex(self):
        assert DEVICE_RE.match("/dev/sr0")
        assert not DEVICE_RE.match("/dev/sda")

    def test_udev_check_without_udevadm_is_permissive(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda *_: None)
        assert is_optical_udev("/dev/sr0") is True

    def test_udev_check_rejects_when_udev_says_no(self, monkeypatch):
        class Proc:
            returncode = 0
            stdout = "ID_CDROM=0\nDEVNAME=/dev/sda\n"

        monkeypatch.setattr("shutil.which", lambda *_: "/usr/bin/udevadm")
        import subprocess

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
        assert is_optical_udev("/dev/sr0") is False


class TestNameValidation:
    @pytest.mark.parametrize("name", ["disc.iso", "My Disc 1.iso", "backup.img"])
    def test_valid_names(self, name):
        assert sanitize_name(name) == name

    @pytest.mark.parametrize("name", ["", "  ", ".", "..", "a/b", "a\x00b", "a\nb"])
    def test_invalid_names(self, name):
        with pytest.raises(AgentError):
            sanitize_name(name)

    def test_iso_suffix_added(self):
        assert validate_iso_name("movie") == "movie.iso"
        assert validate_iso_name("movie.iso") == "movie.iso"
        assert validate_iso_name("movie.IMG") == "movie.IMG"

    def test_job_id(self):
        assert validate_job_id("abc-123_X") == "abc-123_X"
        for bad in ["", "a b", "a/b", "../x", None, "x" * 65]:
            with pytest.raises(AgentError):
                validate_job_id(bad)


class TestRelativePaths:
    @pytest.mark.parametrize("raw", ["iso/a.iso", "/iso/a.iso", "./iso/a.iso"])
    def test_normalises(self, raw):
        assert validate_relative_path(raw) == "iso/a.iso"

    @pytest.mark.parametrize("raw", ["../x", "a/../../b", "a\x00b", "", None])
    def test_rejects_traversal(self, raw):
        with pytest.raises(AgentError):
            validate_relative_path(raw)

    def test_ensure_within(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "ok.txt").write_text("x")
        assert ensure_within(root, root / "ok.txt").name == "ok.txt"
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        with pytest.raises(AgentError):
            ensure_within(root, outside)

    def test_staged_paths(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        got = validate_staged_paths(tmp_path, ["a.txt", "b.txt"])
        assert [p.name for p in got] == ["a.txt", "b.txt"]
        with pytest.raises(AgentError):
            validate_staged_paths(tmp_path, ["missing.txt"])
        with pytest.raises(AgentError):
            validate_staged_paths(tmp_path, [])
        with pytest.raises(AgentError):
            validate_staged_paths(tmp_path, [str(tmp_path / "a.txt")])


class TestScalars:
    @pytest.mark.parametrize(
        "raw,expected",
        [(None, None), ("auto", None), ("", None), (1, 1), ("4", 4), (16, 16)],
    )
    def test_speed(self, raw, expected):
        assert validate_speed(raw) == expected

    @pytest.mark.parametrize("raw", [0, -1, 1000, "fast", 1.5])
    def test_bad_speed(self, raw):
        with pytest.raises(AgentError):
            validate_speed(raw)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, True),
            ("yes", True),
            (False, False),
            ("off", False),
            (1, True),
            ("", True),
        ],
    )
    def test_bool(self, raw, expected):
        assert validate_bool(raw, default=True) is expected

    def test_bad_bool(self):
        with pytest.raises(AgentError):
            validate_bool("maybe")

    def test_choice(self):
        assert validate_choice("md5", ["md5", "sha256"], field="algo", default="md5") == "md5"
        assert validate_choice(None, ["md5"], field="algo", default="md5") == "md5"
        with pytest.raises(AgentError):
            validate_choice("crc32", ["md5", "sha256"], field="algo", default="md5")

    def test_page_limit(self):
        assert validate_page_limit({}) == (0, 5000)
        assert validate_page_limit({"offset": 10, "limit": 5}) == (10, 5)
        with pytest.raises(AgentError):
            validate_page_limit({"limit": 0})
