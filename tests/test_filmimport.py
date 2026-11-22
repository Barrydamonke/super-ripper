import asyncio
import dataclasses
import json

import pytest

from disc_ripper import encodepool, encodes, film_imports, filmimport, jobs, makemkv, video_rips

from .fake_radarr import LEGO, LION_1994, LION_2019, FakeRadarr
from .test_encodepool import FakeSpace, Notifier, sync  # noqa: F401
from .test_videorip_rip import make_services  # noqa: F401
from .test_videorip_scan import setup  # noqa: F401

NOW = "2026-11-22T10:00:00+00:00"


@pytest.fixture(autouse=True)
def instant_polls(monkeypatch):
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.0)


def make_rip(conn, config, *, main=0, label="The Lego Movie", disc_label="THE_LEGO_MOVIE_D1", seconds=6000,
             encode=True, preset="standard", extras=()):
    """A ripped disc: raw file, one done encode of the main title, and (title, name) extras."""
    rip = jobs.enqueue(conn, "serial:A", "rip_title", {}, uses_drive=True, now="t")
    titles = [dataclasses.asdict(makemkv.Title(number=n, seconds=seconds if n == 0 else 300, size=1000, chapters=1,
                                               source="", output_name=f"t{n}.mkv", segments="", name=""))
              for n in range(1 + len(extras))]
    fp = f"fp{rip}"
    video_rips.save_scan(conn, fp, "dvd", disc_label, {"label": label, "titles": titles}, 120, [], None, NOW)
    folder = config.downloads_dir / str(rip)
    folder.mkdir(parents=True)
    with conn:
        conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, main_title, created_at, updated_at) "
                     "VALUES (?, ?, 'ripped', ?, ?, ?)", (rip, fp, main, NOW, NOW))
    raw = folder / "title_t00.mkv"
    raw.write_bytes(b"r" * 100)
    video_rips.record_file(conn, rip, 0, raw, "main", NOW)
    eid = None
    if encode:
        eid = add_encode(conn, rip, 0, folder / "enc", f"{label}.mkv", raw, seconds, "main", preset)
    for n, name in extras:
        path = folder / name
        path.write_bytes(b"x" * 50)
        video_rips.record_file(conn, rip, n, path, "extra", NOW)
    return rip, eid


def add_encode(conn, rip, title, folder, name, raw, seconds, role, preset="standard", disc_type="dvd"):
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / name
    out.write_bytes(b"e" * 200)
    eid = encodes.create(conn, rip_id=rip, title=title, role=role, source=raw, disc_type=disc_type,
                         source_seconds=seconds, source_bytes=100, source_audio=2, preset=preset, now=NOW)
    encodes.start(conn, eid, NOW)
    encodes.finish(conn, eid, "done", NOW, output=str(out), output_bytes=200)
    return eid


def services_with(config, tmp_path, fake=None):
    services = make_services(config)
    fake = fake or FakeRadarr(tmp_path)
    services.radarr = lambda: fake
    return services, fake


def row_of(conn, rip):
    return film_imports.get(conn, rip)


def import_log(config, rip):
    return (config.config_dir / "logs" / f"import-{rip}.log").read_text(encoding="utf-8")


def test_confident_new_film_imports(config, setup, tmp_path):
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    folder = fake.root / "The Lego Movie (2014)"
    assert (folder / "The Lego Movie.mkv").is_file()
    movie = fake.movies[row["movie_id"]]
    assert movie["monitored"] is False and movie["addOptions"] == {"searchForMovie": False}
    assert movie["qualityProfileId"] == 1
    assert row["new_path"] == str(folder / "The Lego Movie.mkv") and row["tmdb_id"] == 137106
    assert (row["title"], row["year"], row["root_folder"], row["encode_id"]) == ("The Lego Movie", 2014, "Movies", eid)
    enc = encodes.get(setup, eid)
    assert enc["imported_path"] == row["new_path"] and enc["imported_at"]
    assert row["imported_at"]
    command = next(iter(fake.commands.values()))
    [entry] = command["files"]
    assert command["importMode"] == "move" and entry["quality"]["quality"]["name"] == "DVD"
    text = import_log(config, rip)
    assert "test-key" not in text
    assert "POST /api/v3/command -> ok" in text and "GET /api/v3/rootfolder -> ok" in text


