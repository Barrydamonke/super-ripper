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


def test_node_status_open_denied(config, monkeypatch):
    (config.dev_root / "sg1").write_text("")

    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(drives.os, "open", deny)
    assert drives.node_status(config.dev_root, "sg1") == "no-access"


def test_node_status_no_device_behind_node(config, monkeypatch):
    import errno

    (config.dev_root / "sg1").write_text("")

    def nxio(*a, **k):
        raise OSError(errno.ENXIO, "No such device or address")

    monkeypatch.setattr(drives.os, "open", nxio)
    assert drives.node_status(config.dev_root, "sg1") == "missing"


def test_duplicate_serials_get_distinct_ids(config, add_sg):
    # Cheap drives can report the same factory-default serial.
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x040000")
    add_sg(config.sysfs_root, 2, sr=1, vpd_serial=b"\x05\x80\x00\x040000")
    found = drives.discover(config.sysfs_root)
    assert len({d.id for d in found}) == 2
    assert all(d.id_source == "port" for d in found)


# IDENTIFY data from the user's drives (/sys/class/ata_device/devN.0/id, first 100 words).
BH10LS38_ID = """\
85c0 0000 0000 0000 0000 0000 0000 0000
0000 0000 4b39 4543 4152 4234 3731 3920
2020 2020 2020 2020 0000 0000 0000 312e
3033 2020 2020 484c 2d44 542d 5354 2042
"""
IHAS124_ID = """\
85c0 0000 0000 0000 0000 0000 0000 0000
0000 0000 3335 3234 3732 3820 324d 3837
3531 3530 3232 3236 0000 0000 0000 434c
"""
EMPTY_ID = "0000 " * 256


def test_read_ata_serial():
    assert drives.read_ata_serial(BH10LS38_ID) == "K9ECARB4719"
    assert drives.read_ata_serial(IHAS124_ID) == "3524728 2M8751502226"
    assert drives.read_ata_serial(EMPTY_ID) is None
    assert drives.read_ata_serial("") is None
    assert drives.read_ata_serial("zzzz " * 20) is None


def _ata_drive(sysfs_root, add_sg, n, port, ident, *, target=0, devno=0):
    port_dir = sysfs_root / "devices" / "pci0000:00" / f"ata{port}"
    device = port_dir / f"host{port}" / f"target{port}:0:{target}" / f"{port}:0:{target}:0"
    add_sg(sysfs_root, n, sr=n, device_dir=device)
    id_dir = port_dir / f"link{port}" / f"dev{port}.{devno}" / "ata_device" / f"dev{port}.{devno}"
    id_dir.mkdir(parents=True)
    (id_dir / "id").write_text(ident)


needs_linux_sysfs = pytest.mark.skipif(
    os.name == "nt", reason="needs symlinks and ':' in names (Linux sysfs layout)"
)


@needs_linux_sysfs
def test_serial_from_ata_identify(config, add_sg):
    _ata_drive(config.sysfs_root, add_sg, 1, 1, BH10LS38_ID)
    _ata_drive(config.sysfs_root, add_sg, 3, 3, IHAS124_ID)
    found = drives.discover(config.sysfs_root)
    assert [(d.id, d.id_source) for d in found] == [
        ("serial:K9ECARB4719", "serial"),
        ("serial:3524728 2M8751502226", "serial"),
    ]
    assert found[0].port_id == "port:ata1:BD-RE WH16NS40"


@needs_linux_sysfs
def test_blank_ata_identify_falls_back_to_port(config, add_sg):
    _ata_drive(config.sysfs_root, add_sg, 1, 2, EMPTY_ID)
    [d] = drives.discover(config.sysfs_root)
    assert d.id == "port:ata2:BD-RE WH16NS40"


@needs_linux_sysfs
def test_ata_identify_picks_device_by_target(config, add_sg):
    # PATA master/slave: two ATA devices on one port; the SCSI target number picks one.
    _ata_drive(config.sysfs_root, add_sg, 1, 1, BH10LS38_ID, target=1, devno=1)
    id_dir = config.sysfs_root / "devices" / "pci0000:00" / "ata1" / "link1" / "dev1.0" / "ata_device" / "dev1.0"
    id_dir.mkdir(parents=True)
    (id_dir / "id").write_text(IHAS124_ID)
    [d] = drives.discover(config.sysfs_root)
    assert d.id == "serial:K9ECARB4719"


@needs_linux_sysfs
def test_vpd_serial_wins_over_ata_identify(config, add_sg):
    _ata_drive(config.sysfs_root, add_sg, 1, 1, BH10LS38_ID)
    vpd = config.sysfs_root / "class" / "scsi_generic" / "sg1" / "device" / "vpd_pg80"
    vpd.write_bytes(SERIAL)
    [d] = drives.discover(config.sysfs_root)
    assert d.id == "serial:ABC123"


def _found(drive_id, port_id, source="serial"):
    return drives.DiscoveredDrive(
        id=drive_id, id_source=source, vendor="HL-DT-ST", model="BD-RE BH10LS38", revision="1.03",
        sg_name="sg1", sr_name="sr0", sg_dev=(21, 1), sr_dev=(11, 0), port_id=port_id,
    )


def test_sync_moves_label_from_port_id_to_serial(conn):
    port_id = "port:ata1:BD-RE BH10LS38"
    drives.sync(conn, [_found(port_id, port_id, "port")], "t1")
    drives.update_drive(conn, port_id, label="Blu-ray", enabled=False)

    drives.sync(conn, [_found("serial:K9ECARB4719", port_id)], "t2")
    [row] = drives.list_drives(conn)
    assert row["id"] == "serial:K9ECARB4719"
    assert row["id_source"] == "serial"
    assert row["label"] == "Blu-ray"
    assert row["enabled"] == 0
    assert row["first_seen"] == "t1"
    assert row["present"] == 1


def test_sync_does_not_merge_into_existing_serial_row(conn):
    port_id = "port:ata1:BD-RE BH10LS38"
    drives.sync(conn, [_found("serial:K9ECARB4719", port_id)], "t1")
    drives.update_drive(conn, "serial:K9ECARB4719", label="Blu-ray", enabled=True)
    # a stale port row for the same port (e.g. a different drive used to be there)
    drives.sync(conn, [_found(port_id, port_id, "port")], "t2")
    drives.update_drive(conn, port_id, label="Old", enabled=True)

    drives.sync(conn, [_found("serial:K9ECARB4719", port_id)], "t3")
    rows = {r["id"]: r for r in drives.list_drives(conn)}
    assert rows["serial:K9ECARB4719"]["label"] == "Blu-ray"
    assert rows[port_id]["label"] == "Old"
    assert rows[port_id]["present"] == 0


def test_set_media_reports_change(config, conn, add_sg):
    add_sg(config.sysfs_root, 1, vpd_serial=SERIAL)
    drives.sync(conn, drives.discover(config.sysfs_root), "t0")
    assert drives.set_media(conn, "serial:ABC123", "disc", "dvd", "MOVIE", "t1") is True
    assert drives.set_media(conn, "serial:ABC123", "disc", "dvd", "MOVIE", "t2") is False
    [row] = drives.list_drives(conn)
    assert row["media_changed_at"] == "t1"
    assert drives.set_media(conn, "serial:ABC123", "disc", "dvd", "OTHER", "t3") is True
    [row] = drives.list_drives(conn)
    assert (row["media_state"], row["disc_type"], row["disc_label"], row["media_changed_at"]) == (
        "disc", "dvd", "OTHER", "t3",
    )
    assert drives.set_media(conn, "serial:nope", "empty", None, "", "t4") is False
