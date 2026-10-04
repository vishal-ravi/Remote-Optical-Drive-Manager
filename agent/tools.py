"""Locate the external optical utilities the agent is allowed to invoke."""

from __future__ import annotations

import shutil
from functools import lru_cache

from common.protocol import ERR_TOOL_MISSING, AgentError

#: name -> candidate binaries, first found wins
_TOOLS: dict[str, tuple[str, ...]] = {
    "lsblk": ("lsblk",),
    "blkid": ("blkid",),
    "udevadm": ("udevadm",),
    "mount": ("mount",),
    "umount": ("umount",),
    "udisksctl": ("udisksctl",),
    "eject": ("eject",),
    "wodim": ("wodim", "cdrecord"),
    "genisoimage": ("genisoimage", "mkisofs"),
    "dd": ("dd",),
    "openssl": ("openssl",),
}


@lru_cache(maxsize=None)
def which(tool: str) -> str | None:
    candidates = _TOOLS.get(tool, (tool,))
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def require(tool: str) -> str:
    path = which(tool)
    if not path:
        raise AgentError(
            f"required utility '{tool}' is not installed on this machine",
            ERR_TOOL_MISSING,
        )
    return path


def available_tools() -> dict[str, str | None]:
    return {name: which(name) for name in sorted(_TOOLS)}
