import asyncio
import json
from pathlib import Path

import pytest

from disc_ripper import cd_rips, cdrip, db, drives, jellyfin, jobs, musicbrainz, settings, tagging
from disc_ripper.cdtoc import Toc
from disc_ripper.cyanrip import TrackCheck

from .conftest import make_flac

MB = Path(__file__).parent / "fixtures" / "mb"
NEVERMIND = "64e.YDM.N2tZJzmqrN48iWfJ9.4-"
NOW44_2 = "4IlxhiOv_kwgl3u4ry8bDpY4DIg-"
WALL_2 = "uDAm7lKUREWYqiy5SA5BJ.OI6QM-"
WALL_GROUP = "f2026101-945b-3d05-9ef4-aa718fc3feef"
NOW44_GROUP = None  # read from the fixture


def releases(name):
    return json.loads((MB / f"discid_{name}.json").read_text(encoding="utf-8"))["releases"]


class FakeMB:
    def __init__(self, data=None, error=False):
        self.data, self.error = data or [], error

    def lookup_discid(self, disc_id):
        if self.error:
            raise musicbrainz.MusicBrainzUnavailable("MusicBrainz unreachable: timed out")
        return self.data


class FakeJellyfin:
    def __init__(self, owned=(), error=False):
        self.owned, self.error = set(owned), error

    def has_album(self, release_id, album):
        if self.error:
            raise jellyfin.JellyfinError("401 Unauthorized")
        return release_id in self.owned


class FakeRefresher:
    def __init__(self):
        self.triggers = 0

    def trigger(self):
        self.triggers += 1


class FakeServices:
    def __init__(self, mb, jf=None):
        self.musicbrainz = mb
        self._jf = jf
        self.refresher = FakeRefresher()
        self.covers = []

    def jellyfin(self):
        return self._jf

    def cover_fetch(self, release_id):
        self.covers.append(release_id)
        return b"\xff\xd8cover"


class Ctx:
    def __init__(self, config, services, job_id):
        self.config, self.services, self.job_id, self.drive_id = config, services, job_id, "serial:A"

    async def db(self, fn, *args, **kwargs):
        def call():
            conn = db.connect(self.config.db_path)
            try:
                return fn(conn, *args, **kwargs)
            finally:
                conn.close()

        return await asyncio.to_thread(call)

    async def report(self, *args, **kwargs):
        pass


