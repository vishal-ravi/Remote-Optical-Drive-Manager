"""Optical drive operations executed by the agent core."""

from .burn import burn_iso, resolve_image, verify_disc
from .images import create_iso_from_disc, create_iso_from_files, list_images
from .media import (
    browse,
    close_tray,
    eject,
    ensure_mounted,
    mount_device,
    resolve_media_path,
    stage_list,
    unmount_device,
)

__all__ = [
    "browse",
    "burn_iso",
    "close_tray",
    "create_iso_from_disc",
    "create_iso_from_files",
    "eject",
    "ensure_mounted",
    "list_images",
    "mount_device",
    "resolve_image",
    "resolve_media_path",
    "stage_list",
    "unmount_device",
    "verify_disc",
]
