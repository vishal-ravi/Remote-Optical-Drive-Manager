"""ISO creation: rips a physical disc or wraps local files into an image."""

from __future__ import annotations

import logging
from pathlib import Path

from common.protocol import ERR_TOOL_FAILED, ERR_VALIDATION, AgentError
from common.validation import (
    sanitize_name,
    validate_device,
    validate_iso_name,
    validate_staged_paths,
)
from .. import tools
from ..config import AgentConfig
from ..drives import optical_capacity, require_media
from ..jobs import JobContext
from ..process import extract_dd_bytes, extract_percent, stream_command

log = logging.getLogger("rodm.agent.ops.images")

DEFAULT_LABEL = "RODM_DISC"


def _safe_out_path(cfg: AgentConfig, name: str, overwrite: bool) -> Path:
    filename = validate_iso_name(name)
    out = cfg.image_dir / filename
    # refuse to collide with anything outside images/
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not overwrite:
        raise AgentError(
            f"{filename} already exists in the images folder (use overwrite)",
            ERR_VALIDATION,
        )
    if out.exists() and not out.is_file():
        raise AgentError(f"{filename} exists but is not a regular file", ERR_VALIDATION)
    return out


def create_iso_from_disc(
    ctx: JobContext,
    cfg: AgentConfig,
    device: str,
    name: str,
    *,
    overwrite: bool = False,
) -> dict:
    """dd the whole medium to ``images/<name>.iso``."""
    device = validate_device(device)
    require_media(device)
    out = _safe_out_path(cfg, name, overwrite)
    total = optical_capacity(device)
    dd = tools.require("dd")

    ctx.status(device=device, target=str(out), total_bytes=total)
    ctx.log(f"ripping {device} -> {out.name} ({total or '?'} bytes)")

    bytes_done = 0

    def on_line(line: str) -> None:
        nonlocal bytes_done
        ctx.check_cancelled()
        copied = extract_dd_bytes(line)
        if copied is not None:
            bytes_done = copied
            fraction = (copied / total) if total else None
            ctx.progress(fraction, f"reading {copied} / {total or '?'} bytes")
            return
        percent = extract_percent(line)
        if percent is not None:
            ctx.progress(percent, line.strip())
            return
        if line.strip():
            ctx.log(line.strip())

    cmd = [
        dd,
        f"if={device}",
        f"of={out}",
        "bs=2M",
        "status=progress",
        "conv=noerror,sync",
        "iflag=fullblock",
    ]
    try:
        stream_command(
            cmd,
            on_line=on_line,
            timeout=60 * 60,
            cancel_check=ctx.check_cancelled,
            kill_on_cancel=ctx._job,
        )
    except AgentError:
        if out.exists() and out.stat().st_size == 0:
            out.unlink(missing_ok=True)
        raise

    size = out.stat().st_size if out.exists() else 0
    if size <= 0:
        out.unlink(missing_ok=True)
        raise AgentError(f"{device} produced no data", ERR_TOOL_FAILED)

    ctx.progress(1.0, f"created {out.name}")
    ctx.log(f"created {out.name} ({size} bytes)")
    return {
        "device": device,
        "path": f"images/{out.name}",
        "absolute_path": str(out),
        "size": size,
        "name": out.name,
    }


def create_iso_from_files(
    ctx: JobContext,
    cfg: AgentConfig,
    source: str,
    name: str,
    *,
    label: str = "",
    overwrite: bool = False,
) -> dict:
    """Wrap a staged directory/file into an ISO with genisoimage."""
    sources = validate_staged_paths(cfg.data_root, [source], field="source")
    src = sources[0]
    out = _safe_out_path(cfg, name, overwrite)
    genisoimage = tools.require("genisoimage")

    volume = sanitize_name(label or DEFAULT_LABEL, field="label")[:32]
    if src.is_dir() and not any(src.iterdir()):
        raise AgentError(f"{source} is empty", ERR_VALIDATION)

    ctx.status(source=str(src), target=str(out))
    ctx.log(f"building ISO from {src.name} with label '{volume}'")

    cmd = [
        genisoimage,
        "-o",
        str(out),
        "-J",
        "-R",
        "-V",
        volume,
        "-graft-points",
        # keep the source name at the ISO root
        f"{src.name}={src.name}" if src.is_dir() else src.name,
    ]
    parent = str(src.parent)

    def on_line(line: str) -> None:
        ctx.check_cancelled()
        percent = extract_percent(line)
        if percent is not None:
            ctx.progress(percent, f"{percent * 100:.1f}%")
        elif line.strip() and not line.startswith("Genisoimage"):
            ctx.log(line.strip())

    try:
        stream_command(
            cmd,
            on_line=on_line,
            cwd=parent,
            timeout=60 * 60,
            cancel_check=ctx.check_cancelled,
            kill_on_cancel=ctx._job,
        )
    except AgentError:
        out.unlink(missing_ok=True)
        raise

    size = out.stat().st_size if out.exists() else 0
    if size <= 0:
        out.unlink(missing_ok=True)
        raise AgentError("genisoimage produced no output", ERR_TOOL_FAILED)

    ctx.progress(1.0, f"created {out.name}")
    return {
        "source": source,
        "path": f"images/{out.name}",
        "absolute_path": str(out),
        "size": size,
        "name": out.name,
        "label": volume,
    }


def list_images(cfg: AgentConfig) -> list[dict]:
    images: list[dict] = []
    if not cfg.image_dir.exists():
        return images
    for entry in sorted(cfg.image_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in (".iso", ".img"):
            info = entry.stat()
            images.append(
                {
                    "name": entry.name,
                    "path": f"images/{entry.name}",
                    "size": int(info.st_size),
                    "mtime": float(info.st_mtime),
                }
            )
    return images
