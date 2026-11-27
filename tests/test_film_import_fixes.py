"""Film import small fixes: undo after clean-up (F8), skip (F9), monitored restore (F10), Radarr
root/profile settings (F11), wording without Jellyfin (F12)."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from disc_ripper import film_imports, filmimport, filmundo, settings
from disc_ripper.radarr import RadarrError

from .test_encodepool import FakeSpace, Notifier, sync  # noqa: F401
from .test_filmimport import NOW, make_rip, row_of, services_with
from .test_filmimport_replace import probe, run
from .test_videorip_rip import make_services  # noqa: F401
from .test_videorip_scan import setup  # noqa: F401
from .test_web_films import films  # noqa: F401
from .test_web_films_import import film_rip, radarr, set_row, wait_for, with_encode  # noqa: F401


@pytest.fixture(autouse=True)
def instant_polls(monkeypatch):
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.0)


# -- F8 --------------------------------------------------------------------------------


def test_undo_after_cleanup_leaves_extras_in_library(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config, extras=[(1, "extra.mkv")])
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    extras = [Path(p) for p in json.loads(row["extras"])]
    assert extras and all(p.is_file() for p in extras)
    shutil.rmtree(config.downloads_dir / str(rip))  # the clean-up after Jellyfin
    film_imports.update(setup, rip, NOW, state="in_jellyfin")
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert all(p.is_file() for p in extras)
    assert json.loads(row_of(setup, rip)["extras"]) == []


def test_undo_before_cleanup_still_removes_extras(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config, extras=[(1, "extra.mkv")])
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    extras = [Path(p) for p in json.loads(row_of(setup, rip)["extras"])]
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert not any(p.exists() for p in extras)


def test_undo_confirm_text_says_extras_stay(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    set_row(conn, rip, state="in_jellyfin", tmdb_id=137106, title="The Lego Movie", year=2014,
            imported_at="2026-11-22T10:00:00+00:00", new_path="/library/Movies/Lego (2014)/lego.mkv")
    assert "Undo this import? The extras stay in the library." in client.get("/films").text


# -- F9 --------------------------------------------------------------------------------


def test_skip_refused_while_ripping(config, setup, tmp_path):
    rip, _ = make_rip(setup, config)
    with setup:
        setup.execute("UPDATE video_rips SET state = 'ripping' WHERE rip_id = ?", (rip,))
    services, fake = services_with(config, tmp_path)
    message = asyncio.run(filmundo.skip(services, rip))
    assert message and "ripping" in message
    assert (config.downloads_dir / str(rip)).is_dir()
    assert row_of(setup, rip) is None or row_of(setup, rip)["state"] != "skipped"


def test_skipped_rip_has_no_encode_controls(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    shutil.rmtree(config.downloads_dir / str(rip))
    set_row(conn, rip, state="skipped", message="Skipped.")
    text = client.get("/films").text
    assert "raw missing" not in text
    assert "/films/encode" not in text and "Encode all" not in text


# -- F10 -------------------------------------------------------------------------------


def test_undo_restores_monitored_of_existing_film(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    mine = fake.add_movie(fake.lookup_tmdb(137106), str(fake.root), 1)
    fake.movies[mine["id"]]["monitored"] = True
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert row["prev_monitored"] == 1
    assert fake.movies[mine["id"]]["monitored"] is False
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert fake.movies[mine["id"]]["monitored"] is True
    assert row_of(setup, rip)["prev_monitored"] is None


def test_undo_leaves_unmonitored_film_unmonitored(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    mine = fake.add_movie(fake.lookup_tmdb(137106), str(fake.root), 1)
    run(services, rip)
    assert row_of(setup, rip)["prev_monitored"] == 0
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert fake.movies[mine["id"]]["monitored"] is False


def test_migration_adds_prev_monitored(setup):
    cols = {r["name"] for r in setup.execute("PRAGMA table_info(film_imports)")}
    assert "prev_monitored" in cols


# -- F11 -------------------------------------------------------------------------------


def test_blank_settings_keep_old_behaviour(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["root_folder"] == "Movies"
    movie = fake.movies[row["movie_id"]]
    assert movie["rootFolderPath"] == str(fake.root) and movie["qualityProfileId"] == 1


def test_root_and_profile_settings_used_in_add_movie(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    settings.save(setup, {"radarr_root_folder": str(fake.other_root), "radarr_quality_profile": "hd-1080p"})
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    movie = fake.movies[row["movie_id"]]
    assert movie["rootFolderPath"] == str(fake.other_root) and movie["qualityProfileId"] == 4


def test_unknown_profile_setting_fails_with_reason(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    settings.save(setup, {"radarr_quality_profile": "Nope"})
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "failed" and "Nope" in row["error"]


def test_settings_page_lists_radarr_choices(radarr):
    client, conn, config, fake = radarr
    settings.save(conn, {"radarr_root_folder": str(fake.other_root)})
    text = client.get("/settings").text
    assert f'<option value="{fake.root}"' in text and "HD-1080p" in text
    assert f'<option value="{fake.other_root}" selected' in text
    assert "test-key" not in text


def test_settings_page_renders_with_radarr_down(radarr, monkeypatch):
    client, conn, config, fake = radarr
    settings.save(conn, {"radarr_root_folder": "/library/Gone", "radarr_quality_profile": "Fancy"})

    def down(*args, **kwargs):
        raise RadarrError("Radarr didn't answer at http://radarr.test")

    monkeypatch.setattr(fake, "root_folders", down)
    monkeypatch.setattr(fake, "quality_profiles", down)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'value="/library/Gone"' in r.text and 'value="Fancy"' in r.text


def test_settings_page_renders_without_radarr(logged_in_client):
    r = logged_in_client.get("/settings")
    assert r.status_code == 200 and 'name="radarr_root_folder"' in r.text


def test_settings_save_keeps_dropdown_values(radarr):
    from .test_web_pages import VALID
    client, conn, config, fake = radarr
    data = {"radarr_url": "http://radarr.test", "radarr_root_folder": str(fake.other_root),
            "radarr_quality_profile": "SD"}
    r = client.post("/settings", data={**VALID, **data})
    assert r.status_code == 200
    assert settings.get(conn, "radarr_root_folder") == str(fake.other_root)
    assert settings.get(conn, "radarr_quality_profile") == "SD"


def test_identify_form_lists_radarr_roots(radarr):
    client, conn, config, fake = radarr
    settings.save(conn, {"radarr_root_folder": str(fake.other_root)})
    rip = film_rip(conn, config)
    set_row(conn, rip, state="needs_identify", candidates=[], message="Pick the film.")
    r = client.post("/films/identify/search", data={"rip_id": rip, "q": "lego movie"})
    assert f'<option value="{fake.root}"' in r.text
    assert f'<option value="{fake.other_root}" selected' in r.text


def test_choose_with_root_path(radarr):
    client, conn, config, fake = radarr
    rip = film_rip(conn, config)
    with_encode(conn, config, rip)
    set_row(conn, rip, state="needs_identify", candidates=[], message="Pick the film.")
    r = client.post("/films/identify/choose", data={"rip_id": rip, "tmdb_id": "137106", "root": str(fake.other_root)})
    assert r.status_code == 200
    row = wait_for(conn, rip, ("imported", "failed"))
    assert row["state"] == "imported", row["error"]
    assert fake.movies[row["movie_id"]]["rootFolderPath"] == str(fake.other_root)


# -- F12 -------------------------------------------------------------------------------


def _imported(conn, config, state):
    rip = film_rip(conn, config)
    set_row(conn, rip, state=state, tmdb_id=137106, title="The Lego Movie", year=2014,
            imported_at="2026-11-22T10:00:00+00:00", new_path="/library/Movies/Lego (2014)/lego.mkv")
    return rip


@pytest.mark.parametrize("state", ["imported", "in_jellyfin"])
def test_no_jellyfin_wording_without_jellyfin(radarr, state):
    client, conn, config, fake = radarr
    _imported(conn, config, state)
    text = client.get("/films").text
    assert "In Jellyfin" not in text and "in Jellyfin" not in text and "waiting for Jellyfin" not in text
    assert "Imported" in text


def test_jellyfin_wording_with_jellyfin(radarr):
    client, conn, config, fake = radarr
    settings.save(conn, {"jellyfin_url": "http://jf.test", "jellyfin_api_key": "k"})
    _imported(conn, config, "imported")
    assert "waiting for Jellyfin to show it" in client.get("/films").text
    conn.execute("UPDATE film_imports SET state = 'in_jellyfin'")
    conn.commit()
    text = client.get("/films").text
    assert "In Jellyfin; the rip" in text and "· in Jellyfin" in text


def test_raw_hold_note_without_jellyfin_does_not_wait_for_it(radarr):
    client, conn, config, fake = radarr
    settings.save(conn, {"raw_keep_until": "jellyfin"})
    rip = _imported(conn, config, "imported")
    assert film_imports.jellyfin_hold(conn, rip) == "imported; waiting for clean-up"
    assert "waiting for Jellyfin" not in client.get("/films").text


# -- review follow-ups -----------------------------------------------------------------


def test_radarr_down_is_cached_between_page_loads(radarr, monkeypatch):
    client, conn, config, fake = radarr
    calls = []

    def down(*args, **kwargs):
        calls.append(1)
        raise RadarrError("Radarr didn't answer at http://radarr.test")

    monkeypatch.setattr(fake, "root_folders", down)
    monkeypatch.setattr(fake, "quality_profiles", down)
    assert client.get("/films").status_code == 200
    assert client.get("/films").status_code == 200
    assert len(calls) == 1


def test_failed_refresh_keeps_last_good_roots(radarr, monkeypatch):
    from disc_ripper.web import films_routes
    client, conn, config, fake = radarr
    films_routes._ROOTS.update(roots=[{"path": "/good/Movies"}], at=-1e9)  # stale, so refreshed next

    def down(*args, **kwargs):
        raise RadarrError("down")

    monkeypatch.setattr(fake, "root_folders", down)
    client.get("/films")
    assert films_routes._ROOTS["roots"] == [{"path": "/good/Movies"}]


def test_reimport_after_cleanup_undo_reuses_extras(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config, extras=[(1, "extra.mkv")])
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    row = row_of(setup, rip)
    [placed] = [Path(p) for p in json.loads(row["extras"])]
    film_imports.update(setup, rip, NOW, state="in_jellyfin")  # the rip folder is still there
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert placed.is_file()
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert [Path(p) for p in json.loads(row["extras"])] == [placed]
    assert not list(placed.parent.glob("*(2)*"))


@pytest.mark.parametrize("state", ["queued", "running", "waiting_space"])
def test_skip_refused_while_rip_job_active(config, setup, tmp_path, state):
    from disc_ripper import jobs
    rip, _ = make_rip(setup, config)
    job = jobs.enqueue(setup, "serial:A", "rip_title", {"rip_id": rip, "title": 1}, uses_drive=True, now="t")
    with setup:
        setup.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job))
    services, fake = services_with(config, tmp_path)
    message = asyncio.run(filmundo.skip(services, rip))
    assert message and "ripping" in message
    assert (config.downloads_dir / str(rip)).is_dir()
