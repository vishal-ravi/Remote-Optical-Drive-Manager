"""Discover optical drives and describe the media inside them.

Sources: ``lsblk`` for the device list, ``udevadm`` for optical properties,
``blkid`` for filesystem/label, ``/proc/mounts`` for mount state and
``/sys/block/<name>/size`` for the raw medium capacity.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from common.protocol import ERR_INVALID_DEVICE, ERR_NO_MEDIA, AgentError, DriveInfo
from common.validation import DEVICE_RE, FORBIDDEN_DEVICE_RE, validate_device
from . import tools

log = logging.getLogger("rodm.agent.drives")

_DEV_LINK_RE = re.compile(r"^/dev/(sr[0-9]+|cdrom[0-9]*)$")


def _run(cmd: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _udev_properties(device: str) -> dict[str, str]:
    udevadm = tools.which("udevadm")
    if not udevadm:
        return {}
    try:
        proc = _run([udevadm, "info", "--query=property", f"--name={device}"])
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    props: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def _blkid(device: str) -> dict[str, str]:
    blkid = tools.which("blkid")
    if not blkid:
        return {}
    try:
        proc = _run([blkid, "-o", "export", device])
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    return out


def _block_size(device: str) -> int:
    name = os.path.basename(device)
    # resolve /dev/cdrom -> sr0 style symlinks
    try:
        name = os.path.basename(os.path.realpath(device))
    except OSError:
        pass
    sysfs = Path(f"/sys/block/{name}/size")
    if not sysfs.exists():
        # sr0 lives at /sys/block/sr0 normally; try class dir
        alt = Path("/sys/class/block") / name / "size"
        sysfs = alt if alt.exists() else sysfs
    try:
        return int(sysfs.read_text().strip()) * 512
    except (OSError, ValueError):
        return 0


def _mounted_map() -> dict[str, str]:
    mounts: dict[str, str] = {}
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return mounts
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and _DEV_LINK_RE.match(parts[0]):
            mounts[parts[0]] = parts[1]
    # follow /dev/cdrom -> /dev/sr0 aliasing in both directions
    for src, dst in list(mounts.items()):
        try:
            real = os.path.realpath(src)
        except OSError:
            continue
        if real != src and real not in mounts:
            mounts[real] = dst
    return mounts


def _disc_type(props: dict[str, str]) -> str:
    order = [
        ("ID_CDROM_BD", "Blu-ray"),
        ("ID_CDROM_BDR", "Blu-ray-R"),
        ("ID_CDROM_BDRE", "Blu-ray-RE"),
        ("ID_CDROM_DVD_RAM", "DVD-RAM"),
        ("ID_CDROM_DVD_R", "DVD-R"),
        ("ID_CDROM_DVD_RW", "DVD-RW"),
        ("ID_CDROM_DVD_PLUS_R", "DVD+R"),
        ("ID_CDROM_DVD_PLUS_RW", "DVD+RW"),
        ("ID_CDROM_DVD_DL_R", "DVD+R DL"),
        ("ID_CDROM_DVD", "DVD"),
        ("ID_CDROM_CDR", "CD-R"),
        ("ID_CDROM_CDRW", "CD-RW"),
        ("ID_CDROM_CD", "CD"),
    ]
    for key, label in order:
        if props.get(key) == "1":
            return label
    return ""


def _is_writable(props: dict[str, str], device: str) -> bool:
    if props.get("ID_CDROM_R") == "1":
        return True
    writable_keys = (
        "ID_CDROM_DVD_R",
        "ID_CDROM_DVD_RW",
        "ID_CDROM_DVD_PLUS_R",
        "ID_CDROM_DVD_PLUS_RW",
        "ID_CDROM_DVD_DL_R",
        "ID_CDROM_DVD_RAM",
        "ID_CDROM_CDR",
        "ID_CDROM_CDRW",
        "ID_CDROM_BDR",
        "ID_CDROM_BDRE",
    )
    if any(props.get(key) == "1" for key in writable_keys):
        return True
    # last resort: the block device's write-intent sysfs flag
    name = os.path.basename(os.path.realpath(device))
    ro_flag = Path(f"/sys/block/{name}/ro")
    try:
        return ro_flag.read_text().strip() == "0"
    except OSError:
        return False


def _media_present(device: str, props: dict[str, str], blk: dict[str, str]) -> bool:
    """Detect loaded media.

    Order matters: a *blank* recorder disc has no filesystem, so ``blkid``
    must never be the deciding signal.
    """
    if props.get("ID_CDROM_MEDIA") == "1":
        return True
    if props.get("ID_CDROM_MEDIA") == "0":
        return False
    if _block_size(device) > 0:
        return True
    if blk:
        return True
    return False


def describe_device(device: str) -> DriveInfo:
    """Build a :class:`DriveInfo` for one optical device node."""
    device = validate_device(device)
    if not os.path.exists(device):
        raise AgentError(f"{device} does not exist", ERR_INVALID_DEVICE)

    props = _udev_properties(device)
    blk = _blkid(device)
    mounts = _mounted_map()
    mountpoint = mounts.get(device) or mounts.get(os.path.realpath(device))

    vendor = (props.get("ID_VENDOR") or "").replace("_", " ").strip()
    model = (props.get("ID_MODEL") or "").replace("_", " ").strip()
    if not model:
        model = (props.get("ID_SCSI_MODEL") or "").strip()

    media = _media_present(device, props, blk)
    info = DriveInfo(
        device=device,
        model=model,
        vendor=vendor,
        size=str(_block_size(device)) if media else "",
        writable=_is_writable(props, device),
        hotpluggable=props.get("ID_USB_DRIVER") is not None,
        mountpoint=mountpoint,
        mounted=bool(mountpoint),
        media_present=media,
        label=blk.get("LABEL", ""),
        fstype=blk.get("TYPE", ""),
        disc_type=_disc_type(props) if media else "",
        read_only=not _is_writable(props, device),
    )
    return info


def list_optical_drives() -> list[DriveInfo]:
    """Enumerate every optical drive on this machine."""
    devices = _scan_lsblk()
    if not devices:
        devices = _scan_dev()
    drives: list[DriveInfo] = []
    for device in devices:
        if FORBIDDEN_DEVICE_RE.match(device):
            continue
        if not DEVICE_RE.match(device):
            continue
        if not os.path.exists(device):
            continue
        try:
            drives.append(describe_device(device))
        except AgentError as exc:
            log.warning("skipping %s: %s", device, exc.message)
        except Exception:  # pragma: no cover - never let one bad node hide the rest
            log.exception("unexpected error while describing %s", device)
    drives.sort(key=lambda d: d.device)
    return drives


def _scan_lsblk() -> list[str]:
    lsblk = tools.which("lsblk")
    if not lsblk:
        return []
    cmd = [
        lsblk,
        "-J",
        "-d",
        "-o",
        "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,TRAN,RM",
    ]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        log.debug("lsblk failed: %s", proc.stderr.strip())
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []
    found: list[str] = []
    for node in payload.get("blockdevices", []) or []:
        if not isinstance(node, dict):
            continue
        ntype = (node.get("type") or "").lower()
        path = node.get("path") or f"/dev/{node.get('name', '')}"
        tran = (node.get("tran") or "").lower()
        if ntype == "rom" or _DEV_LINK_RE.match(str(path)):
            found.append(str(path))
        elif ntype == "disk" and tran in ("sr", "usb") and str(path).endswith(
            tuple(f"sr{i}" for i in range(8))
        ):
            found.append(str(path))
    # de-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for dev in found:
        if dev not in seen:
            seen.add(dev)
            unique.append(dev)
    return unique


def _scan_dev() -> list[str]:
    """Fallback when lsblk is unavailable: glob /dev/sr* and /dev/cdrom*."""
    import glob

    candidates: list[str] = []
    for pattern in ("/dev/sr[0-9]", "/dev/cdrom*", "/dev/scd[0-9]"):
        candidates.extend(sorted(glob.glob(pattern)))
    return candidates


def require_media(device: str) -> DriveInfo:
    info = describe_device(device)
    if not info.media_present:
        raise AgentError(f"no disc is inserted in {device}", ERR_NO_MEDIA)
    return info


def optical_capacity(device: str) -> int:
    """Size in bytes of the medium currently loaded (0 when unknown/empty)."""
    return _block_size(device)
