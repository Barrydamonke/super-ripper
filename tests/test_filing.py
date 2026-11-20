import json

import pytest

from disc_ripper import db, filing, tagging
from disc_ripper.filing import AlbumPlan

from .conftest import make_flac


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


ALBUM_TAGS = {"ALBUM": "The Dark Side of the Moon", "ALBUMARTIST": "Pink Floyd", "DATE": "1973-03-01"}


def plan(tmp_path, *, key="rel-1", release="rel-1", group="rg-1", disc=None, disc_total=1, titles=("Speak to Me",),
         album="The Dark Side of the Moon", artist="Pink Floyd", year="1973", compilation=False,
         track_artists=None, disc_id="disc-a", src="src"):
    tracks = []
    for n, title in enumerate(titles, start=1):
        path = make_flac(tmp_path / src / f"{n:02d}.flac")
        tags = {**ALBUM_TAGS, "ALBUM": album, "ALBUMARTIST": artist, "TITLE": title, "TRACKNUMBER": str(n),
                "DISCNUMBER": str(disc or 1)}
        if track_artists:
            tags["ARTIST"] = track_artists[n - 1]
        tracks.append((path, tags))
    return AlbumPlan(
        album_key=key, release_id=release, release_group_id=group, album_artist=artist, album=album, year=year,
        compilation=compilation, disc=disc, disc_total=disc_total, disc_id=disc_id,
        album_tags={**ALBUM_TAGS, "ALBUM": album, "ALBUMARTIST": artist}, tracks=tracks,
    )


def test_file_album_single_disc(tmp_path, conn, config):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")
    p = plan(tmp_path)
    folder = filing.file_album(conn, p, config.music_dir, cover=cover, now="t1")
    assert folder == config.music_dir / "Pink Floyd" / "The Dark Side of the Moon (1973)"
    assert (folder / "01 - Speak to Me.flac").is_file()
    assert (folder / "cover.jpg").read_bytes() == b"jpeg"
    assert not p.tracks[0][0].exists()  # moved, not copied
    row = conn.execute("SELECT * FROM albums").fetchone()
    assert (row["album_key"], row["folder"], row["release_group_id"], row["disc_total"]) == (
        "rel-1", "Pink Floyd/The Dark Side of the Moon (1973)", "rg-1", 1,
    )
    assert json.loads(row["discs"]) == {"1": "disc-a"}
    assert conn.execute("SELECT album_key FROM album_releases WHERE release_id = 'rel-1'").fetchone()[0] == "rel-1"


def test_second_disc_joins_folder_by_release(tmp_path, conn, config):
    filing.file_album(conn, plan(tmp_path, disc=1, disc_total=2, titles=("In the Flesh?",), src="a"),
                      config.music_dir, cover=None, now="t1")
    folder = filing.file_album(
        conn, plan(tmp_path, disc=2, disc_total=2, titles=("Hey You",), disc_id="disc-b", src="b",
                   album="The Dark Side Of The Moon", year="1974"),  # different spelling must not matter
        config.music_dir, cover=None, now="t2",
    )
    assert sorted(p.name for p in folder.iterdir()) == ["1-01 - In the Flesh_.flac", "2-01 - Hey You.flac"]
    assert json.loads(conn.execute("SELECT discs FROM albums").fetchone()[0]) == {"1": "disc-a", "2": "disc-b"}


def test_other_pressing_joins_via_album_key(tmp_path, conn, config):
    filing.file_album(conn, plan(tmp_path, disc=1, disc_total=2, src="a"), config.music_dir, cover=None, now="t1")
    folder = filing.file_album(
        conn, plan(tmp_path, key="rel-1", release="rel-OTHER", disc=2, disc_total=2, titles=("X",), src="b"),
        config.music_dir, cover=None, now="t2",
    )
    assert folder.name == "The Dark Side of the Moon (1973)"
    assert conn.execute("SELECT album_key FROM album_releases WHERE release_id = 'rel-OTHER'").fetchone()[0] == "rel-1"


def test_never_overwrites(tmp_path, conn, config):
    target = config.music_dir / "Pink Floyd" / "The Dark Side of the Moon (1973)"
    target.mkdir(parents=True)
    (target / "01 - Speak to Me.flac").write_bytes(b"existing")

    p = plan(tmp_path, src="b")
    # the folder exists but belongs to no album: that is a conflict too
    with pytest.raises(filing.FileExists):
        filing.file_album(conn, p, config.music_dir, cover=None, now="t1")
    assert p.tracks[0][0].exists()
    assert (target / "01 - Speak to Me.flac").read_bytes() == b"existing"


def test_existing_track_in_known_album_is_not_overwritten(tmp_path, conn, config):
    filing.file_album(conn, plan(tmp_path, src="a"), config.music_dir, cover=None, now="t1")
    again = plan(tmp_path, src="b", titles=("Speak to Me", "Breathe"))
    with pytest.raises(filing.FileExists):
        filing.file_album(conn, again, config.music_dir, cover=None, now="t2")
    assert all(src.exists() for src, _ in again.tracks)  # nothing moved, not even track 2


def test_duplicate_folder_suffix(tmp_path, conn, config):
    filing.file_album(conn, plan(tmp_path, src="a"), config.music_dir, cover=None, now="t1")
    folder = filing.file_album(
        conn, plan(tmp_path, key="dup", release="rel-1", src="b"), config.music_dir, cover=None, now="t2",
        allow_duplicate_folder=True,
    )
    assert folder.name == "The Dark Side of the Moon (1973) (2)"


