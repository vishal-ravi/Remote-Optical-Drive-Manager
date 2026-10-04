"""Input validation and the safety rail around block devices and file paths.

The agent must never be able to touch a non-optical block device
(`/dev/sda`, `/dev/nvme0n1`, ...) or read/write outside its data root.
Every rule here is enforced on the *agent* side, regardless of transport.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .protocol import (
    ERR_INVALID_DEVICE,
    ERR_VALIDATION,
    AgentError,
)

#: Only optical-class device nodes are ever accepted.
DEVICE_RE = re.compile(r"^/dev/(sr[0-9]+|cdrom[0-9]*|scd[0-9]+|optical[0-9]*)$")

#: Block device names that must never be accepted even if they slip past.
FORBIDDEN_DEVICE_RE = re.compile(
    r"^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+|vd[a-z]+|md[0-9]+|dm-[0-9]+)"
)

MAX_LABEL_LEN = 128
MAX_SPEED = 999
MIN_SPEED = 1
MAX_ISO_NAME_LEN = 200
DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
DEFAULT_MAX_STAGED_FILES = 4096

_ISO_SUFFIXES = (".iso", ".img")


def _fail(message: str, code: str = ERR_VALIDATION) -> None:
    raise AgentError(message, code=code)


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #


def validate_device(device: Any) -> str:
    """Return a normalised optical device path or raise."""
    if not isinstance(device, str) or not device.strip():
        _fail("device path is required", ERR_INVALID_DEVICE)
    path = device.strip()
    if not path.startswith("/"):
        path = "/dev/" + path
    path = os.path.normpath(path)

    if FORBIDDEN_DEVICE_RE.match(path):
        _fail(
            f"{path} is not an optical drive; refusing to touch system storage",
            ERR_INVALID_DEVICE,
        )
    if not DEVICE_RE.match(path):
        _fail(f"{path} is not a supported optical device", ERR_INVALID_DEVICE)
    return path


def validate_device_exists(device: str, *, require_node: bool = True) -> str:
    """Validate then confirm the node exists on this machine."""
    path = validate_device(device)
    if require_node and not os.path.exists(path):
        _fail(f"{path} does not exist", ERR_INVALID_DEVICE)
    return path


def is_optical_udev(path: str) -> bool:
    """Ask udev whether the node really is a CD/DVD device.

    Returns True when udev cannot be queried (e.g. tests) but the path
    already passed :func:`validate_device`; a definitive ``ID_CDROM=0``
    answer is treated as failure.
    """
    import shutil
    import subprocess

    udevadm = shutil.which("udevadm")
    if not udevadm:
        return True
    try:
        proc = subprocess.run(
            [udevadm, "info", "--query=property", f"--name={path}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if proc.returncode != 0:
        return False
    props = dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
    )
    if "ID_CDROM" in props:
        return props["ID_CDROM"] == "1"
    # No ID_CDROM property: accept only if udev still classes it as a rom device.
    return props.get("DEVTYPE") == "disk" and "/sr" in path


# --------------------------------------------------------------------------- #
# Filesystem paths
# --------------------------------------------------------------------------- #


def sanitize_name(name: Any, *, field: str = "name", allow_dot: bool = False) -> str:
    """Accept a single path component; reject traversal and control chars."""
    if not isinstance(name, str) or not name.strip():
        _fail(f"{field} is required")
    value = unicodedata.normalize("NFC", name.strip())
    if len(value) > MAX_ISO_NAME_LEN:
        _fail(f"{field} is too long (max {MAX_ISO_NAME_LEN} characters)")
    if value in (".", "..") and not allow_dot:
        _fail(f"{field} may not be '{value}'")
    if "/" in value or "\x00" in value:
        _fail(f"{field} must not contain '/' or NUL")
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        _fail(f"{field} contains control characters")
    return value


def validate_relative_path(path: Any, *, field: str = "path") -> str:
    """Validate a path relative to the agent data root (no traversal out)."""
    if not isinstance(path, str) or not path.strip():
        _fail(f"{field} is required")
    raw = path.strip().replace("\\", "/")
    if "\x00" in raw:
        _fail(f"{field} contains NUL")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        _fail(f"{field} may not contain '..'")
    cleaned = "/".join(parts)
    if cleaned.startswith("/"):
        _fail(f"{field} must be relative")
    return cleaned


def ensure_within(root: Path | str, candidate: Path | str) -> Path:
    """Resolve ``candidate`` and prove it lives inside ``root``."""
    root_path = Path(root).resolve()
    cand_path = Path(candidate).resolve()
    try:
        cand_path.relative_to(root_path)
    except ValueError:
        _fail(f"path escapes the agent data root: {candidate}")
    return cand_path


def resolve_in_root(root: Path | str, relative: str) -> Path:
    rel = validate_relative_path(relative)
    return ensure_within(root, Path(root) / rel)


def validate_iso_name(name: Any) -> str:
    value = sanitize_name(name, field="iso name")
    if not value.lower().endswith(_ISO_SUFFIXES):
        value += ".iso"
    return value


def validate_speed(speed: Any) -> int | None:
    """Return an explicit drive speed, or ``None`` for "let the drive decide"."""
    if speed is None or speed == "":
        return None
    if isinstance(speed, bool):
        _fail("speed must be an integer (CD/DVD speed units) or 'auto'")
    if isinstance(speed, int):
        value = speed
    elif isinstance(speed, str) and speed.strip().lower() == "auto":
        return None
    elif isinstance(speed, str) and speed.strip().isdigit():
        value = int(speed.strip())
    else:
        _fail("speed must be an integer (CD/DVD speed units) or 'auto'")
        return None
    if not MIN_SPEED <= value <= MAX_SPEED:
        _fail(f"speed must be between {MIN_SPEED} and {MAX_SPEED}")
    return value


def validate_bool(value: Any, *, default: bool = False, field: str = "flag") -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    _fail(f"{field} must be a boolean")
    return default  # unreachable


def validate_choice(
    value: Any, choices: Iterable[str], *, field: str, default: str | None = None
) -> str:
    if value is None or value == "":
        if default is None:
            _fail(f"{field} is required")
        return default
    if not isinstance(value, str) or value not in set(choices):
        _fail(f"{field} must be one of: {', '.join(sorted(choices))}")
    return value


def validate_limit(
    value: Any, *, field: str, default: int, minimum: int, maximum: int
) -> int:
    if value is None or value == "":
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError):
        _fail(f"{field} must be an integer")
    if not minimum <= limit <= maximum:
        _fail(f"{field} must be between {minimum} and {maximum}")
    return limit


def validate_device_size(device: str, max_bytes: int) -> None:
    """Refuse ISOs bigger than the medium currently in the drive."""
    try:
        st = os.stat(device)
    except OSError:
        return
    size = getattr(st, "st_size", 0)
    if size and size > max_bytes:
        _fail(
            f"image is larger than the medium in {device} "
            f"({size} > {max_bytes} bytes)"
        )


def validate_staged_paths(
    root: Path | str, paths: Any, *, field: str = "paths"
) -> list[Path]:
    """Validate a list of files/dirs already living inside the data root."""
    if not isinstance(paths, (list, tuple)) or not paths:
        _fail(f"{field} must be a non-empty list")
    if len(paths) > DEFAULT_MAX_STAGED_FILES:
        _fail(f"{field} may contain at most {DEFAULT_MAX_STAGED_FILES} entries")
    resolved: list[Path] = []
    for item in paths:
        if not isinstance(item, str) or not item.strip():
            _fail(f"{field} contains an empty entry")
        target = resolve_in_root(root, item)
        if not target.exists():
            _fail(f"{field}: {item} does not exist")
        resolved.append(target)
    return resolved


def validate_dir_nonempty(path: Path) -> Path:
    if not path.is_dir():
        _fail(f"{path} is not a directory")
    if not any(path.iterdir()):
        _fail(f"{path} is empty")
    return path


def safe_file_size(path: Path) -> int:
    try:
        info = path.lstat()
    except OSError as exc:
        _fail(f"cannot stat {path.name}: {exc}")
    if stat.S_ISLNK(info.st_mode):
        info = path.stat()
    return int(info.st_size)


def validate_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        _fail("job_id is required")
    value = job_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        _fail("job_id has an invalid format")
    return value


def validate_page_limit(params: dict[str, Any]) -> tuple[int, int]:
    offset = validate_limit(
        params.get("offset"), field="offset", default=0, minimum=0, maximum=10**9
    )
    limit = validate_limit(
        params.get("limit"), field="limit", default=5000, minimum=1, maximum=50000
    )
    return offset, limit
