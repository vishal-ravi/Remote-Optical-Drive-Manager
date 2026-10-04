"""Optical media operations: mount, unmount, browse, eject, tray control."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from common.protocol import (
    ERR_NO_FILESYSTEM,
    ERR_NOT_MOUNTED,
    ERR_TOOL_FAILED,
    ERR_VALIDATION,
    AgentError,
    DirEntry,
)
from common.validation import (
    ensure_within,
    validate_device,
    validate_relative_path,
    resolve_in_root,
)
from .. import tools
from ..config import AgentConfig
from ..drives import describe_device, require_media
from ..process import run_checked

log = logging.getLogger("rodm.agent.ops.media")

_MOUNTED_RE = re.compile(r"Mounted\s+.+?\s+at\s+(?P<path>.+?)\.?\s*$")

# discard volume labels that would make ugly/unusable mount directories
_UNSAFE_MOUNT_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _mountpoint_for(cfg: AgentConfig, device: str) -> str | None:
    info = describe_device(device)
    return info.mountpoint


def mount_device(cfg: AgentConfig, device: str, *, readonly: bool = True) -> str:
    """Mount the disc and return its mountpoint."""
    device = validate_device(device)
    info = require_media(device)
    if info.mounted and info.mountpoint:
        return info.mountpoint
    if not info.fstype:
        raise AgentError(
            f"{device} has no filesystem - the disc is blank, unformatted or "
            "an audio CD, so it cannot be browsed",
            ERR_NO_FILESYSTEM,
        )

    udisksctl = tools.which("udisksctl")
    if udisksctl:
        try:
            proc = run_checked(
                [udisksctl, "mount", "-b", device, "-o", "ro" if readonly else "rw"],
                timeout=60,
                error_hint="check that udisks2/polkit allow this user to mount",
            )
            match = _MOUNTED_RE.search(proc.stdout)
            if match:
                return match.group("path").strip()
        except AgentError as exc:
            log.info("udisksctl mount failed, falling back to mount(8): %s", exc.message)

    label = _UNSAFE_MOUNT_RE.sub("_", info.label or "").strip() or os.path.basename(device)
    target = cfg.mount_root / f"{device.replace('/dev/', '')}_{label}"[:120]
    target.mkdir(parents=True, exist_ok=True)
    mount_bin = tools.require("mount")
    run_checked(
        [mount_bin, "-o", "ro", device, str(target)],
        timeout=60,
        escalate=True,
        error_hint=(
            "run the agent as root (rodm-agent --root), or grant passwordless "
            f"sudo: echo 'Cmnd_Alias R = {mount_bin} -o ro /dev/sr*' | "
            "sudo tee /etc/sudoers.d/rodm"
        ),
    )
    return str(target)


def unmount_device(cfg: AgentConfig, device: str) -> None:
    device = validate_device(device)
    mountpoint = _mountpoint_for(cfg, device)
    if not mountpoint:
        return  # already unmounted - idempotent

    udisksctl = tools.which("udisksctl")
    if udisksctl and mountpoint.startswith(("/media", "/run/media", "/mnt")):
        try:
            run_checked(
                [udisksctl, "unmount", "-b", device],
                timeout=60,
                escalate=True,
                error_hint="close any programs still using the disc",
            )
            return
        except AgentError as exc:
            log.info("udisksctl unmount failed, falling back to umount: %s", exc.message)

    umount = tools.require("umount")
    run_checked(
        [umount, mountpoint],
        timeout=60,
        escalate=True,
        error_hint="close any programs still using the disc",
    )


def ensure_mounted(cfg: AgentConfig, device: str) -> str:
    device = validate_device(device)
    info = describe_device(device)
    if info.mounted and info.mountpoint:
        return info.mountpoint
    return mount_device(cfg, device)


def browse(cfg: AgentConfig, device: str, path: str = "/", *, mount: bool = True) -> dict:
    """List one directory of the mounted disc.

    ``path`` is always relative to the disc root so a client can never walk
    into the agent host filesystem.
    """
    device = validate_device(device)
    relative = validate_relative_path(path) if path not in ("", "/") else ""
    root = ensure_mounted(cfg, device) if mount else (_mountpoint_for(cfg, device) or "")
    if not root:
        raise AgentError(f"{device} is not mounted", ERR_NOT_MOUNTED)
    root_path = Path(root)
    target = ensure_within(root_path, root_path / relative) if relative else root_path
    if not target.exists():
        raise AgentError(f"{relative or '/'} does not exist on this disc", ERR_VALIDATION)
    if not target.is_dir():
        raise AgentError(f"{relative} is not a directory", ERR_VALIDATION)

    entries: list[DirEntry] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError as exc:
        raise AgentError(f"cannot list {relative or '/'}: {exc}", ERR_TOOL_FAILED) from exc
    for child in children:
        try:
            info = child.lstat()
        except OSError:
            continue
        entries.append(
            DirEntry(
                name=child.name,
                path=f"{relative}/{child.name}" if relative else child.name,
                is_dir=child.is_dir(),
                size=int(info.st_size),
                mtime=float(info.st_mtime),
            )
        )
    return {
        "device": device,
        "mountpoint": str(root_path),
        "path": relative,
        "parent": "/".join(relative.split("/")[:-1]) if relative else None,
        "entries": [entry.to_dict() for entry in entries],
    }


def resolve_media_path(cfg: AgentConfig, device: str, path: str) -> Path:
    """Resolve a path on the disc to an absolute host path (for SFTP/REST)."""
    device = validate_device(device)
    root = _mountpoint_for(cfg, device)
    if not root:
        raise AgentError(f"{device} is not mounted", ERR_NOT_MOUNTED)
    relative = validate_relative_path(path) if path not in ("", "/") else ""
    root_path = Path(root)
    target = ensure_within(root_path, root_path / relative) if relative else root_path
    if not target.exists():
        raise AgentError(f"{path} does not exist on this disc", ERR_VALIDATION)
    return target


def eject(cfg: AgentConfig, device: str) -> dict:
    device = validate_device(device)
    info = describe_device(device)
    if info.mounted:
        try:
            unmount_device(cfg, device)
        except AgentError as exc:
            log.warning("unmount before eject failed: %s", exc.message)
    eject_bin = tools.require("eject")
    run_checked(
        [eject_bin, device], timeout=30, escalate=True, error_hint="is the tray locked?"
    )
    return {"device": device, "action": "ejected"}


def close_tray(cfg: AgentConfig, device: str) -> dict:
    device = validate_device(device)
    eject_bin = tools.require("eject")
    run_checked(
        [eject_bin, "-t", device],
        timeout=30,
        escalate=True,
        error_hint="is the tray motor OK?",
    )
    return {"device": device, "action": "closed"}


def stage_list(cfg: AgentConfig, path: str = "stage") -> dict:
    """List files staged for ISO creation / burning (inside the data root)."""
    target = resolve_in_root(cfg.data_root, path)
    if not target.exists():
        return {"path": path, "entries": []}
    entries: list[dict] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            info = child.stat()
        except OSError:
            continue
        entries.append(
            DirEntry(
                name=child.name,
                path=f"{path}/{child.name}" if path != "." else child.name,
                is_dir=child.is_dir(),
                size=int(info.st_size) if child.is_file() else 0,
                mtime=float(info.st_mtime),
            ).to_dict()
        )
    return {"path": path, "entries": entries}
