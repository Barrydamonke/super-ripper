"""Runtime configuration read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    config_dir: Path
    sysfs_root: Path
    dev_root: Path
    work_dir: Path = Path("/work")
    # Shared with unraid-backend for GET /api/busy; blank disables the endpoint.
    busy_secret: str = ""
    # Drive poller, job workers and schedule refresher. Tests turn them off.
    run_background: bool = True

    @property
    def db_path(self) -> Path:
        return self.config_dir / "disc-ripper.db"

    @property
    def secret_key_path(self) -> Path:
        return self.config_dir / "secret_key"


def load_config(env: Mapping[str, str] = os.environ) -> AppConfig:
    return AppConfig(
        config_dir=Path(env.get("CONFIG_DIR", "/config")),
        sysfs_root=Path(env.get("SYSFS_ROOT", "/sys")),
        dev_root=Path(env.get("DEV_ROOT", "/dev")),
        work_dir=Path(env.get("WORK_DIR", "/work")),
        # Trimmed: a pasted value can pick up a trailing space or newline, which an
        # HTTP header can't carry, so it could never match.
        busy_secret=env.get("BUSY_SECRET", "").strip(),
    )
