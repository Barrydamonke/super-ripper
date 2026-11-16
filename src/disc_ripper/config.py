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
    )
