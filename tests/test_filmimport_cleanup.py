import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from disc_ripper import film_imports, filmimport, services as services_mod
from disc_ripper.jellyfin import JellyfinError

from .fake_radarr import FakeRadarr
from .test_encodepool import FakeSpace, Notifier, sync  # noqa: F401
from .test_filmimport import make_rip, row_of, services_with
from .test_videorip_rip import make_services  # noqa: F401
from .test_videorip_scan import setup  # noqa: F401


@pytest.fixture(autouse=True)
def instant_polls(monkeypatch):
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.0)


class FakeJellyfin:
    def __init__(self, sources=(), error=None):
        self.sources, self.error, self.asked = list(sources), error, []

    def movie_sources(self, tmdb_id, title):
        self.asked.append((tmdb_id, title))
        if self.error:
            raise self.error
        return self.sources


def imported(config, setup, tmp_path):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    return rip, services, row


def at(row, hours):
    return datetime.fromisoformat(row["imported_at"]) + timedelta(hours=hours)


def confirm(services, now):
    asyncio.run(filmimport.confirm_and_clean(services, now))


def test_cleanup_after_jellyfin_shows_it(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    new = Path(row["new_path"])
    jf = FakeJellyfin([("/media/Movies/x/" + new.name, new.stat().st_size)])  # another mount point
    services.jellyfin = lambda: jf
    confirm(services, at(row, 0.1))
    after = row_of(setup, rip)
    assert after["state"] == "in_jellyfin" and not (config.downloads_dir / str(rip)).exists()
    assert jf.asked == [(137106, "The Lego Movie")] and new.is_file()


def test_same_name_but_old_size_is_not_seen(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    new = Path(row["new_path"])
    services.jellyfin = lambda: FakeJellyfin([(str(new), new.stat().st_size + 1)])
    confirm(services, at(row, 1))
    assert row_of(setup, rip)["state"] == "imported" and (config.downloads_dir / str(rip)).exists()


def test_no_jellyfin_cleans_after_24h(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    services.jellyfin = lambda: None
    confirm(services, at(row, 23))
    assert row_of(setup, rip)["state"] == "imported"
    confirm(services, at(row, 24))
    assert row_of(setup, rip)["state"] == "in_jellyfin" and not (config.downloads_dir / str(rip)).exists()


def test_not_seen_after_24h_keeps_rip_with_message(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    services.jellyfin = lambda: FakeJellyfin()
    confirm(services, at(row, 25))
    after = row_of(setup, rip)
    assert after["state"] == "imported" and after["message"] == filmimport.NOT_SEEN
    assert (config.downloads_dir / str(rip)).exists()
    stamp = after["updated_at"]
    confirm(services, at(row, 26))
    assert row_of(setup, rip)["updated_at"] == stamp  # set once, not rewritten


def test_jellyfin_error_leaves_row_for_next_pass(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    services.jellyfin = lambda: FakeJellyfin(error=JellyfinError("down"))
    confirm(services, at(row, 30))
    after = row_of(setup, rip)
    assert after["state"] == "imported" and after["message"] != filmimport.NOT_SEEN
    assert (config.downloads_dir / str(rip)).exists()
    assert "down" in (config.config_dir / "logs" / f"import-{rip}.log").read_text(encoding="utf-8")


def test_cleanup_with_folder_already_gone(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    services.jellyfin = lambda: None
    import shutil
    shutil.rmtree(config.downloads_dir / str(rip))
    confirm(services, at(row, 24))
    assert row_of(setup, rip)["state"] == "in_jellyfin"


def test_failed_delete_is_retried(config, setup, tmp_path, monkeypatch):
    rip, services, row = imported(config, setup, tmp_path)
    services.jellyfin = lambda: None
    monkeypatch.setattr(filmimport, "_remove", lambda folder: (_ for _ in ()).throw(PermissionError("locked")))
    confirm(services, at(row, 24))
    assert row_of(setup, rip)["state"] == "imported"
    monkeypatch.undo()
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.0)
    confirm(services, at(row, 25))
    assert row_of(setup, rip)["state"] == "in_jellyfin"


def test_confirm_skips_rip_with_flow_running(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    services.jellyfin = lambda: None

    async def go():
        async with services.import_lock(rip):
            await filmimport.confirm_and_clean(services, at(row, 24))

    asyncio.run(go())
    assert row_of(setup, rip)["state"] == "imported"


def test_restart_marks_importing_failed(config, setup, tmp_path):
    rip, _ = make_rip(setup, config)
    film_imports.ensure(setup, rip, "t")
    film_imports.update(setup, rip, "t", state="importing", tmdb_id=1, step="import")
    services, _ = services_with(config, tmp_path)
    asyncio.run(filmimport.resume_after_restart(services))
    row = row_of(setup, rip)
    assert row["state"] == "failed" and row["error"] == "Interrupted by a restart; press Retry."


def test_restart_resumes_waiting_rips(config, setup, tmp_path):
    first, _ = make_rip(setup, config)
    second, _ = make_rip(setup, config)
    film_imports.ensure(setup, first, "t")
    film_imports.update(setup, first, "t", state="importing", tmdb_id=137106, step="import")
    film_imports.ensure(setup, second, "t")
    film_imports.update(setup, second, "t", state="waiting_other", tmdb_id=137106, step="claim")
    services, fake = services_with(config, tmp_path)

    async def go():
        await filmimport.resume_after_restart(services)
        await asyncio.gather(*services._import_tasks)

    asyncio.run(go())
    assert row_of(setup, first)["state"] == "failed"
    assert row_of(setup, second)["state"] == "imported", row_of(setup, second)["error"]


def test_raw_sweep_skips_imported_rips(config, setup, tmp_path):
    rip, services, row = imported(config, setup, tmp_path)
    assert row["state"] == "imported"
    refusal = services.delete_raw_sync(setup, rip, 0, now=datetime.now(timezone.utc), keep_hours=None)
    assert "cleaned up once Jellyfin has the film" in refusal
    assert (config.downloads_dir / str(rip) / "title_t00.mkv").exists()
    assert services._sweep_raw(setup) == 0
