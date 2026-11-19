"""Find optical drives in sysfs and keep the drives table in step.

Drives are identified by serial number, never by their srX/sgX number, which
can change between boots. The serial comes from SCSI VPD page 0x80, or for SATA
drives from the ATA IDENTIFY data libata exposes in sysfs. If neither has one
the ID falls back to the drive's WWID, then to the SATA port it is plugged into.
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
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
    # The port-based ID this drive would get without a serial. Used to carry a
    # label over from a row created before the serial could be read.
    port_id: str = ""


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


def read_ata_serial(text: str) -> str | None:
    """Serial number from libata's ata_device/devN.M/id (IDENTIFY data as hex words).

    Words 10-19 hold the serial; ATA strings store two characters per word,
    high byte first.
    """
    words = text.split()
    if len(words) < 20:
        return None
    try:
        raw = b"".join(int(w, 16).to_bytes(2, "big") for w in words[10:20])
    except ValueError:
        return None
    serial = raw.decode("ascii", errors="ignore").strip(" \x00\t\r\n")
    return serial if serial and serial.isprintable() else None


def _ata_identify_serial(device: Path) -> str | None:
    """Find the libata device behind a SCSI device and read its serial."""
    parts = Path(os.path.realpath(device)).parts
    port_index = next((i for i, part in enumerate(parts) if _ATA_PORT.match(part)), None)
    if port_index is None:
        return None
    port_dir = Path(*parts[: port_index + 1])
    candidates = sorted(port_dir.glob("link*/dev*/ata_device/dev*/id"))
    if len(candidates) > 1:
        # PATA master/slave share a port: the SCSI target number is the ATA device number.
        hctl = next((part for part in reversed(parts) if _HCTL.match(part)), None)
        if hctl is None:
            return None
        target = hctl.split(":")[2]
        candidates = [c for c in candidates if c.parent.name.rsplit(".", 1)[-1] == target]
        if len(candidates) != 1:
            return None
    if not candidates:
        return None
    return read_ata_serial(_read_text(candidates[0]) or "")


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
    if not serial:
        serial = _ata_identify_serial(device)
    if serial:
        return f"serial:{serial}", "serial"
    wwid = _read_text(device / "wwid")
    if wwid:
        return f"wwid:{wwid}", "wwid"
    return _port_id(device, sg_name, model), "port"


def _port_id(device: Path, sg_name: str, model: str) -> str:
    return f"port:{_port_name(device, sg_name)}:{model}"


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
                    port_id=_port_id(device, sg_dir.name, model),
                ),
            )
        )
    ordered = [d for _, d in sorted(found, key=lambda item: item[0])]
    # Two drives reporting the same serial/WWID (e.g. a factory default) would
    # merge into one record; give those drives port-based IDs instead.
    counts = Counter(d.id for d in ordered)
    return [replace(d, id=d.port_id, id_source="port") if counts[d.id] > 1 else d for d in ordered]


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
            if d.port_id and d.id != d.port_id:
                # The drive used to have a port-based ID (e.g. before its serial
                # could be read): keep that row, label and all, under the new ID.
                conn.execute(
                    """
                    UPDATE drives SET id = ? WHERE id = ?
                    AND NOT EXISTS (SELECT 1 FROM drives WHERE id = ?)
                    """,
                    (d.id, d.port_id, d.id),
                )
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


READ_OFFSET_MIN = -3000
READ_OFFSET_MAX = 3000


def update_drive(
    conn: sqlite3.Connection, drive_id: str, *, label: str, enabled: bool, read_offset: int | None = None
) -> bool:
    """Update a drive's label and enabled flag, and its read offset when given."""
    with conn:
        cur = conn.execute(
            "UPDATE drives SET label = ?, enabled = ?, read_offset = COALESCE(?, read_offset) WHERE id = ?",
            (label.strip()[:LABEL_MAX], 1 if enabled else 0, read_offset, drive_id),
        )
    return cur.rowcount == 1


def set_media(
    conn: sqlite3.Connection,
    drive_id: str,
    state: str,
    disc_type: str | None,
    label: str,
    now: str,
) -> bool:
    """Record a drive's media state. True if anything changed."""
    with conn:
        cur = conn.execute(
            """
            UPDATE drives SET media_state = ?, disc_type = ?, disc_label = ?, media_changed_at = ?
            WHERE id = ? AND NOT (media_state = ? AND disc_type IS ? AND disc_label = ?)
            """,
            (state, disc_type, label, now, drive_id, state, disc_type, label),
        )
    return cur.rowcount == 1
