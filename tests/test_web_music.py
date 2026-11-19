import json
from pathlib import Path

import pytest

from disc_ripper import cd_rips, cdrip, db, jobs, musicbrainz, tagging

from .conftest import make_flac

MB = Path(__file__).parent / "fixtures" / "mb"
NEVERMIND = "64e.YDM.N2tZJzmqrN48iWfJ9.4-"


def nevermind_candidates():
    data = json.loads((MB / "discid_pressings.json").read_text(encoding="utf-8"))["releases"]
    return musicbrainz.candidates_for_disc(data, NEVERMIND)


@pytest.fixture
def music(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    logged_in_client.post("/drives/rescan")
    conn = db.connect(config.db_path)
    yield logged_in_client, conn, config
    conn.close()


def waiting_rip(conn, config, *, tracks=12, cands=None, reasons=("several possible releases",), release_id=None,
                disc_id=NEVERMIND):
    job_id = jobs.enqueue(conn, "serial:A", "rip_cd", {"disc_id": disc_id}, uses_drive=True, now="t")
    cd_rips.create(conn, job_id, disc_id, "t")
    jobs.start(conn, job_id, "t")
    jobs.finish(conn, job_id, "done", "t")
    dest = config.downloads_dir / str(job_id)
    for n in range(1, tracks + 1):
        make_flac(dest / f"{n:02d}.flac", seconds=60 + n)
    cd_rips.update(
        conn, job_id, state="needs_look", reasons=list(reasons),
        candidates=cdrip.candidate_snapshot(cands or []), release_id=release_id, downloads_path=str(dest),
        accuraterip=[{"track": n, "status": "verified", "confidence": 3} for n in range(1, tracks + 1)],
        summary=f"Unknown album · {tracks} tracks",
    )
    return job_id


def test_music_page_lists(music):
    client, conn, config = music
    j = waiting_rip(conn, config)
    r = client.get("/music")
    assert r.status_code == 200
    assert "Needs a look" in r.text and "several possible releases" in r.text
    assert f'href="/music/{j}"' in r.text


def test_item_page(music):
    client, conn, config = music
    j = waiting_rip(conn, config, cands=nevermind_candidates())
    r = client.get(f"/music/{j}")
    assert r.status_code == 200
    assert "Smells Like Teen Spirit" in r.text  # candidate track list
    assert f'src="/music/{j}/track/1"' in r.text
    assert "1:01" in r.text  # track 1 duration
    assert client.get("/music/99999").status_code == 404


def test_use_release_files(music):
    client, conn, config = music
    cands = nevermind_candidates()
    j = waiting_rip(conn, config, cands=cands)
    r = client.post(f"/music/{j}/use-release", data={"release_id": cands[0].release_id})
    assert r.status_code in (200, 303)
    folder = config.music_dir / "Nirvana" / f"Nevermind ({cands[0].year})"
    assert (folder / "01 - Smells Like Teen Spirit.flac").is_file()
    assert cd_rips.get(conn, j)["state"] == "filed"
    assert not (config.downloads_dir / str(j)).exists()


def test_use_release_rejects_unknown_id(music):
    client, conn, config = music
    j = waiting_rip(conn, config, cands=nevermind_candidates())
    r = client.post(f"/music/{j}/use-release", data={"release_id": "not-a-candidate"})
    assert r.status_code == 400
    assert cd_rips.get(conn, j)["state"] == "needs_look"


def test_use_url_rejects_junk(music):
    client, conn, config = music
    j = waiting_rip(conn, config)
    r = client.post(f"/music/{j}/use-url", data={"ref": "https://example.com/not-musicbrainz"})
    assert r.status_code == 400
    assert "MusicBrainz release link" in r.text


def test_use_url_looks_up_release(music):
    client, conn, config = music
    release = json.loads((MB / "release_wall.json").read_text(encoding="utf-8"))

    class FakeMB:
        def get_release(self, release_id):
            assert release_id == release["id"]
            return release

    client.app.state.services.musicbrainz = FakeMB()
    disc2 = "uDAm7lKUREWYqiy5SA5BJ.OI6QM-"
    j = waiting_rip(conn, config, tracks=13, disc_id=disc2)
    r = client.post(f"/music/{j}/use-url", data={"ref": f"https://musicbrainz.org/release/{release['id']}"})
    assert r.status_code in (200, 303), r.text
    folder = config.music_dir / "Pink Floyd" / "The Wall (1994)"
    assert sorted(p.name for p in folder.glob("*.flac"))[0].startswith("2-01 - ")


def _manual_form(tracks, **extra):
    form = {"album": "Live at Home", "album_artist": "The Band", "year": "2003", "disc": "1", "disc_total": "1"}
    form.update({f"title_{n}": f"Song {n}" for n in range(1, tracks + 1)})
    form.update(extra)
    return form


def test_manual_requires_titles(music):
    client, conn, config = music
    j = waiting_rip(conn, config, tracks=3)
    form = _manual_form(3)
    del form["title_2"]
    r = client.post(f"/music/{j}/manual", data=form)
    assert r.status_code == 400
    assert "Every track needs a title" in r.text
    r = client.post(f"/music/{j}/manual", data=_manual_form(3, year="99"))
    assert r.status_code == 400


def test_manual_files_with_manual_key(music):
    client, conn, config = music
    j = waiting_rip(conn, config, tracks=3)
    client.post(f"/music/{j}/manual", data=_manual_form(3))
    folder = config.music_dir / "The Band" / "Live at Home (2003)"
    assert (folder / "02 - Song 2.flac").is_file()
    tags = tagging.read_tags(folder / "02 - Song 2.flac")
    assert (tags["ALBUM"], tags["ARTIST"], tags["TRACKNUMBER"], tags["DATE"]) == ("Live at Home", "The Band", "2", "2003")
    assert conn.execute("SELECT album_key FROM albums").fetchone()[0].startswith("manual:")


def test_add_to_existing_album(music):
    client, conn, config = music
    j1 = waiting_rip(conn, config, tracks=2)
    client.post(f"/music/{j1}/manual", data=_manual_form(2, disc_total="2"))
    key = conn.execute("SELECT album_key FROM albums").fetchone()[0]
    j2 = waiting_rip(conn, config, tracks=2, disc_id="disc-two")
    form = {"album_key": key, "disc": "2", "title_1": "Encore", "title_2": "Goodnight"}
    r = client.post(f"/music/{j2}/add-to", data=form)
    assert r.status_code in (200, 303), r.text
    folder = config.music_dir / "The Band" / "Live at Home (2003)"
    assert sorted(p.name for p in folder.glob("*.flac")) == [
        "1-01 - Song 1.flac", "1-02 - Song 2.flac", "2-01 - Encore.flac", "2-02 - Goodnight.flac",
    ]
    assert tagging.read_tags(folder / "2-01 - Encore.flac")["ALBUM"] == "Live at Home"


def test_file_anyway_duplicate_suffix(music):
    client, conn, config = music
    cands = nevermind_candidates()
    first = waiting_rip(conn, config, cands=cands)
    client.post(f"/music/{first}/use-release", data={"release_id": cands[0].release_id})
    j = waiting_rip(conn, config, cands=cands, reasons=("already in the library",), release_id=cands[0].release_id)
    r = client.post(f"/music/{j}/file-anyway")
    assert r.status_code in (200, 303), r.text
    assert (config.music_dir / "Nirvana" / f"Nevermind ({cands[0].year}) (2)").is_dir()


def test_file_anyway_needs_a_release(music):
    client, conn, config = music
    j = waiting_rip(conn, config)
    assert client.post(f"/music/{j}/file-anyway").status_code == 400


def test_discard_removes_files(music):
    client, conn, config = music
    j = waiting_rip(conn, config)
    client.post(f"/music/{j}/discard")
    assert not (config.downloads_dir / str(j)).exists()
    assert cd_rips.get(conn, j)["state"] == "discarded"


def test_track_route(music):
    client, conn, config = music
    j = waiting_rip(conn, config, tracks=2)
    r = client.get(f"/music/{j}/track/2")
    assert r.status_code == 200
    assert r.content.startswith(b"fLaC")
    assert client.get(f"/music/{j}/track/3").status_code == 404
    assert client.get(f"/music/{j}/track/0").status_code == 404
    assert client.get(f"/music/{j}/track/..%2F..%2Fsecret").status_code in (404, 422)


def test_edit_album_renames_folder(music):
    client, conn, config = music
    j = waiting_rip(conn, config, tracks=2)
    client.post(f"/music/{j}/manual", data=_manual_form(2))
    key = conn.execute("SELECT album_key FROM albums").fetchone()[0]
    assert client.get(f"/music/album/{key}/edit").status_code == 200
    r = client.post(f"/music/album/{key}/edit", data={"album": "Live in the Garden", "album_artist": "The Band",
                                                      "year": "2004"})
    assert r.status_code in (200, 303), r.text
    assert (config.music_dir / "The Band" / "Live in the Garden (2004)" / "01 - Song 1.flac").is_file()


def test_nav_count(music):
    client, conn, config = music
    waiting_rip(conn, config)
    waiting_rip(conn, config, disc_id="other")
    assert "Music (2)" in client.get("/drives").text


def test_titles_escaped(music):
    client, conn, config = music
    j = waiting_rip(conn, config, tracks=1)
    client.post(f"/music/{j}/manual", data=_manual_form(1, album="<b>x</b>", title_1="<i>y</i>"))
    page = client.get("/music").text
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page


def test_music_requires_login(client):
    assert client.get("/music").status_code == 303
