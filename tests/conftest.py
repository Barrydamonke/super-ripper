from pathlib import Path

import pytest

from disc_ripper.config import AppConfig


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(
        config_dir=tmp_path / "config",
        sysfs_root=tmp_path / "sys",
        dev_root=tmp_path / "dev",
    )
    cfg.sysfs_root.mkdir()
    cfg.dev_root.mkdir()
    return cfg
