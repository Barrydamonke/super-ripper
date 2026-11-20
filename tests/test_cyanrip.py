from pathlib import Path

from disc_ripper import cyanrip

FIXTURES = Path(__file__).parent / "fixtures" / "cyanrip"


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8").split("\n", 1)[1]


def test_build_argv_exact():
    argv = cyanrip.build_argv(device=Path("/dev/sr0"), offset=667, retries=10, mode="secure")
    assert argv[1:] == [
        "-d", "/dev/sr0", "-s", "667", "-r", "10", "-P", "3", "-N", "-U", "-G",
        "-o", "flac", "-D", "rip", "-F", "{track}", "-L", "log",
    ]
    assert cyanrip.build_argv(device=Path("/dev/sr1"), offset=-6, retries=0, mode="fast")[7:9] == ["-P", "0"]


def test_command_prefix(monkeypatch):
    monkeypatch.delenv("CYANRIP_BIN", raising=False)
    assert cyanrip.command_prefix() == ["cyanrip"]
    monkeypatch.setenv("CYANRIP_BIN", '["python", "fake.py"]')
    assert cyanrip.command_prefix() == ["python", "fake.py"]
    monkeypatch.setenv("CYANRIP_BIN", "/opt/cyanrip")
    assert cyanrip.command_prefix() == ["/opt/cyanrip"]


def test_split_lines_handles_carriage_returns():
    buf = cyanrip.LineSplitter()
    assert buf.feed("a\rb") == ["a"]
    assert buf.feed("c\nd\r\ne") == ["bc", "d"]
    assert buf.flush() == ["e"]


def test_progress_parser_fixture():
    parser = cyanrip.ProgressParser()
    splitter = cyanrip.LineSplitter()
    seen = [r for line in splitter.feed(_fixture("stdout.txt")) if (r := parser.feed(line))]
    assert seen[0] == (1, 0.125)
    assert (2, 0.5) in seen
    assert seen[-1] == (3, 1.0)
    tracks = [t for t, _ in seen]
    assert tracks == sorted(tracks)


def test_progress_parser_ignores_junk():
    parser = cyanrip.ProgressParser()
    assert parser.feed("cyanrip 0.9.3 (release)") is None
    assert parser.feed("Ripping and encoding track x, progress - nan%") is None
    assert parser.feed("") is None
    assert parser.feed("Ripping track 4, progress - 33.00%") == (4, 0.33)


def test_parse_accuraterip_fixture():
    checks = cyanrip.parse_accuraterip(_fixture("log.txt"), [1, 2, 3])
    assert [(c.track, c.status) for c in checks] == [(1, "verified"), (2, "mismatch"), (3, "verified")]
    assert checks[0].confidence == 40


def test_parse_accuraterip_not_in_database_and_missing():
    log = (
        "Track 1 ripped and encoded successfully!\n"
        "  Accurip:       not found\n"
        "    Accurip v1:  00000001\n"
    )
    checks = cyanrip.parse_accuraterip(log, [1, 2])
    assert [(c.track, c.status) for c in checks] == [(1, "not_found"), (2, "unknown")]


def test_track_files(tmp_path):
    for name in ("1.flac", "2.flac", "10.flac", "log.log"):
        (tmp_path / name).write_bytes(b"")
    assert cyanrip.track_files(tmp_path) == {1: tmp_path / "1.flac", 2: tmp_path / "2.flac", 10: tmp_path / "10.flac"}
