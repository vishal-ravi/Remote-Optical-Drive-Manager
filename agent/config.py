"""Agent configuration: data roots, network binding, token and TLS material."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8443
TOKEN_FILENAME = "agent.token"
TOKEN_LEN = 4096  # token file mode bits


def default_data_root() -> Path:
    override = os.environ.get("RODM_DATA_ROOT")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "rodm"


def default_state_dir() -> Path:
    override = os.environ.get("RODM_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(xdg) / "rodm"


@dataclass
class AgentConfig:
    data_root: Path = field(default_factory=default_data_root)
    state_dir: Path = field(default_factory=default_state_dir)
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    token: str = ""
    require_tls: bool = True
    max_iso_bytes: int = 12 * 1024 * 1024 * 1024  # dual-layer DVD upper bound
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #

    @property
    def stage_dir(self) -> Path:
        return self.data_root / "stage"

    @property
    def image_dir(self) -> Path:
        return self.data_root / "images"

    @property
    def mount_root(self) -> Path:
        return self.data_root / "mnt"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def cert_path(self) -> Path:
        return self.data_root / "tls" / "server.crt"

    @property
    def key_path(self) -> Path:
        return self.data_root / "tls" / "server.key"

    @property
    def token_path(self) -> Path:
        return self.data_root / TOKEN_FILENAME

    # ------------------------------------------------------------------ #

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.stage_dir,
            self.image_dir,
            self.mount_root,
            self.log_dir,
            self.cert_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
            _harden_dir(path)

    def load_or_create_token(self) -> str:
        self.data_root.mkdir(parents=True, exist_ok=True)
        path = self.token_path
        env_token = os.environ.get("RODM_TOKEN")
        if env_token:
            self.token = env_token
            return self.token
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                self.token = token
                return token
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        self.token = token
        return token

    def read_token(self) -> str | None:
        if self.token:
            return self.token
        env_token = os.environ.get("RODM_TOKEN")
        if env_token:
            self.token = env_token
            return self.token
        path = self.token_path
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                self.token = token
                return token
        return None


def _harden_dir(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        if mode & stat.S_IROTH:
            os.chmod(path, 0o700)
    except OSError:
        pass


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")
