"""Burning and post-burn verification."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from common.protocol import ERR_TOOL_FAILED, ERR_VALIDATION, AgentError
from common.validation import resolve_in_root, validate_device, validate_speed
from .. import tools
from ..config import AgentConfig
from ..drives import optical_capacity, require_media
from ..jobs import JobContext
from ..process import extract_percent, stream_command

log = logging.getLogger("rodm.agent.ops.burn")

#: cdrecord/wodim writes ISO images starting at LBA 16 (16 * 2048 bytes)
ISO_SECTOR_OFFSET = 16 * 2048
RATIO_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def resolve_image(cfg: AgentConfig, name: str) -> Path:
    """Accept ``images/x.iso`` or a bare ``x.iso`` and confine it to the root."""
    if not isinstance(name, str) or not name.strip():
        raise AgentError("iso name is required", ERR_VALIDATION)
    candidate = name.strip()
    for relative in (candidate, f"images/{candidate}", f"stage/{candidate}"):
        try:
            path = resolve_in_root(cfg.data_root, relative)
        except AgentError:
            continue
        if path.is_file():
            return path
    raise AgentError(
        f"'{name}' was not found in the agent images folder", ERR_VALIDATION
    )


def _require_writable_drive(cfg: AgentConfig, device: str):

    info = require_media(device)
    if not info.writable:
        raise AgentError(f"{device} is a read-only optical drive", ERR_VALIDATION)
    return info


def burn_iso(
    ctx: JobContext,
    cfg: AgentConfig,
    device: str,
    iso: str,
    *,
    speed: int | None = None,
    overburn: bool = False,
    eject_after: bool = False,
    simulate: bool = False,
) -> dict:
    """Write ``iso`` to the disc currently loaded in ``device``."""
    device = validate_device(device)
    _require_writable_drive(cfg, device)
    image = resolve_image(cfg, iso)
    speed = validate_speed(speed)

    iso_size = image.stat().st_size
    capacity = optical_capacity(device)
    if capacity and iso_size > capacity and not overburn:
        raise AgentError(
            f"{image.name} ({iso_size} B) does not fit on the medium "
            f"({capacity} B); enable overburn to force it",
            ERR_VALIDATION,
        )

    wodim = tools.require("wodim")
    cmd = [wodim, f"dev={device}", "-data", "-v"]
    if simulate:
        cmd.append("-dummy")
    if speed is not None:
        cmd.append(f"-speed={speed}")
    if overburn:
        cmd.append("-overburn")
    cmd.append(str(image))

    ctx.status(
        device=device,
        image=str(image),
        image_bytes=iso_size,
        capacity_bytes=capacity,
        speed=speed,
        simulate=simulate,
    )
    ctx.log(f"burning {image.name} to {device} at {'max' if speed is None else str(speed) + 'x'}")
    ctx.progress(0.0, "starting burn")

    last_message = ""

    def on_line(line: str) -> None:
        nonlocal last_message
        ctx.check_cancelled()
        text = line.strip()
        if not text:
            return
        percent = extract_percent(text)
        if percent is None:
            ratio = RATIO_RE.search(text)
            if ratio:
                try:
                    done, total = int(ratio.group(1)), int(ratio.group(2))
                except ValueError:
                    done = total = 0
                if total > 0:
                    percent = min(1.0, done / total)
        if percent is not None:
            last_message = text
            ctx.progress(percent, text)
            return
        if text.lower().startswith(("writing", "fixating", "flushing", "closing")):
            last_message = text
            ctx.status(step=text)
        ctx.log(text)

    stream_command(
        cmd,
        on_line=on_line,
        timeout=60 * 60,
        cancel_check=ctx.check_cancelled,
        kill_on_cancel=ctx._job,
        escalate=True,
    )

    if eject_after:
        try:
            from ..ops.media import eject

            eject(cfg, device)
            ctx.log(f"ejected {device}")
        except AgentError as exc:  # burn succeeded, tray did not open
            ctx.log(f"burn finished but eject failed: {exc.message}")

    ctx.progress(1.0, last_message or "burn complete")
    return {
        "device": device,
        "image": str(image),
        "image_name": image.name,
        "image_bytes": iso_size,
        "speed": speed,
        "simulated": simulate,
        "ejected": eject_after,
    }


def _hash_file(path: Path, ctx: JobContext, *, label: str) -> str:
    digest = hashlib.md5()
    total = path.stat().st_size
    read = 0
    with path.open("rb") as handle:
        while True:
            ctx.check_cancelled()
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
            if total:
                ctx.progress(0.1 * read / total, f"{label} {read}/{total} bytes")
    return digest.hexdigest()


def _read_windows(
    device: str,
    windows: list[tuple[int, int, "hashlib._Hash"]],
    needed: int,
    ctx: JobContext,
) -> int:
    """Stream the disc once, feeding every requested byte window."""
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    pos = 0
    try:
        while pos < needed:
            ctx.check_cancelled()
            try:
                chunk = os.read(fd, 4 * 1024 * 1024)
            except OSError as exc:
                raise AgentError(
                    f"failed reading {device} after {pos} bytes: {exc}",
                    ERR_TOOL_FAILED,
                ) from exc
            if not chunk:
                break
            for start, end, digest in windows:
                lo = max(start, pos)
                hi = min(end, pos + len(chunk))
                if lo < hi:
                    digest.update(chunk[lo - pos : hi - pos])
            pos += len(chunk)
            if needed:
                ctx.progress(min(1.0, pos / needed), f"read {pos} / {needed} bytes")
    finally:
        os.close(fd)
    return pos


def verify_disc(
    ctx: JobContext,
    cfg: AgentConfig,
    device: str,
    iso: str,
) -> dict:
    """Compare the burned disc against the source ISO.

    Reads back ``len(iso)`` bytes twice-worth of offsets (0 and the 32 KiB
    ISO sector offset used by cdrecord) in a single pass and reports which
    window matches. Only the ISO-sized prefix is read - media padding after
    the image is ignored on purpose, and nothing is written to disk.
    """
    device = validate_device(device)
    require_media(device)
    image = resolve_image(cfg, iso)

    ctx.status(device=device, image=str(image), phase="hash-source")
    ctx.log(f"hashing source {image.name}")
    source_md5 = _hash_file(image, ctx, label="hashing source")
    iso_size = image.stat().st_size
    if iso_size <= 0:
        raise AgentError(f"{image.name} is empty", ERR_VALIDATION)

    capacity = optical_capacity(device)
    if capacity and iso_size + ISO_SECTOR_OFFSET > capacity:
        raise AgentError(
            f"disc in {device} is too small to contain {image.name}",
            ERR_VALIDATION,
        )

    windows = [
        (0, iso_size, hashlib.md5()),
        (ISO_SECTOR_OFFSET, ISO_SECTOR_OFFSET + iso_size, hashlib.md5()),
    ]
    needed = ISO_SECTOR_OFFSET + iso_size

    ctx.status(phase="readback", source_md5=source_md5, bytes_needed=needed)
    ctx.log(f"reading back {iso_size} bytes from {device}")
    read = _read_windows(device, windows, needed, ctx)
    if read < iso_size:
        raise AgentError(
            f"disc returned only {read} bytes, expected at least {iso_size}",
            ERR_TOOL_FAILED,
        )

    # a window is only meaningful once every one of its bytes was read
    covered_offset = read >= ISO_SECTOR_OFFSET + iso_size
    md5_zero = windows[0][2].hexdigest()
    md5_offset = windows[1][2].hexdigest() if covered_offset else None

    matched = None
    if md5_zero == source_md5:
        matched = 0
    elif md5_offset == source_md5:
        matched = ISO_SECTOR_OFFSET

    if matched is None:
        ctx.progress(1.0, "verification failed")
        detail = (
            f"disc md5@{ISO_SECTOR_OFFSET} {md5_offset}"
            if covered_offset
            else "second window not present on the disc"
        )
        raise AgentError(
            f"disc content does not match {image.name} "
            f"(source md5 {source_md5}, disc md5@0 {md5_zero}, {detail})",
            ERR_TOOL_FAILED,
        )

    ctx.progress(1.0, "verification passed")
    ctx.log(f"verified: md5 {source_md5} matches at byte offset {matched}")
    return {
        "device": device,
        "image": str(image),
        "image_name": image.name,
        "bytes_compared": iso_size,
        "source_md5": source_md5,
        "disc_md5": source_md5,
        "match_offset": matched,
        "verified": True,
    }
