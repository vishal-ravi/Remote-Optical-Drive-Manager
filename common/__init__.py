"""Shared protocol, validation and logging helpers for Remote Optical Drive Manager."""

from .logging_setup import format_bytes, setup_logging
from .protocol import (
    AgentError,
    DirEntry,
    DriveInfo,
    JobEvent,
    JobSnapshot,
    ProtocolError,
    make_error,
    make_request,
    make_result,
)
from .validation import validate_device

__all__ = [
    "AgentError",
    "DirEntry",
    "DriveInfo",
    "JobEvent",
    "JobSnapshot",
    "ProtocolError",
    "format_bytes",
    "make_error",
    "make_request",
    "make_result",
    "setup_logging",
    "validate_device",
]
