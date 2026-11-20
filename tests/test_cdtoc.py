from pathlib import Path

from disc_ripper import cdrom, cdtoc
from disc_ripper.cdtoc import Toc

# https://musicbrainz.org/doc/Disc_ID_Calculation worked example. The page lists
# offsets with the 150-frame lead-in already added; Toc holds raw LBAs.
EXAMPLE = Toc(
    first=1,
    last=6,
    offsets=tuple(o - 150 for o in (150, 15363, 32314, 46592, 63414, 80489)),
    leadout=95462 - 150,
    data_tracks=frozenset(),
)


def test_disc_id_musicbrainz_example():
    assert cdtoc.musicbrainz_disc_id(EXAMPLE) == "49HHV7Eb8UKF3aQiNmu1GR8vKTY-"


def test_disc_id_enhanced_cd():
    audio = tuple(i * 18000 for i in range(10))
    enhanced = Toc(first=1, last=11, offsets=audio + (200000,), leadout=260000, data_tracks=frozenset({11}))
    plain = Toc(first=1, last=10, offsets=audio, leadout=200000 - 11400, data_tracks=frozenset())
    assert cdtoc.musicbrainz_disc_id(enhanced) == cdtoc.musicbrainz_disc_id(plain)
    assert enhanced.audio_tracks == list(range(1, 11))


def test_toc_json_round_trip():
    toc = Toc(first=1, last=2, offsets=(0, 15000), leadout=30000, data_tracks=frozenset({2}))
    assert Toc.from_json(toc.to_json()) == toc


def test_audio_seconds():
    assert EXAMPLE.audio_seconds == (95462 - 150) / 75
    enhanced = Toc(first=1, last=2, offsets=(0, 30000), leadout=40000, data_tracks=frozenset({2}))
    assert enhanced.audio_seconds == (30000 - 11400) / 75


class FakeToc:
    """Answers CDROMREADTOCHDR / CDROMREADTOCENTRY like the kernel would."""

    def __init__(self, entries: dict[int, tuple[int, int]], first: int, last: int):
        self.entries = entries  # track (0xAA = lead-out) -> (lba, ctrl)
        self.first, self.last = first, last

    def ioctl(self, fd, request, arg=0):
        if request == cdrom.CDROMREADTOCHDR:
            arg.cdth_trk0, arg.cdth_trk1 = self.first, self.last
        elif request == cdrom.CDROMREADTOCENTRY:
            assert arg.cdte_format == cdrom.CDROM_LBA
            lba, ctrl = self.entries[arg.cdte_track]
            arg.cdte_addr.lba = lba
            arg.cdte_adr_ctrl = (ctrl << 4) | 1
        return 0


def _drive(fake):
    return cdrom.CdDrive(Path("/dev/sr0"), ioctl=fake.ioctl, open_fn=lambda p, f: 3, close_fn=lambda fd: None)


def test_read_toc_ioctls():
    fake = FakeToc({1: (0, 0), 2: (15000, 0), 0xAA: (30000, 0)}, 1, 2)
    assert _drive(fake).read_toc() == Toc(
        first=1, last=2, offsets=(0, 15000), leadout=30000, data_tracks=frozenset()
    )


def test_read_toc_marks_data_track():
    fake = FakeToc({1: (0, 0), 2: (30000, 0x4), 0xAA: (40000, 0)}, 1, 2)
    assert _drive(fake).read_toc().data_tracks == frozenset({2})


def test_tocentry_layout():
    import ctypes

    assert ctypes.sizeof(cdrom.CdromTocEntry) == 12
    assert cdrom.CdromTocEntry.cdte_addr.offset == 4
