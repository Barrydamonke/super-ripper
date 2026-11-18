"""Talk to one optical drive through its /dev/srX node (Linux CD-ROM ioctls and SG_IO).

Every call opens the node, does its work and closes it again, so the app never
holds a drive open between polls (an open handle makes eject fail with EBUSY
for other programs). O_NONBLOCK lets the open succeed with no disc in.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

# linux/cdrom.h
CDROMEJECT = 0x5309
CDROMCLOSETRAY = 0x5319
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDROM_LOCKDOOR = 0x5329
CDSL_CURRENT = 0x7FFFFFFF
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4
# scsi/sg.h
SG_IO = 0x2285
_SG_DXFER_FROM_DEV = -3
_SECTOR = 2048

MediaState = Literal["unknown", "empty", "tray_open", "not_ready", "disc"]
_STATES: dict[int, MediaState] = {
    CDS_NO_DISC: "empty",
    CDS_TRAY_OPEN: "tray_open",
    CDS_DRIVE_NOT_READY: "not_ready",
    CDS_DISC_OK: "disc",
}


class SgIoHdr(ctypes.Structure):
    """struct sg_io_hdr from <scsi/sg.h>."""

    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


def _default_ioctl(fd: int, request: int, arg=0):
    import fcntl  # Linux only; imported here so the module loads on Windows for tests

    return fcntl.ioctl(fd, request, arg)


class CdDrive:
    def __init__(
        self,
        path: Path,
        *,
        ioctl: Callable | None = None,
        open_fn: Callable[[str, int], int] = os.open,
        close_fn: Callable[[int], None] = os.close,
        pread: Callable[[int, int, int], bytes] | None = None,
    ):
        self.path = path
        self._ioctl = ioctl or _default_ioctl
        self._open = open_fn
        self._close = close_fn
        self._pread = pread or getattr(os, "pread", None)

    @contextmanager
    def _fd(self):
        fd = self._open(str(self.path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            yield fd
        finally:
            self._close(fd)

    def drive_status(self) -> MediaState:
        try:
            with self._fd() as fd:
                code = self._ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
        except OSError:
            return "unknown"
        return _STATES.get(code, "unknown")

    def disc_status(self) -> int:
        with self._fd() as fd:
            return self._ioctl(fd, CDROM_DISC_STATUS, 0)

    def eject(self) -> None:
        with self._fd() as fd:
            try:
                # MakeMKV and friends lock the door; unlocking can fail harmlessly.
                self._ioctl(fd, CDROM_LOCKDOOR, 0)
            except OSError:
                pass
            self._ioctl(fd, CDROMEJECT, 0)

    def close_tray(self) -> None:
        with self._fd() as fd:
            self._ioctl(fd, CDROMCLOSETRAY, 0)

    def read_sector(self, lba: int) -> bytes:
        if self._pread is None:
            raise OSError("pread is not available on this platform")
        with self._fd() as fd:
            data = self._pread(fd, _SECTOR, lba * _SECTOR)
        if len(data) != _SECTOR:
            raise OSError(f"short read at sector {lba}: {len(data)} bytes")
        return data

    def get_configuration(self) -> bytes:
        """GET CONFIGURATION header (8 bytes): the drive's current media profile."""
        cdb = (ctypes.c_ubyte * 10)(0x46, 0x01, 0, 0, 0, 0, 0, 0, 0x08, 0)
        buf = (ctypes.c_ubyte * 8)()
        sense = (ctypes.c_ubyte * 32)()
        hdr = SgIoHdr(
            interface_id=ord("S"),
            dxfer_direction=_SG_DXFER_FROM_DEV,
            cmd_len=10,
            mx_sb_len=32,
            dxfer_len=8,
            dxferp=ctypes.addressof(buf),
            cmdp=ctypes.addressof(cdb),
            sbp=ctypes.addressof(sense),
            timeout=5000,
        )
        with self._fd() as fd:
            self._ioctl(fd, SG_IO, hdr)
        if hdr.status or hdr.host_status or hdr.driver_status:
            raise OSError(
                f"GET CONFIGURATION failed (status {hdr.status}, host {hdr.host_status}, "
                f"driver {hdr.driver_status})"
            )
        return bytes(buf)
