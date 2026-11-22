import time

import pytest

from disc_ripper import film_imports, filmimport, settings

from .fake_radarr import LEGO, FakeRadarr
from .test_web_films import films, make_rip, queue  # noqa: F401

NOW = "2026-11-22T10:00:00+00:00"
NOT_CONFIGURED = "Set up Radarr in Settings to import films."


@pytest.fixture(autouse=True)
def instant_polls(monkeypatch):
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.0)


@pytest.fixture
def radarr(films, tmp_path):  # noqa: F811
    client, conn, config = films
    fake = FakeRadarr(tmp_path)
    client.app.state.services.radarr = lambda: fake
    settings.save(conn, {"radarr_url": "http://radarr.test", "radarr_api_key": "test-key"})
    return client, conn, config, fake


def film_rip(conn, config, **kwargs):
    rip = make_rip(conn, config, **kwargs)
    with conn:
        conn.execute("UPDATE video_rips SET main_title = 0 WHERE rip_id = ?", (rip,))
    return rip


def set_row(conn, rip, **fields):
    film_imports.ensure(conn, rip, NOW)
    film_imports.update(conn, rip, NOW, **fields)


def wait_for(conn, rip, states, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        row = film_imports.get(conn, rip)
        if row is not None and row["state"] in states:
            return row
        time.sleep(0.02)
    raise AssertionError(f"state stayed {film_imports.get(conn, rip)['state']!r}")


def with_encode(conn, config, rip):
    enc = config.downloads_dir / str(rip) / "enc"
    enc.mkdir()
    (enc / "lego.mkv").write_bytes(b"e" * 500)
    queue(conn, rip, 0, state="done", output=str(enc / "lego.mkv"))


CANDS = [
    {"tmdb_id": 137106, "title": "The Lego Movie", "year": 2014, "runtime_min": 100,
     "poster": "https://image.test/lego.jpg", "in_library": True, "folder": "The Lego Movie (2014)",
     "title_score": 1.0, "runtime_ok": True},
    {"tmdb_id": 2, "title": "Lego Movie 2", "year": 2019, "runtime_min": 107, "poster": "",
     "in_library": False, "folder": "", "title_score": 0.6, "runtime_ok": False},
]


def test_waiting_rip_shows_identify_form_and_candidates(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="needs_identify", candidates=CANDS, message="Pick the film.")
    text = client.get("/films").text
    for needle in ("Pick the film.", "The Lego Movie", "2014", "100 min", "in your library",
                   'loading="lazy"', 'alt="The Lego Movie"', "https://image.test/lego.jpg",
                   "/films/identify/search", "/films/identify/choose", "Use this film",
                   "Movies", "Other movies", 'name="q"'):
        assert needle in text, needle


def test_search_and_choose(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    with_encode(conn, config, rip)
    set_row(conn, rip, state="needs_identify", candidates=[], message="Pick the film.")
    r = client.post("/films/identify/search", data={"rip_id": rip, "q": "lego movie"})
    assert r.status_code == 200 and "The Lego Movie" in r.text and "Use this film" in r.text
    r = client.post("/films/identify/choose", data={"rip_id": rip, "tmdb_id": "137106", "root": "Movies"})
    assert r.status_code == 200
    row = wait_for(conn, rip, ("imported", "failed"))
    assert row["state"] == "imported", row["error"]
    assert row["tmdb_id"] == 137106 and row["root_folder"] == "Movies"


def test_search_bare_number_merges_title_and_tmdb(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    fake.catalog.append({"title": "1917", "year": 2019, "runtime": 119, "tmdbId": 530915, "imdbId": "tt8579674",
                         "remotePoster": "", "folder": "1917 (2019)"})
    fake.catalog.append({"title": "Other", "year": 2000, "runtime": 90, "tmdbId": 1917, "imdbId": "tt1",
                         "remotePoster": "", "folder": "Other (2000)"})
    r = client.post("/films/identify/search", data={"rip_id": rip, "q": "1917"})
    assert r.text.count('name="tmdb_id" value="530915"') == 1
    assert 'name="tmdb_id" value="1917"' in r.text
    assert "lookup" in fake.calls and "lookup_tmdb" in fake.calls


def test_search_error_shown(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    r = client.post("/films/identify/search", data={"rip_id": rip, "q": "tt9999999"})
    assert r.status_code == 200 and "404" in r.text and "<html" not in r.text


def test_paste_imdb_id(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    r = client.post("/films/identify/search", data={"rip_id": rip, "q": "https://www.imdb.com/title/tt1490017/"})
    assert "The Lego Movie" in r.text
    assert "lookup_imdb" in fake.calls and "lookup" not in fake.calls
    r = client.post("/films/identify/choose", data={"rip_id": rip, "tmdb_id": "tt1490017", "root": "Movies"})
    assert r.status_code == 200
    row = wait_for(conn, rip, ("failed", "imported", "needs_identify"))
    assert row["tmdb_id"] == 137106


def test_choose_refuses_bad_reference(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    r = client.post("/films/identify/choose", data={"rip_id": rip, "tmdb_id": "nonsense", "root": "Movies"})
    assert r.status_code == 400 and "Enter an IMDb ID" in r.text


REPLACE = {
    "old": {"path": "/library/Movies/Lego (2014)/lego.mkv", "size": 8_000_000_000, "resolution": "1920x1080",
            "video_codec": "h264", "audio_codec": "ac3", "audio_channels": 6, "seconds": 6000},
    "new": {"path": "/downloads/1/enc/lego.mkv", "size": 3_000_000_000, "resolution": "720x480",
            "video_codec": "hevc", "audio_codec": "aac", "audio_channels": 2, "seconds": 6100},
    "checks": {"length": True, "recycle_bin": True, "resolution": False},
    "reasons": {"resolution": "The new file is 480p but the old one is 1080p (a downgrade)."},
}


def test_replace_comparison_and_button(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="needs_replace_ok", tmdb_id=137106, title="The Lego Movie", year=2014,
            replace=REPLACE, message="This film is already in Radarr.")
    text = client.get("/films").text
    for needle in ("Resolution", "Video", "Audio", "Size", "Length", "1920x1080", "720x480", "1:40:00",
                   "(a downgrade)", 'class="bad"', "/films/replace", "Replace", "/films/skip",
                   "Delete this rip? Its files in downloads will be removed.", "Choose a different film"):
        assert needle in text, needle
    assert 'name="typed"' not in text


def test_replace_typed_confirm_without_recycle_bin(radarr):
    client, conn, config, fake = radarr
    fake.bin_set = False
    rip = film_rip(conn, config)
    bad = {**REPLACE, "checks": {"length": True, "recycle_bin": False, "resolution": True},
           "reasons": {"recycle_bin": "Radarr's recycle bin isn't set, so the old file could not be recovered."}}
    set_row(conn, rip, state="needs_replace_ok", tmdb_id=137106, title="The Lego Movie", year=2014, replace=bad)
    text = client.get("/films").text
    assert 'name="typed"' in text and "REPLACE" in text and "recycle bin isn&#39;t set" in text
    r = client.post("/films/replace", data={"rip_id": rip, "typed": ""})
    assert r.status_code == 409 and "Type REPLACE" in r.text
    assert film_imports.get(conn, rip)["state"] == "needs_replace_ok"


def test_replace_route_starts_in_background(radarr, monkeypatch):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="needs_replace_ok", tmdb_id=137106, replace=REPLACE)
    calls = []

    async def fake_approve(services, rip_id, typed=""):
        calls.append((rip_id, typed))

    monkeypatch.setattr(filmimport, "approve_replace", fake_approve)
    r = client.post("/films/replace", data={"rip_id": rip, "typed": ""})
    assert r.status_code == 200
    end = time.time() + 5
    while not calls and time.time() < end:
        time.sleep(0.02)
    assert calls == [(rip, "")]


def test_recently_imported_with_undo(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="imported", tmdb_id=137106, title="The Lego Movie", year=2014,
            imported_at="2026-11-22T10:00:00+00:00", replace=REPLACE, old_path=REPLACE["old"]["path"],
            new_path="/library/Movies/Lego (2014)/lego.mkv")
    text = client.get("/films").text
    assert "Recently imported" in text
    assert text.index("Recently imported") < text.index(f"Rip {rip}")
    for needle in ("The Lego Movie", "2014", "/films/undo", "Undo this import?", "Undo available until",
                   "29 Nov"):
        assert needle in text, needle


def test_undo_route_reports_result(radarr, monkeypatch):
    from disc_ripper import filmundo
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    results = iter(["Undone.", "Only an imported film can be undone."])

    async def fake_undo(services, rip_id):
        return next(results)

    monkeypatch.setattr(filmundo, "undo", fake_undo)
    r = client.post("/films/undo", data={"rip_id": rip})
    assert r.status_code == 200 and "Undone." in r.text
    r = client.post("/films/undo", data={"rip_id": rip})
    assert r.status_code == 409 and "Only an imported film" in r.text


def test_failed_import_shows_reason_retry_and_log(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="failed", step="import", error="Radarr didn't answer at http://radarr.test")
    text = client.get("/films").text
    assert "Radarr didn&#39;t answer at http://radarr.test" in text
    assert "/films/import/retry" in text and f"/films/import/{rip}/log" in text
    assert client.get(f"/films/import/{rip}/log").status_code == 404
    log_dir = config.config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"import-{rip}.log").write_text("GET /api/v3/movie -> ok\n", encoding="utf-8")
    r = client.get(f"/films/import/{rip}/log")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "GET /api/v3/movie" in r.text


def test_retry_resumes_in_background(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    with_encode(conn, config, rip)
    set_row(conn, rip, state="failed", step="claim", tmdb_id=137106, title="The Lego Movie", year=2014,
            root_folder="Movies", error="boom")
    assert client.post("/films/import/retry", data={"rip_id": rip}).status_code == 200
    assert wait_for(conn, rip, ("imported", "failed"))["state"] == "imported"


def test_skip_removes_rip_folder(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="needs_identify", candidates=[])
    r = client.post("/films/skip", data={"rip_id": rip})
    assert r.status_code == 200
    assert film_imports.get(conn, rip)["state"] == "skipped"
    assert not (config.downloads_dir / str(rip)).exists()


def test_radarr_not_configured_message(films):  # noqa: F811
    client, conn, config = films
    film_rip(conn, config)
    assert NOT_CONFIGURED in client.get("/films").text


def test_import_text_escaped(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    evil = "<script>alert(1)</script>"
    cands = [{**CANDS[0], "title": evil, "poster": 'x" onerror="alert(2)'}]
    set_row(conn, rip, state="needs_identify", candidates=cands, message=evil)
    other = film_rip(conn, config, fp="fp2")
    set_row(conn, other, state="failed", error=evil)
    text = client.get("/films").text
    assert "<script>alert" not in text and 'onerror="alert' not in text
    assert "&lt;script&gt;" in text
    fake.catalog.append({**LEGO, "title": evil, "tmdbId": 77, "imdbId": "tt7777777", "remotePoster": ""})
    r = client.post("/films/identify/search", data={"rip_id": rip, "q": "tt7777777"})
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_import_state_change_publishes_films_list(config):
    import asyncio

    from .test_videorip_rip import make_services

    services = make_services(config)

    async def main():
        with services.broker.subscribe() as sub:
            services.films_changed()
            services.films_changed()
            return await asyncio.wait_for(sub.queue.get(), 5)

    name, html = asyncio.run(main())
    assert name == "films" and "No DVD or Blu-ray rips yet." in html