def test_log_records_failed_calls(config, setup, tmp_path):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    fake.fail_once.add("command_status")
    asyncio.run(filmimport.run_import(services, rip))
    text = import_log(config, rip)
    assert "GET /api/v3/command/" in text and "didn't answer" in text


def test_unsure_waits_with_candidates(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, label="The Lion King", disc_label="LION_KING", seconds=4000)
    services, fake = services_with(config, tmp_path, FakeRadarr(tmp_path, [LION_1994, LION_2019]))
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "needs_identify"
    cands = json.loads(row["candidates"])
    assert {c["tmdb_id"] for c in cands} == {8587, 420818} and len(cands) <= 5
    assert "add_movie" not in fake.calls


def test_tv_rip_skipped(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, main=None)
    services, fake = services_with(config, tmp_path)
    asyncio.run(filmimport.run_import(services, rip))
    assert row_of(setup, rip)["state"] == "tv_skipped"
    assert fake.calls == []


def test_choose_film_by_tmdb_id(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, label="The Lion King", disc_label="LION_KING", seconds=4000)
    services, fake = services_with(config, tmp_path, FakeRadarr(tmp_path, [LION_1994, LION_2019, LEGO]))
    asyncio.run(filmimport.run_import(services, rip))
    assert row_of(setup, rip)["state"] == "needs_identify"
    assert asyncio.run(filmimport.choose_film(services, rip, "8587")) is None
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert (row["tmdb_id"], row["year"]) == (8587, 1994)
    assert (fake.root / "The Lion King (1994)" / "The Lion King.mkv").is_file()


def test_choose_film_by_imdb_link_and_bad_ref(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, label="Unknown", disc_label="X", seconds=4000)
    services, fake = services_with(config, tmp_path, FakeRadarr(tmp_path, [LION_1994]))
    asyncio.run(filmimport.run_import(services, rip))
    assert row_of(setup, rip)["state"] == "needs_identify"
    assert asyncio.run(filmimport.choose_film(services, rip, "nonsense")) is not None
    assert asyncio.run(filmimport.choose_film(services, rip, "https://imdb.com/title/tt0110357/")) is None
    assert row_of(setup, rip)["tmdb_id"] == 8587


def test_second_rip_of_same_film_waits(config, setup, tmp_path):
    first, _ = make_rip(setup, config)
    second, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    film_imports.ensure(setup, first, NOW)
    film_imports.update(setup, first, NOW, state="importing", tmdb_id=137106)
    asyncio.run(filmimport.run_import(services, second))
    row = row_of(setup, second)
    assert row["state"] == "waiting_other" and "Another rip" in row["message"]
    assert "add_movie" not in fake.calls


def test_existing_file_waits_for_replace_step(config, setup, tmp_path):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    movie = fake.add_movie(fake.lookup_tmdb(137106), str(fake.root), 1)
    fake.movies[movie["id"]]["hasFile"] = True
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "needs_replace_ok" and "already in Radarr" in row["message"]
    assert "command" not in fake.calls


def test_no_finished_encode_fails(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, encode=False)
    services, fake = services_with(config, tmp_path)
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "failed" and row["error"] == "No finished encode of the film."


def test_radarr_down_mid_import_fails_and_retry_resumes(config, setup, tmp_path):
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    fake.fail_once.add("command")
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "failed" and row["step"] == "import" and "Radarr didn't answer" in row["error"]
    assert fake.calls.count("add_movie") == 1
    assert asyncio.run(filmimport.retry_import(services, rip)) is None
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert fake.calls.count("add_movie") == 1
    assert fake.movies[row["movie_id"]]["monitored"] is False


