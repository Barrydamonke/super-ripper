import ctypes
import errno
import struct
from pathlib import Path

import pytest

from disc_ripper import cdrom, discprobe


class FakeDevice:
    def __init__(self, results=None, errors=None):
        self.calls = []
        self.results = results or {}
        self.errors = errors or {}
        self.open_count = 0
        self.closed = 0

    def open(self, path, flags):
        self.open_count += 1
        self.path = path
        return 42

    def close(self, fd):
        assert fd == 42
        self.closed += 1

    def ioctl(self, fd, request, arg=0):
        assert fd == 42
        self.calls.append((request, arg))
        if request in self.errors:
            raise self.errors[request]
        return self.results.get(request, 0)

    def drive(self, **kw):
        return cdrom.CdDrive(Path("/dev/sr0"), ioctl=self.ioctl, open_fn=self.open, close_fn=self.close, **kw)


@pytest.mark.parametrize(
    "code,state",
    [(1, "empty"), (2, "tray_open"), (3, "not_ready"), (4, "disc"), (0, "unknown"), (99, "unknown")],
)
def test_drive_status_mapping(code, state):
    dev = FakeDevice(results={cdrom.CDROM_DRIVE_STATUS: code})
    assert dev.drive().drive_status() == state
    assert dev.calls == [(cdrom.CDROM_DRIVE_STATUS, cdrom.CDSL_CURRENT)]
    assert dev.closed == 1


def test_drive_status_error_is_unknown():
    dev = FakeDevice(errors={cdrom.CDROM_DRIVE_STATUS: OSError(errno.EIO, "Input/output error")})
    assert dev.drive().drive_status() == "unknown"
    assert dev.closed == 1


def test_open_error_is_unknown():
    def no_node(path, flags):
        raise FileNotFoundError(errno.ENOENT, "No such file")

    d = cdrom.CdDrive(Path("/dev/sr9"), ioctl=lambda *a: 0, open_fn=no_node)
    assert d.drive_status() == "unknown"


def test_disc_status():
    dev = FakeDevice(results={cdrom.CDROM_DISC_STATUS: discprobe.CDS_AUDIO})
    assert dev.drive().disc_status() == discprobe.CDS_AUDIO


def test_eject_unlocks_then_ejects():
    dev = FakeDevice(errors={cdrom.CDROM_LOCKDOOR: OSError(errno.EBUSY, "busy")})
    dev.drive().eject()
    assert [c[0] for c in dev.calls] == [cdrom.CDROM_LOCKDOOR, cdrom.CDROMEJECT]
    assert dev.calls[0] == (cdrom.CDROM_LOCKDOOR, 0)


def test_eject_error_propagates_and_closes():
    dev = FakeDevice(errors={cdrom.CDROMEJECT: OSError(errno.EBUSY, "Device or resource busy")})
    with pytest.raises(OSError) as exc:
        dev.drive().eject()
    assert exc.value.errno == errno.EBUSY
    assert dev.closed == 1


def test_close_tray():
    dev = FakeDevice()
    dev.drive().close_tray()
    assert [c[0] for c in dev.calls] == [cdrom.CDROMCLOSETRAY]


def test_read_sector_offset():
    seen = []

    def pread(fd, n, offset):
        seen.append((fd, n, offset))
        return b"x" * n

    dev = FakeDevice()
    assert dev.drive(pread=pread).read_sector(16) == b"x" * 2048
    assert seen == [(42, 2048, 16 * 2048)]


def test_read_sector_short_read_raises():
    dev = FakeDevice()
    with pytest.raises(OSError):
        dev.drive(pread=lambda fd, n, off: b"x" * 10).read_sector(16)


def test_get_configuration_builds_cdb():
    def sg_ioctl(fd, request, hdr):
        assert request == cdrom.SG_IO
        assert hdr.interface_id == ord("S")
        assert hdr.dxfer_direction == -3
        assert hdr.cmd_len == 10
        assert hdr.dxfer_len == 8
        assert ctypes.string_at(hdr.cmdp, 10) == bytes([0x46, 0x01, 0, 0, 0, 0, 0, 0, 0x08, 0])
        ctypes.memmove(hdr.dxferp, b"\x00\x00\x00\x04\x00\x00" + struct.pack(">H", 0x41), 8)
        return 0

    d = cdrom.CdDrive(Path("/dev/sr0"), ioctl=sg_ioctl, open_fn=lambda p, f: 42, close_fn=lambda fd: None)
    assert discprobe.media_kind(d.get_configuration()) == "bd"


def test_get_configuration_scsi_error_raises():
    def sg_ioctl(fd, request, hdr):
        hdr.status = 2  # CHECK CONDITION
        return 0

    d = cdrom.CdDrive(Path("/dev/sr0"), ioctl=sg_ioctl, open_fn=lambda p, f: 42, close_fn=lambda fd: None)
    with pytest.raises(OSError):
        d.get_configuration()
