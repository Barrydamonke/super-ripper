import struct

from disc_ripper import discprobe

SECTOR = 2048


def pvd(label: bytes, root_lba: int = 20, root_len: int = SECTOR) -> bytes:
    data = bytearray(SECTOR)
    data[0] = 1
    data[1:6] = b"CD001"
    data[6] = 1
    data[40:72] = label.ljust(32, b" ")[:32]
    data[156] = 34
    data[158:162] = struct.pack("<I", root_lba)
    data[162:166] = struct.pack(">I", root_lba)
    data[166:170] = struct.pack("<I", root_len)
    data[170:174] = struct.pack(">I", root_len)
    return bytes(data)


def _record(name: bytes) -> bytes:
    length = 33 + len(name)
    length += length % 2
    rec = bytearray(length)
    rec[0] = length
    rec[32] = len(name)
    rec[33 : 33 + len(name)] = name
    return bytes(rec)


def dir_sector(names: list[str]) -> bytes:
    data = _record(b"\x00") + _record(b"\x01") + b"".join(_record(n.encode()) for n in names)
    return data.ljust(SECTOR, b"\x00")


def _tag(tag_id: int) -> bytearray:
    data = bytearray(SECTOR)
    data[0:2] = struct.pack("<H", tag_id)
    return data


def udf_disc(label: str, utf16: bool = False) -> dict[int, bytes]:
    anchor = _tag(2)
    anchor[16:20] = struct.pack("<I", 4 * SECTOR)
    anchor[20:24] = struct.pack("<I", 32)
    lvd = _tag(6)
    if utf16:
        encoded = bytes([16]) + label.encode("utf-16-be")
    else:
        encoded = bytes([8]) + label.encode("ascii")
    lvd[84 : 84 + len(encoded)] = encoded
    lvd[84 + 127] = len(encoded)
    return {256: bytes(anchor), 32: bytes(_tag(1)), 33: bytes(lvd), 34: bytes(_tag(8)), 35: bytes(_tag(0))}


def config_header(profile: int) -> bytes:
    return b"\x00\x00\x00\x04\x00\x00" + struct.pack(">H", profile)


class FakeTarget:
    def __init__(self, status=discprobe.CDS_DATA_1, profile=None, sectors=None, config_error=False, status_error=False):
        self.status = status
        self.profile = profile
        self.sectors = sectors or {}
        self.config_error = config_error
        self.status_error = status_error

    def disc_status(self):
        if self.status_error:
            raise OSError(5, "Input/output error")
        return self.status

    def get_configuration(self):
        if self.config_error or self.profile is None:
            raise OSError(5, "Input/output error")
        return config_header(self.profile)

    def read_sector(self, lba):
        if lba not in self.sectors:
            return bytes(SECTOR)
        return self.sectors[lba]


def test_iso9660_label_sanitised():
    label, lba, length = discprobe.iso9660_pvd(pvd(b"<script>\x00\xff MOVIE  "))
    assert label == "<script> MOVIE"
    assert (lba, length) == (20, SECTOR)
    assert len(discprobe.clean_label("x" * 70)) == 64
    assert discprobe.iso9660_pvd(bytes(SECTOR)) is None


def test_directory_names():
    names = discprobe.directory_names(dir_sector(["VIDEO_TS", "AUDIO_TS;1", "readme.txt;1"]))
    assert names == ["VIDEO_TS", "AUDIO_TS", "README.TXT"]


def test_directory_names_stops_on_garbage():
    assert discprobe.directory_names(b"\xff" * 10) == []


def test_udf_label():
    sectors = udf_disc("THE_MATRIX")
    assert discprobe.udf_label(lambda lba: sectors.get(lba, bytes(SECTOR))) == "THE_MATRIX"
    sectors = udf_disc("THE_MATRIX", utf16=True)
    assert discprobe.udf_label(lambda lba: sectors.get(lba, bytes(SECTOR))) == "THE_MATRIX"
    assert discprobe.udf_label(lambda lba: bytes(SECTOR)) is None


def test_media_kind():
    assert discprobe.media_kind(config_header(0x08)) == "cd"
    assert discprobe.media_kind(config_header(0x10)) == "dvd"
    assert discprobe.media_kind(config_header(0x2B)) == "dvd"
    assert discprobe.media_kind(config_header(0x41)) == "bd"
    assert discprobe.media_kind(config_header(0x50)) == "unknown"
    assert discprobe.media_kind(b"\x00\x00") == "unknown"


def test_probe_audio_cd():
    assert discprobe.probe(FakeTarget(status=discprobe.CDS_AUDIO, profile=0x08)) == ("audio", "")


def test_probe_mixed():
    assert discprobe.probe(FakeTarget(status=discprobe.CDS_MIXED, profile=0x08)) == ("mixed", "")


def test_probe_dvd_video():
    sectors = {16: pvd(b"MOVIE"), 20: dir_sector(["AUDIO_TS", "VIDEO_TS"])}
    assert discprobe.probe(FakeTarget(profile=0x10, sectors=sectors)) == ("dvd", "MOVIE")


def test_probe_data_dvd():
    sectors = {16: pvd(b"BACKUP"), 20: dir_sector(["PHOTOS"])}
    assert discprobe.probe(FakeTarget(profile=0x10, sectors=sectors)) == ("data", "BACKUP")


def test_probe_data_cd():
    sectors = {16: pvd(b"DRIVERS"), 20: dir_sector(["SETUP.EXE"])}
    assert discprobe.probe(FakeTarget(profile=0x08, sectors=sectors)) == ("data", "DRIVERS")


def test_probe_bluray_udf_label():
    assert discprobe.probe(FakeTarget(profile=0x40, sectors=udf_disc("THE_MATRIX"))) == ("bluray", "THE_MATRIX")


def test_probe_config_fails_uses_filesystem():
    sectors = {16: pvd(b"MOVIE"), 20: dir_sector(["VIDEO_TS"])}
    assert discprobe.probe(FakeTarget(config_error=True, sectors=sectors)) == ("dvd", "MOVIE")


def test_probe_config_fails_no_iso9660_is_unknown():
    assert discprobe.probe(FakeTarget(config_error=True, sectors=udf_disc("X"))) == ("unknown", "X")


def test_probe_status_fails():
    assert discprobe.probe(FakeTarget(status_error=True)) == ("unknown", "")


def test_probe_unreadable_sectors_still_classifies():
    class Broken(FakeTarget):
        def read_sector(self, lba):
            raise OSError(5, "Input/output error")

    assert discprobe.probe(Broken(profile=0x41)) == ("bluray", "")
    assert discprobe.probe(Broken(profile=0x10)) == ("data", "")
