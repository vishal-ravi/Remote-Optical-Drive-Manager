"""Multi-host connection profiles stored in ``~/.config/rodm/profiles.json``."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_AGENT_CMD = "~/rodm/bin/rodm-agent"


def config_dir() -> Path:
    override = os.environ.get("RODM_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "rodm"


@dataclass
class Profile:
    name: str
    host: str
    user: str = ""
    port: int = 22
    key_path: str = ""
    agent_cmd: str = DEFAULT_AGENT_CMD

    # REST frontend ---------------------------------------------------- #
    rest_enabled: bool = True
    rest_port: int = 8443
    rest_scheme: str = "https"
    rest_token: str = ""
    tls_fingerprint: str = ""
    tls_cert_file: str = ""
    tls_verify: bool = True

    #: absolute data root on the agent ("" = <home>/.local/share/rodm)
    remote_data_root: str = ""

    # remembered state ------------------------------------------------- #
    last_device: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # ------------------------------------------------------------------ #

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def rest_base_url(self) -> str:
        scheme = self.rest_scheme or "https"
        return f"{scheme}://{self.host}:{self.rest_port}"

    @property
    def key_path_expanded(self) -> str:
        return os.path.expanduser(self.key_path) if self.key_path else ""

    @property
    def remote_home(self) -> str:
        if self.user:
            return f"/home/{self.user}"
        return "/home"

    @property
    def agent_data_root(self) -> str:
        return self.remote_data_root or f"{self.remote_home}/.local/share/rodm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Profile":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        if not data.get("name"):
            raise ValueError("profile requires a name")
        if not data.get("host"):
            raise ValueError(f"profile {data.get('name')} requires a host")
        data.setdefault("user", "")
        data.setdefault("port", 22)
        return cls(**data)

    def redacted(self) -> dict[str, Any]:
        data = self.to_dict()
        if data.get("rest_token"):
            data["rest_token"] = "***"
        return data


class ProfileStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else (config_dir() / "profiles.json")

    # ------------------------------------------------------------------ #

    def load(self) -> list[Profile]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read {self.path}: {exc}") from exc
        profiles: list[Profile] = []
        for entry in raw.get("profiles", []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict):
                continue
            try:
                profiles.append(Profile.from_dict(entry))
            except (TypeError, ValueError):
                continue
        return profiles

    def save(self, profiles: list[Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "profiles": [profile.to_dict() for profile in profiles],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover
            pass

    # convenience ------------------------------------------------------ #

    def get(self, profile_id: str) -> Profile:
        for profile in self.load():
            if profile.id == profile_id or profile.name == profile_id:
                return profile
        raise KeyError(f"unknown profile '{profile_id}'")

    def upsert(self, profile: Profile) -> Profile:
        profiles = self.load()
        for index, existing in enumerate(profiles):
            if existing.id == profile.id:
                profiles[index] = profile
                self.save(profiles)
                return profile
        profiles.append(profile)
        self.save(profiles)
        return profile

    def remove(self, profile_id: str) -> bool:
        profiles = self.load()
        remaining = [p for p in profiles if p.id != profile_id and p.name != profile_id]
        if len(remaining) == len(profiles):
            return False
        self.save(remaining)
        return True

    def update_fields(self, profile_id: str, **updates: Any) -> Profile:
        profile = self.get(profile_id)
        for key, value in updates.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        return self.upsert(profile)
