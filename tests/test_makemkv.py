from pathlib import Path

from disc_ripper import makemkv

FIXTURES = Path(__file__).parent / "fixtures" / "makemkv"


def _scan(name):
    parser = makemkv.RobotParser()
    text = (FIXTURES / name).read_text(encoding="utf-8").split("\n", 1)[1]
    for line in text.splitlines():
        parser.feed(line)
    return parser.result()


def test_parse_quoted_values():
    assert makemkv.parse_line('CINFO:2,0,"A \\"B\\", C <x> Ü"') == ("CINFO", ["2", "0", 'A "B", C <x> Ü'])
    assert makemkv.parse_line("garbage") is None


def test_parse_dvd_fixture():
    scan = _scan("info_dvd.txt")
    assert len(scan.titles) == 3
    first = scan.titles[0]
    assert first.seconds == 6730
    assert first.size == 7516192768
    assert first.chapters == 24
    assert first.source == "00001.mpls"
    assert first.segments == "1,2,3"
    assert first.output_name == "FILM_LABEL_t00.mkv"
    assert scan.titles[1].name == 'Extra, "Trailer"'
    assert scan.label == "FILM_LABEL"
    assert len(scan.messages) == 3


def test_progress():
    parser = makemkv.RobotParser()
    assert parser.feed("PRGV:100,32768,65536") == 0.5
    assert parser.feed('TINFO:0,9,0,"1:00:00"') is None


def test_argv_exact():
    dev, out = Path("/dev/sr0"), Path("/work/7")
    assert makemkv.info_argv(dev, 120)[1:] == ["-r", "--progress=-same", "--minlength=120", "info", "dev:/dev/sr0"]
    assert makemkv.mkv_argv(dev, 3, 120, out)[1:] == [
        "-r", "--progress=-same", "--minlength=120", "mkv", "dev:/dev/sr0", "3", str(out),
    ]


def test_fingerprint_stable_and_sensitive():
    titles = _scan("info_dvd.txt").titles
    base = makemkv.fingerprint("dvd", "FILM_LABEL", titles)
    assert makemkv.fingerprint("dvd", "FILM_LABEL", list(reversed(titles))) == base
    assert makemkv.fingerprint("dvd", "OTHER", titles) != base
    from dataclasses import replace
    changed = [replace(titles[0], seconds=titles[0].seconds + 1), *titles[1:]]
    assert makemkv.fingerprint("dvd", "FILM_LABEL", changed) != base


def test_settings_conf_escapes():
    text = makemkv.settings_conf('ab"c\\d', "-sel:all")
    assert 'app_Key = "ab\\"c\\\\d"' in text
    assert 'app_DefaultSelectionString = "-sel:all"' in text
    assert 'app_UpdateEnable = "0"' in text
    assert "app_Key" not in makemkv.settings_conf("", "-sel:all")
    injected = makemkv.settings_conf("k\nx\ry", "s\r\ny")
    assert injected == 'app_Key = "kxy"'+chr(10)+'app_DefaultSelectionString = "sy"'+chr(10)+'app_UpdateEnable = "0"'+chr(10)


def test_update_hint():
    assert makemkv.update_hint(_scan("info_too_old.txt").messages).startswith("Update MakeMKV")
    assert makemkv.update_hint(_scan("info_dvd.txt").messages) is None


def test_parse_decoy_fixture():
    titles = _scan("info_bluray_decoy.txt").titles
    assert [t.seconds for t in titles] == [8164, 8162, 750]
    assert titles[0].segments != titles[1].segments


def test_parser_degrades_gracefully():
    parser = makemkv.RobotParser()
    parser.feed('TINFO:0,9,0,"abc"')
    assert parser.result().titles[0].seconds == 0
    assert parser.feed("PRGV:1,2,0") is None
    assert parser.feed("PRGV:x,y,z") is None
    assert makemkv.parse_line('CINFO:2,0,"abc') is not None
    assert makemkv.parse_line('TINFO:0,9,0,""')[1][-1] == ""
