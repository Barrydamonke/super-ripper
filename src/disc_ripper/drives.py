"""Find optical drives in sysfs and keep the drives table in step.

Drives are identified by serial number, never by their srX/sgX number, which
can change between boots. If the serial can't be read the ID falls back to the
drive's WWID, then to the SATA port it is plugged into.
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCSI_TYPE_ROM = 5
LABEL_MAX = 40
_SG_NAME = re.compile(r"^sg(\d+)$")
_ATA_PORT = re.compile(r"^ata\d+$")
_HCTL = re.compile(r"^\d+:\d+:\d+:\d+$")

NodeStatus = Literal["ok", "missing", "no-access"]


@dataclass(frozen=True)
class DiscoveredDrive:
    id: str
    id_source: Literal["serial", "wwid", "port"]
    vendor: str
    model: str
    revision: str
    sg_name: str
    sr_name: str | None
    sg_dev: tuple[int, int]
    sr_dev: tuple[int, int] | None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _parse_dev(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    major, sep, minor = text.partition(":")
    if not sep or not major.isdigit() or not minor.isdigit():
        return None
    return int(major), int(minor)


def read_vpd_serial(data: bytes) -> str | None:
    """Serial number from a SCSI VPD page 0x80 (Unit Serial Number)."""
    if len(data) < 4 or data[1] != 0x80:
        return None
    length = int.from_bytes(data[2:4], "big")
    serial = data[4 : 4 + length].decode("ascii", errors="ignore")
    serial = serial.strip(" \x00\t\r\n")
    return serial if serial and serial.isprintable() else None


def _port_name(device: Path, sg_name: str) -> str:
    parts = Path(os.path.realpath(device)).parts
    for part in parts:
        if _ATA_PORT.match(part):
            return part
    for part in reversed(parts):
        if _HCTL.match(part):
            return part
    return sg_name


def _identify(device: Path, sg_name: str, model: str) -> tuple[str, Literal["serial", "wwid", "port"]]:
    try:
        serial = read_vpd_serial((device / "vpd_pg80").read_bytes())
    except OSError:
        serial = None
    if serial:
        return f"serial:{serial}", "serial"
    wwid = _read_text(device / "wwid")
    if wwid:
        return f"wwid:{wwid}", "wwid"
    return f"port:{_port_name(device, sg_name)}:{model}", "port"


def _find_sr(device: Path) -> tuple[str | None, tuple[int, int] | None]:
    block = device / "block"
    try:
        names = sorted(p.name for p in block.iterdir() if p.name.startswith("sr"))
    except OSError:
        return None, None
    for name in names:
        dev = _parse_dev(_read_text(block / name / "dev"))
        if dev:
            return name, dev
    return None, None


def discover(sysfs_root: Path) -> list[DiscoveredDrive]:
    class_dir = sysfs_root / "class" / "scsi_generic"
    try:
        entries = list(class_dir.iterdir())
    except OSError:
        return []
    found: list[tuple[int, DiscoveredDrive]] = []
    for sg_dir in entries:
        match = _SG_NAME.match(sg_dir.name)
        if not match:
            continue
        device = sg_dir / "device"
        if _read_text(device / "type") != str(SCSI_TYPE_ROM):
            continue
        sg_dev = _parse_dev(_read_text(sg_dir / "dev"))
        if sg_dev is None:
            continue
        model = _read_text(device / "model") or ""
        drive_id, id_source = _identify(device, sg_dir.name, model)
        sr_name, sr_dev = _find_sr(device)
        found.append(
            (
                int(match.group(1)),
                DiscoveredDrive(
                    id=drive_id,
                    id_source=id_source,
                    vendor=_read_text(device / "vendor") or "",
                    model=model,
                    revision=_read_text(device / "rev") or "",
                    sg_name=sg_dir.name,
                    sr_name=sr_name,
                    sg_dev=sg_dev,
                    sr_dev=sr_dev,
                ),
            )
        )
    return [d for _, d in sorted(found, key=lambda item: item[0])]


def node_status(dev_root: Path, name: str | None) -> NodeStatus:
    if not name:
        return "missing"
    path = dev_root / name
    if not path.exists():
        return "missing"
    if not os.access(path, os.R_OK | os.W_OK):
        return "no-access"
    # Opening the node also catches a missing --device-cgroup-rule (EPERM), which
    # file permissions alone don't show. O_NONBLOCK: don't wait for a disc.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except PermissionError:
        return "no-access"
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENODEV):
            return "missing"
        return "ok"  # e.g. no medium: the node itself is usable
    os.close(fd)
    return "ok"


def sync(conn: sqlite3.Connection, found: list[DiscoveredDrive], now: str) -> None:
    with conn:
        conn.execute("UPDATE drives SET present = 0")
        for d in found:
            conn.execute(
                """
                INSERT INTO drives (id, id_source, vendor, model, revision, sg_name, sr_name,
                                    present, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    id_source = excluded.id_source, vendor = excluded.vendor,
                    model = excluded.model, revision = excluded.revision,
                    sg_name = excluded.sg_name, sr_name = excluded.sr_name,
                    present = 1, last_seen = excluded.last_seen
                """,
                (d.id, d.id_source, d.vendor, d.model, d.revision, d.sg_name, d.sr_name, now, now),
            )


def list_drives(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM drives ORDER BY present DESC, length(sg_name), sg_name, id"
    ).fetchall()


def update_drive(conn: sqlite3.Connection, drive_id: str, *, label: str, enabled: bool) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE drives SET label = ?, enabled = ? WHERE id = ?",
            (label.strip()[:LABEL_MAX], 1 if enabled else 0, drive_id),
        )
    return cur.rowcount == 1