def test_file_album_unicode_and_slash(tmp_path, conn, config):
    folder = filing.file_album(
        conn, plan(tmp_path, album="AC/DC Live", artist="Sigur Rós", year=""), config.music_dir, cover=None, now="t"
    )
    assert folder == config.music_dir / "Sigur Rós" / "AC_DC Live"
    assert tagging.read_tags(folder / "01 - Speak to Me.flac")["ALBUM"] == "AC/DC Live"


def test_compilation_layout(tmp_path, conn, config):
    folder = filing.file_album(
        conn,
        plan(tmp_path, album="Now 44", artist="Various Artists", year="1999", compilation=True, disc=2,
             disc_total=2, titles=("I Have a Dream",), track_artists=["Westlife"]),
        config.music_dir, cover=None, now="t",
    )
    assert (folder / "2-01 - Westlife - I Have a Dream.flac").is_file()
    assert folder.parent.name == "Various Artists"


def test_retag_album_moves_folder(tmp_path, conn, config):
    folder = filing.file_album(conn, plan(tmp_path, titles=("Speak to Me", "Breathe")), config.music_dir,
                               cover=None, now="t1")
    new = filing.retag_album(conn, "rel-1", {"ALBUM": "Dark Side", "ALBUMARTIST": "Pink Floyd", "DATE": "1973"},
                             config.music_dir, "t2")
    assert new == config.music_dir / "Pink Floyd" / "Dark Side (1973)"
    assert not folder.exists()
    assert sorted(p.name for p in new.iterdir()) == ["01 - Speak to Me.flac", "02 - Breathe.flac"]
    assert tagging.read_tags(new / "02 - Breathe.flac")["ALBUM"] == "Dark Side"
    assert tagging.read_tags(new / "02 - Breathe.flac")["TITLE"] == "Breathe"
    assert conn.execute("SELECT folder, album FROM albums").fetchone()[:] == ("Pink Floyd/Dark Side (1973)", "Dark Side")


def test_retag_refuses_existing_target(tmp_path, conn, config):
    filing.file_album(conn, plan(tmp_path), config.music_dir, cover=None, now="t1")
    (config.music_dir / "Pink Floyd" / "Taken (1973)").mkdir(parents=True)
    with pytest.raises(filing.FileExists):
        filing.retag_album(conn, "rel-1", {"ALBUM": "Taken", "ALBUMARTIST": "Pink Floyd", "DATE": "1973"},
                           config.music_dir, "t2")


def test_two_discs_filing_at_once_both_recorded(tmp_path, conn, config):
    import threading

    from disc_ripper import db as dbmod

    p1 = plan(tmp_path, disc=1, disc_total=2, titles=("A",), src="a", disc_id="d1")
    p2 = plan(tmp_path, disc=2, disc_total=2, titles=("B",), src="b", disc_id="d2")
    errors = []

    def go(p):
        c = dbmod.connect(config.db_path)
        try:
            filing.file_album(c, p, config.music_dir, cover=None, now="t")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=go, args=(p,)) for p in (p1, p2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(conn.execute("SELECT discs FROM albums").fetchone()[0]) == {"1": "d1", "2": "d2"}


def test_partial_failure_rolls_back(tmp_path, conn, config, monkeypatch):
    p = plan(tmp_path, titles=("One", "Two", "Three"))
    real_move = filing._move
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real_move(src, dst)

    monkeypatch.setattr(filing, "_move", flaky)
    with pytest.raises(OSError):
        filing.file_album(conn, p, config.music_dir, cover=None, now="t")
    assert all(src.exists() for src, _ in p.tracks)  # everything back where it was
    assert not (config.music_dir / "Pink Floyd" / "The Dark Side of the Moon (1973)").exists()
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 0


def test_refiling_same_disc_number_refused(tmp_path, conn, config):
    filing.file_album(conn, plan(tmp_path, src="a"), config.music_dir, cover=None, now="t1")
    again = plan(tmp_path, src="b", titles=("Speak to Me (Remastered)",))
    with pytest.raises(filing.FileExists):
        filing.file_album(conn, again, config.music_dir, cover=None, now="t2")
    assert len(list((config.music_dir / "Pink Floyd" / "The Dark Side of the Moon (1973)").glob("*.flac"))) == 1


def test_late_join_by_release_group(tmp_path, conn, config):
    # Disc 1 was filed (from release A) while disc 2's plan (release B, same group) was being built.
    filing.file_album(conn, plan(tmp_path, key="A", release="A", group="rg", disc=1, disc_total=2, src="a"),
                      config.music_dir, cover=None, now="t1")
    late = plan(tmp_path, key="B", release="B", group="rg", disc=2, disc_total=2, titles=("Two",), src="b",
                year="1974")
    late.album_tags["MUSICBRAINZ_ALBUMID"] = "B"
    folder = filing.file_album(conn, late, config.music_dir, cover=None, now="t2")
    assert folder.name == "The Dark Side of the Moon (1973)"
    assert late.album_key == "A"
    assert tagging.read_tags(folder / "2-01 - Two.flac")["DATE"] == ALBUM_TAGS["DATE"]