@pytest.fixture
def conn(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    c = db.connect(config.db_path)
    db.migrate(c)
    drives.sync(c, drives.discover(config.sysfs_root), "t0")
    yield c
    c.close()


def rip(config, conn, services, disc_id, n_tracks, *, statuses=None):
    job_id = jobs.enqueue(conn, "serial:A", "rip_cd", {}, uses_drive=True, now="t")
    cd_rips.create(conn, job_id, disc_id, "t")
    rip_dir = config.work_dir / str(job_id) / "rip"
    for n in range(1, n_tracks + 1):
        make_flac(rip_dir / f"{n}.flac")
    (rip_dir / "log.log").write_text("log", encoding="utf-8")
    statuses = statuses or {}
    checks = [TrackCheck(n, statuses.get(n, "verified"), 5) for n in range(1, n_tracks + 1)]
    toc = Toc(1, n_tracks, tuple(range(0, n_tracks * 1000, 1000)), n_tracks * 1000, frozenset())
    asyncio.run(cdrip.after_rip(Ctx(config, services, job_id), disc_id=disc_id, toc=toc, checks=checks,
                                rip_dir=rip_dir))
    return job_id, cd_rips.get(conn, job_id)


def reasons(row):
    return json.loads(row["reasons"])


def test_clean_match_files_itself(config, conn):
    services = FakeServices(FakeMB(releases("pressings")))
    job_id, row = rip(config, conn, services, NEVERMIND, 12)
    assert row["state"] == "filed", reasons(row)
    folder = config.music_dir / "Nirvana" / "Nevermind (1991)"
    assert (folder / "01 - Smells Like Teen Spirit.flac").is_file()
    assert (folder / "cover.jpg").read_bytes() == b"\xff\xd8cover"
    assert tagging.read_tags(folder / "01 - Smells Like Teen Spirit.flac")["MUSICBRAINZ_DISCID"] == NEVERMIND
    assert services.refresher.triggers == 1
    assert row["summary"] == "Nirvana — Nevermind (1991)"
    assert not (config.work_dir / str(job_id)).exists()
    assert not (config.downloads_dir / str(job_id)).exists()


def test_unverified_goes_to_look(config, conn):
    _, row = rip(config, conn, FakeServices(FakeMB(releases("pressings"))), NEVERMIND, 12, statuses={4: "mismatch"})
    assert row["state"] == "needs_look"
    assert reasons(row) == ["1 track(s) not verified"]
    downloads = Path(row["downloads_path"])
    assert sorted(p.name for p in downloads.glob("*.flac"))[:2] == ["01.flac", "02.flac"]
    assert tagging.read_tags(downloads / "01.flac")["ALBUM"] == "Nevermind"  # tagged already


def test_several_releases_goes_to_look(config, conn):
    _, row = rip(config, conn, FakeServices(FakeMB(releases("two_disc_2"))), WALL_2, 13)
    assert row["state"] == "needs_look"
    assert "several possible releases" in reasons(row)
    assert len(json.loads(row["candidates"])) == 8


def test_no_match_goes_to_look(config, conn):
    _, row = rip(config, conn, FakeServices(FakeMB([])), "nomatch", 9)
    assert reasons(row) == ["no MusicBrainz match"]
    assert row["summary"] == "Unknown album · 9 tracks"


def test_mb_unreachable_goes_to_look(config, conn):
    job_id, row = rip(config, conn, FakeServices(FakeMB(error=True)), NEVERMIND, 12)
    assert reasons(row) == ["MusicBrainz unreachable"]
    assert len(list((config.downloads_dir / str(job_id)).glob("*.flac"))) == 12


def _filed_album(conn, *, key, group, disc_total, discs, folder="Pink Floyd/The Wall (1994)"):
    conn.execute(
        "INSERT INTO albums (album_key, folder, release_group_id, album_artist, album, year, disc_total, album_tags,"
        " discs, filed_at) VALUES (?, ?, ?, 'Pink Floyd', 'The Wall', '1994', ?, ?, ?, 't')",
        (key, folder, group, disc_total,
         json.dumps({"ALBUM": "The Wall", "ALBUMARTIST": "Pink Floyd", "DATE": "1994-10-10",
                     "MUSICBRAINZ_ALBUMID": key}), json.dumps(discs)),
    )
    conn.commit()


def test_duplicate_goes_to_look(config, conn):
    services = FakeServices(FakeMB(releases("pressings")))
    rip(config, conn, services, NEVERMIND, 12)
    _, row = rip(config, conn, services, NEVERMIND, 12)
    assert reasons(row) == ["already in the library"]


def test_jellyfin_duplicate(config, conn):
    first = musicbrainz.group_equivalent(musicbrainz.candidates_for_disc(releases("pressings"), NEVERMIND))[0][0]
    _, row = rip(config, conn, FakeServices(FakeMB(releases("pressings")), FakeJellyfin({first.release_id})),
                 NEVERMIND, 12)
    assert reasons(row) == ["already in the library"]


def test_other_pressing_of_filed_album_joins(config, conn):
    (config.music_dir / "Pink Floyd" / "The Wall (1994)").mkdir(parents=True)
    _filed_album(conn, key="wall-disc1-release", group=WALL_GROUP, disc_total=2, discs={"1": "d1"})
    _, row = rip(config, conn, FakeServices(FakeMB(releases("two_disc_2"))), WALL_2, 13)
    assert row["state"] == "filed", reasons(row)
    folder = config.music_dir / "Pink Floyd" / "The Wall (1994)"
    files = sorted(p.name for p in folder.glob("*.flac"))
    assert files[0].startswith("2-01 - ")
    tags = tagging.read_tags(folder / files[0])
    assert tags["MUSICBRAINZ_ALBUMID"] == "wall-disc1-release"  # album-level tags copied from disc 1
    assert tags["DISCNUMBER"] == "2"


def test_deluxe_edition_goes_to_look(config, conn):
    _filed_album(conn, key="wall-deluxe", group=WALL_GROUP, disc_total=3, discs={"1": "d1"})
    _, row = rip(config, conn, FakeServices(FakeMB(releases("two_disc_2"))), WALL_2, 13)
    assert "another edition of an album in the library" in reasons(row)


def test_second_disc_while_first_waiting(config, conn):
    group = musicbrainz.candidates_for_disc(releases("compilation"), NOW44_2)[0].release_group_id
    waiting = jobs.enqueue(conn, "serial:A", "rip_cd", {}, now="t")
    cd_rips.create(conn, waiting, "disc1", "t")
    cd_rips.update(conn, waiting, state="needs_look", reasons=["1 track(s) not verified"],
                   candidates=[{"release_group_id": group, "medium": 1}])
    _, row = rip(config, conn, FakeServices(FakeMB(releases("compilation"))), NOW44_2, 22)
    assert reasons(row) == ["disc 2 of an album waiting for a look"]


def test_approval_always(config, conn):
    settings.save(conn, {"cd_approval": "always"})
    _, row = rip(config, conn, FakeServices(FakeMB(releases("pressings"))), NEVERMIND, 12)
    assert reasons(row) == ["you asked to check every album"]


def test_track_count_mismatch_goes_to_look(config, conn):
    _, row = rip(config, conn, FakeServices(FakeMB(releases("pressings"))), NEVERMIND, 11)
    assert "MusicBrainz track list doesn't match the disc" in reasons(row)


def test_jellyfin_errors_do_not_block_filing(config, conn):
    _, row = rip(config, conn, FakeServices(FakeMB(releases("pressings")), FakeJellyfin(error=True)), NEVERMIND, 12)
    assert row["state"] == "filed"
    assert "Jellyfin check failed" in row["summary"]


def test_cover_art_off(config, conn):
    settings.save(conn, {"cd_cover_art": False})
    services = FakeServices(FakeMB(releases("pressings")))
    rip(config, conn, services, NEVERMIND, 12)
    assert services.covers == []
    assert not (config.music_dir / "Nirvana" / "Nevermind (1991)" / "cover.jpg").exists()


def test_refresh_debounced():
    calls = []

    class Client:
        def refresh(self):
            calls.append(1)

    async def main():
        r = jellyfin.RefreshDebouncer(lambda: Client(), delay=0.05)
        for _ in range(3):
            r.trigger()
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        return r

    r = asyncio.run(main())
    assert calls == [1]
    assert r.last_error is None


def test_refresh_error_kept():
    class Client:
        def refresh(self):
            raise jellyfin.JellyfinError("connection refused")

    async def main():
        r = jellyfin.RefreshDebouncer(lambda: Client(), delay=0.01)
        r.trigger()
        await asyncio.sleep(0.1)
        return r

    assert "connection refused" in asyncio.run(main()).last_error


def test_jellyfin_client_requests():
    seen = []

    def fetch(method, url, headers, timeout):
        seen.append((method, url, headers))
        if method == "GET":
            return json.dumps({"Items": [{"ProviderIds": {"MusicBrainzAlbum": "rel-1"}}]}).encode()
        return b""

    client = jellyfin.Jellyfin("http://jf:8096/", "KEY", fetch=fetch)
    assert client.has_album("rel-1", "Nevermind") is True
    assert client.has_album("rel-2", "Nevermind") is False
    client.refresh()
    method, url, headers = seen[0]
    assert method == "GET" and url.startswith("http://jf:8096/Items?")
    assert "searchTerm=Nevermind" in url and "IncludeItemTypes=MusicAlbum" in url
    assert headers["Authorization"] == 'MediaBrowser Token="KEY"'
    assert seen[-1][:2] == ("POST", "http://jf:8096/Library/Refresh")


# --- checkpoint 2 review fixes -------------------------------------------------------


def test_garbled_mb_answer_keeps_files_and_location(config, conn):
    bad = releases("pressings")
    bad[0] = {**bad[0], "media": [{**bad[0]["media"][0], "position": "x"}]}
    job_id, row = rip(config, conn, FakeServices(FakeMB(bad)), NEVERMIND, 12)
    assert row["state"] == "needs_look"
    assert reasons(row) == ["MusicBrainz unreachable"]
    assert row["downloads_path"] == str(config.downloads_dir / str(job_id))
    assert len(list((config.downloads_dir / str(job_id)).glob("*.flac"))) == 12


def test_mb_http_exception_is_retried_then_unreachable():
    import http.client

    calls = []

    def fetch(url, headers, timeout):
        calls.append(url)
        raise http.client.IncompleteRead(b"")

    mb = musicbrainz.MusicBrainz(fetch=fetch, clock=lambda: 0.0, sleep=lambda s: None)
    with pytest.raises(musicbrainz.MusicBrainzUnavailable):
        mb.lookup_discid(NEVERMIND)
    assert len(calls) == 4


def test_joining_disc_ignores_jellyfin_having_disc_one(config, conn):
    (config.music_dir / "Pink Floyd" / "The Wall (1994)").mkdir(parents=True)
    _filed_album(conn, key="wall-disc1-release", group=WALL_GROUP, disc_total=2, discs={"1": "d1"})
    _, row = rip(config, conn, FakeServices(FakeMB(releases("two_disc_2")), FakeJellyfin({"wall-disc1-release"})),
                 WALL_2, 13)
    assert row["state"] == "filed", reasons(row)


def test_unambiguous_other_edition_goes_to_look(config, conn):
    group = musicbrainz.candidates_for_disc(releases("pressings"), NEVERMIND)[0].release_group_id
    conn.execute(
        "INSERT INTO albums (album_key, folder, release_group_id, album_artist, album, year, disc_total, discs,"
        " filed_at) VALUES ('deluxe', 'Nirvana/Nevermind (2011)', ?, 'Nirvana', 'Nevermind', '2011', 4, '{}', 't')",
        (group,),
    )
    conn.commit()
    _, row = rip(config, conn, FakeServices(FakeMB(releases("pressings"))), NEVERMIND, 12)
    assert row["state"] == "needs_look"
    assert "another edition of an album in the library" in reasons(row)


def test_jellyfin_odd_json_does_not_block():
    for body in (b"[]", b'{"Items": ["x"]}', b"not json"):
        client = jellyfin.Jellyfin("http://jf", "k", fetch=lambda m, u, h, t, body=body: body)
        with pytest.raises(jellyfin.JellyfinError):
            client.has_album("r", "a")
    # "Items": null is simply no albums
    assert jellyfin.Jellyfin("http://jf", "k", fetch=lambda m, u, h, t: b'{"Items": null}').has_album("r", "a") is False


def test_cover_fetch_http_exception_is_none():
    import http.client

    from disc_ripper import coverart

    def fetch(url, headers, timeout):
        raise http.client.IncompleteRead(b"")

    assert coverart.fetch_front("abc", fetch=fetch) is None


# --- final review fixes ----------------------------------------------------------------


def test_move_failure_keeps_files_visible(config, conn, monkeypatch):
    from disc_ripper import filing as filing_mod

    real = filing_mod._move
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise PermissionError(13, "Permission denied")
        real(src, dst)

    monkeypatch.setattr(filing_mod, "_move", flaky)
    job_id = jobs.enqueue(conn, "serial:A", "rip_cd", {}, uses_drive=True, now="t")
    cd_rips.create(conn, job_id, NEVERMIND, "t")
    rip_dir = config.work_dir / str(job_id) / "rip"
    for n in range(1, 13):
        make_flac(rip_dir / f"{n:02d}.flac")
    toc = Toc(1, 12, tuple(range(0, 12000, 1000)), 12000, frozenset())
    ctx = Ctx(config, FakeServices(FakeMB(releases("pressings"))), job_id)
    with pytest.raises(PermissionError):
        asyncio.run(cdrip.after_rip(ctx, disc_id=NEVERMIND, toc=toc, checks=[], rip_dir=rip_dir))
    row = cd_rips.get(conn, job_id)
    assert row["downloads_path"] == str(rip_dir)  # where the audio actually is
    assert len(cdrip.rip_files(row)) == 12  # moved files were put back