def test_retry_after_command_ran_but_poll_failed(config, setup, tmp_path):
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    fake.fail_once.add("command_status")
    asyncio.run(filmimport.run_import(services, rip))
    assert row_of(setup, rip)["state"] == "failed"
    asyncio.run(filmimport.retry_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert fake.calls.count("command") == 1  # the file was already moved: not imported twice
    assert encodes.get(setup, eid)["imported_path"] == row["new_path"]


def test_import_timeout_fails(config, setup, tmp_path, monkeypatch):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    fake.command_status = lambda cid: "started"
    monkeypatch.setattr(filmimport, "IMPORT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.01)
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "failed" and "too long" in row["error"]


def test_one_flow_per_rip_at_a_time(config, setup, tmp_path):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    fake.block_on = "manual_import_view"

    async def main():
        task = asyncio.create_task(filmimport.run_import(services, rip))
        while not fake.entered.is_set():
            await asyncio.sleep(0.01)
        assert await filmimport.retry_import(services, rip) == filmimport.BUSY
        assert await filmimport.choose_film(services, rip, "137106") == filmimport.BUSY
        fake.release.set()
        await task

    asyncio.run(main())
    assert row_of(setup, rip)["state"] == "imported"
    assert fake.calls.count("command") == 1


def test_extras_copied_into_extras_folder(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, extras=[(1, "title_t01.mkv"), (2, "title_t02.mkv")])
    folder = config.downloads_dir / str(rip)
    add_encode(setup, rip, 2, folder / "enc", "bonus.mkv", folder / "title_t02.mkv", 300, "extra")
    services, fake = services_with(config, tmp_path)
    existing = fake.root / "The Lego Movie (2014)" / "extras"
    existing.mkdir(parents=True)
    (existing / "title_t01.mkv").write_bytes(b"keep")
    asyncio.run(filmimport.run_import(services, rip))
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert (existing / "title_t01.mkv").read_bytes() == b"keep"  # never overwritten
    assert (existing / "bonus.mkv").read_bytes() == b"e" * 200  # the extra's encode, not its raw
    assert not (existing / "title_t02.mkv").exists()
    assert (folder / "title_t01.mkv").exists()  # copied: the source stays
    assert str(existing / "bonus.mkv") in json.loads(row["extras"])
    assert "already exists" in import_log(config, rip)


def test_encode_done_triggers_import(config, setup, tmp_path):
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)

    async def main():
        await services.encode_done(eid)
        await asyncio.gather(*services._import_tasks)
        await services.encode_done(eid)  # an imported rip is left alone
        assert not services._import_tasks

    asyncio.run(main())
    assert row_of(setup, rip)["state"] == "imported"
    assert fake.calls.count("command") == 1


def test_encode_done_ignores_extra_titles(config, setup, tmp_path):
    rip, _ = make_rip(setup, config, extras=[(1, "title_t01.mkv")])
    folder = config.downloads_dir / str(rip)
    extra = add_encode(setup, rip, 1, folder / "enc", "x.mkv", folder / "title_t01.mkv", 300, "extra")
    services, fake = services_with(config, tmp_path)
    asyncio.run(services.encode_done(extra))
    assert row_of(setup, rip) is None and not services._import_tasks


def test_no_radarr_configured_does_nothing(config, setup):
    rip, eid = make_rip(setup, config)
    services = make_services(config)
    assert services.radarr() is None
    asyncio.run(services.encode_done(eid))
    asyncio.run(filmimport.run_import(services, rip))
    assert row_of(setup, rip) is None


def test_radarr_built_from_settings(config, setup):
    services = make_services(config)
    with setup:
        setup.execute("INSERT INTO settings (key, value) VALUES ('radarr_url', '\"http://r:7878\"')")
        setup.execute("INSERT INTO settings (key, value) VALUES ('radarr_api_key', '\"test-key\"')")
    assert services.radarr().base == "http://r:7878"


class Hook:
    def __init__(self, fail=False):
        self.seen, self.fail = [], fail

    async def encode_done(self, encode_id):
        self.seen.append(encode_id)
        if self.fail:
            raise RuntimeError("boom")


@sync
async def test_encode_pool_tells_services_when_done(config, setup):
    hook = Hook()
    pool = encodepool.EncodePool(config, Notifier(), FakeSpace(), services=hook)
    await pool._finish(5, "done", None)
    await pool._finish(6, "failed", "x")
    assert hook.seen == [5]


@sync
async def test_encode_pool_survives_failing_hook(config, setup):
    hook = Hook(fail=True)
    pool = encodepool.EncodePool(config, Notifier(), FakeSpace(), services=hook)
    await pool._finish(5, "done", None)
    assert hook.seen == [5]
