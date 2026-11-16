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


def make_sg(
    sysfs_root: Path,
    n: int,
    *,
    type: int = 5,
    vendor: str = "HL-DT-ST",
    model: str = "BD-RE WH16NS40",
    rev: str = "1.05",
    sr: int | None = 0,
    vpd_serial: bytes | None = None,
    wwid: str | None = None,
    device_dir: Path | None = None,
) -> Path:
    """Build a fake /sys/class/scsi_generic/sg<n> entry. Returns the sg dir.

    device_dir: where the device's files live. When given, sg<n>/device is a
    symlink to it (as on real sysfs); otherwise device/ is a plain directory.
    """
    sg_dir = sysfs_root / "class" / "scsi_generic" / f"sg{n}"
    sg_dir.mkdir(parents=True)
    (sg_dir / "dev").write_text(f"21:{n}\n")
    if device_dir is None:
        device = sg_dir / "device"
        device.mkdir()
    else:
        device_dir.mkdir(parents=True)
        (sg_dir / "device").symlink_to(device_dir, target_is_directory=True)
        device = device_dir
    (device / "type").write_text(f"{type}\n")
    (device / "vendor").write_text(f"{vendor}  \n")
    (device / "model").write_text(f"{model}    \n")
    (device / "rev").write_text(f"{rev}\n")
    if sr is not None:
        block = device / "block" / f"sr{sr}"
        block.mkdir(parents=True)
        (block / "dev").write_text(f"11:{sr}\n")
    if vpd_serial is not None:
        (device / "vpd_pg80").write_bytes(vpd_serial)
    if wwid is not None:
        (device / "wwid").write_text(f"{wwid}\n")
    return sg_dir


@pytest.fixture
def add_sg():
    return make_sg


TEST_PASSWORD = "test-password"


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    from disc_ripper.app import create_app

    with TestClient(create_app(config), follow_redirects=False) as c:
        yield c


@pytest.fixture
def logged_in_client(client):
    r = client.post("/setup", data={"password": TEST_PASSWORD, "confirm": TEST_PASSWORD})
    assert r.status_code == 303
    r = client.post("/login", data={"password": TEST_PASSWORD, "next": "/"})
    assert r.status_code == 303
    return client
