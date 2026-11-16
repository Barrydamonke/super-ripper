import os

import pytest

from disc_ripper import db, drives

SERIAL = b"\x05\x80\x00\x08ABC123  "


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def test_discovers_optical_only(config, add_sg):
    add_sg(config.sysfs_root, 0, type=0, model="WDC WD40", sr=None)
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    found = drives.discover(config.sysfs_root)
    assert len(found) == 1
    d = found[0]
    assert d.id == "serial:ABC123"
    assert d.id_source == "serial"
    assert d.vendor == "HL-DT-ST"
    assert d.model == "BD-RE WH16NS40"
    assert d.revision == "1.05"
    assert d.sg_name == "sg1"
    assert d.sr_name == "sr0"
    assert d.sg_dev == (21, 1)
    assert d.sr_dev == (11, 0)


def test_discover_with_no_sysfs_class(config):
    assert drives.discover(config.sysfs_root) == []


def test_discover_sorted_by_sg_number(config, add_sg):
    add_sg(config.sysfs_root, 10, sr=1, vpd_serial=b"\x05\x80\x00\x01B")
    add_sg(config.sysfs_root, 2, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    assert [d.sg_name for d in drives.discover(config.sysfs_root)] == ["sg2", "sg10"]


def test_read_vpd_serial():
    assert drives.read_vpd_serial(SERIAL) == "ABC123"
    assert drives.read_vpd_serial(b"\x05\x80") is None
    assert drives.read_vpd_serial(b"\x05\x83\x00\x03ABC") is None
    assert drives.read_vpd_serial(b"\x05\x80\x00\x04\x00\x00  ") is None
    # length field longer than the data: use what is there
    assert drives.read_vpd_serial(b"\x05\x80\x00\x20XYZ") == "XYZ"


def test_falls_back_when_vpd_truncated(config, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=b"\x05\x80", wwid="naa.5000")
    [d] = drives.discover(config.sysfs_root)
    assert d.id == "wwid:naa.5000"
    assert d.id_source == "wwid"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks and ':' in names (Linux sysfs layout)")
def test_port_fallback_id(config, add_sg):
    target = config.sysfs_root / "devices" / "pci0000:00" / "ata3" / "host2" / "target2:0:0" / "2:0:0:0"
    add_sg(config.sysfs_root, 1, device_dir=target)
    [d] = drives.discover(config.sysfs_root)
    assert d.id == "port:ata3:BD-RE WH16NS40"
    assert d.id_source == "port"


def test_port_fallback_without_ata_path(config, add_sg):
    add_sg(config.sysfs_root, 4)
    [d] = drives.discover(config.sysfs_root)
    assert d.id == "port:sg4:BD-RE WH16NS40"


def test_drive_without_sr(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=None, vpd_serial=SERIAL)
    [d] = drives.discover(config.sysfs_root)
    assert d.sr_name is None
    assert d.sr_dev is None


def test_sync_keeps_label_when_numbers_change(config, conn, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=SERIAL)
    drives.sync(conn, drives.discover(config.sysfs_root), "2026-11-16T10:00:00")
    assert drives.update_drive(conn, "serial:ABC123", label="Blu-ray", enabled=False) is True

    other = config.sysfs_root.parent / "sys2"
    add_sg(other, 3, sr=1, vpd_serial=SERIAL)
    drives.sync(conn, drives.discover(other), "2026-11-17T10:00:00")

    [row] = drives.list_drives(conn)
    assert row["sg_name"] == "sg3"
    assert row["sr_name"] == "sr1"
    assert row["label"] == "Blu-ray"
    assert row["enabled"] == 0
    assert row["present"] == 1
    assert row["first_seen"] == "2026-11-16T10:00:00"
    assert row["last_seen"] == "2026-11-17T10:00:00"


def test_sync_marks_absent(config, conn, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    drives.sync(conn, drives.discover(config.sysfs_root), "t1")
    drives.sync(conn, [], "t2")
    [row] = drives.list_drives(conn)
    assert row["present"] == 0
    assert row["last_seen"] == "t1"


def test_update_drive(conn):
    assert drives.update_drive(conn, "serial:nope", label="x", enabled=True) is False


def test_update_drive_trims_label(config, conn, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    drives.sync(conn, drives.discover(config.sysfs_root), "t1")
    drives.update_drive(conn, "serial:ABC123", label="  " + "x" * 50 + " ", enabled=True)
    [row] = drives.list_drives(conn)
    assert row["label"] == "x" * 40


def test_node_status(config):
    assert drives.node_status(config.dev_root, None) == "missing"
    assert drives.node_status(config.dev_root, "sr0") == "missing"
    (config.dev_root / "sr0").write_text("")
    assert drives.node_status(config.dev_root, "sr0") == "ok"
