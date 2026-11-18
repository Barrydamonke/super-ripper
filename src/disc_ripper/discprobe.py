"""Work out what kind of disc is in a drive, and its volume label.

Everything here is a parser over bytes the drive returns, so it is tested on
fabricated sectors. The drive itself is reached through a ProbeTarget
(cdrom.CdDrive in production).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from typing import Literal, Protocol

log = logging.getLogger(__name__)

DiscType = Literal["audio", "dvd", "bluray", "data", "mixed", "unknown"]
MediaKind = Literal["cd", "dvd", "bd", "unknown"]

# CDROM_DISC_STATUS results (linux/cdrom.h)
CDS_AUDIO = 100
CDS_DATA_1 = 101
CDS_DATA_2 = 102
CDS_XA_2_1 = 103
CDS_XA_2_2 = 104
CDS_MIXED = 105

SECTOR = 2048
LABEL_MAX = 64
_ROOT_DIR_MAX_SECTORS = 16
_UDF_VDS_MAX_SECTORS = 64


class ProbeTarget(Protocol):
    def disc_status(self) -> int: ...
    def get_configuration(self) -> bytes: ...
    def read_sector(self, lba: int) -> bytes: ...


def clean_label(raw: bytes | str) -> str:
    """Disc labels are untrusted: keep printable ASCII only, trimmed and capped."""
    text = raw.decode("ascii", errors="ignore") if isinstance(raw, bytes) else raw
    text = "".join(c for c in text if c.isascii() and c.isprintable())
    return text.strip()[:LABEL_MAX].strip()


def iso9660_pvd(sector16: bytes) -> tuple[str, int, int] | None:
    """(label, root directory LBA, root directory length) from the primary volume descriptor."""
    if len(sector16) < 190 or sector16[0] != 1 or sector16[1:6] != b"CD001":
        return None
    root = sector16[156:190]
    lba = struct.unpack_from("<I", root, 2)[0]
    length = struct.unpack_from("<I", root, 10)[0]
    return clean_label(sector16[40:72]), lba, length


def directory_names(data: bytes) -> list[str]:
    """File and folder names in an ISO 9660 directory extent (upper-case, no ';1')."""
    names = []
    pos = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            # Records never cross a sector boundary; the rest of this sector is padding.
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        if length < 34 or pos + length > len(data):
            break
        name_len = data[pos + 32]
        name = data[pos + 33 : pos + 33 + name_len]
        if name not in (b"\x00", b"\x01"):
            names.append(name.decode("ascii", errors="ignore").split(";")[0].upper())
        pos += length
    return names


def _dstring(field: bytes) -> str:
    used = field[-1]
    if used < 2 or used > len(field) - 1:
        return ""
    compression, body = field[0], field[1:used]
    if compression == 8:
        return body.decode("latin-1")
    if compression == 16:
        return body.decode("utf-16-be", errors="ignore")
    return ""


def udf_label(read_sector: Callable[[int], bytes]) -> str | None:
    """The UDF logical volume identifier (the label on UDF-only discs such as Blu-ray)."""
    anchor = read_sector(256)
    if len(anchor) < 24 or struct.unpack_from("<H", anchor, 0)[0] != 2:
        return None
    length, location = struct.unpack_from("<II", anchor, 16)
    for i in range(min(length // SECTOR, _UDF_VDS_MAX_SECTORS)):
        descriptor = read_sector(location + i)
        tag = struct.unpack_from("<H", descriptor, 0)[0]
        if tag == 6:
            return clean_label(_dstring(descriptor[84:212])) or None
        if tag == 8:
            break
    return None


def media_kind(config_header: bytes) -> MediaKind:
    """Physical media from a GET CONFIGURATION header's current profile."""
    if len(config_header) < 8:
        return "unknown"
    profile = struct.unpack_from(">H", config_header, 6)[0]
    if 0x08 <= profile <= 0x0A:
        return "cd"
    if 0x10 <= profile <= 0x2B:
        return "dvd"
    if 0x40 <= profile <= 0x43:
        return "bd"
    return "unknown"


def _has_video_ts(target: ProbeTarget, root_lba: int, root_len: int) -> bool:
    sectors = min(max(root_len, SECTOR) // SECTOR, _ROOT_DIR_MAX_SECTORS)
    data = b"".join(target.read_sector(root_lba + i) for i in range(sectors))
    return "VIDEO_TS" in directory_names(data)


def probe(target: ProbeTarget) -> tuple[DiscType, str]:
    """(disc type, volume label). Never raises; unknown parts degrade to 'unknown' / ''."""
    try:
        status = target.disc_status()
    except OSError as exc:
        log.warning("disc status failed: %s", exc)
        return "unknown", ""
    if status == CDS_AUDIO:
        return "audio", ""
    if status == CDS_MIXED:
        return "mixed", ""

    try:
        media = media_kind(target.get_configuration())
    except OSError as exc:
        log.info("GET CONFIGURATION failed, judging from the filesystem: %s", exc)
        media = "unknown"

    label = ""
    video_ts = False
    iso = None
    try:
        iso = iso9660_pvd(target.read_sector(16))
        if iso is not None:
            label = iso[0]
            video_ts = _has_video_ts(target, iso[1], iso[2])
    except (OSError, struct.error) as exc:
        log.info("ISO 9660 read failed: %s", exc)
    if not label:
        try:
            label = udf_label(target.read_sector) or ""
        except (OSError, struct.error) as exc:
            log.info("UDF read failed: %s", exc)

    if media == "bd":
        return "bluray", label
    if media == "dvd":
        return ("dvd" if video_ts else "data"), label
    if media == "cd":
        return "data", label
    if iso is not None:
        return ("dvd" if video_ts else "data"), label
    return "unknown", label
