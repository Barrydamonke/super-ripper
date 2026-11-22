import asyncio
import json
from pathlib import Path

import pytest

from disc_ripper import encodes, film_imports, filmimport, filmundo, settings, videofiles

from .fake_radarr import LEGO, FakeRadarr
from .test_encodepool import FakeSpace, Notifier, sync  # noqa: F401
from .test_filmimport import NOW, make_rip, row_of, services_with
from .test_videorip_rip import make_services  # noqa: F401
from .test_videorip_scan import setup  # noqa: F401

ELF = {"title": "Elf", "year": 2003, "runtime": 97, "tmdbId": 10719, "imdbId": "tt0319343",
       "remotePoster": "", "folder": "Elf (2003)"}


@pytest.fixture(autouse=True)
def instant_polls(monkeypatch):
    monkeypatch.setattr(filmimport, "POLL_SECONDS", 0.0)


def probe(monkeypatch, seconds=6000, width=720, height=480):
    monkeypatch.setattr(videofiles, "PROBE", lambda path: videofiles.MkvInfo(
        seconds=seconds, video=1, audio=1, audio_channels=(2,), width=width, height=height,
        video_codec="h264", audio_codec="aac"))


def seed(fake, lookup=LEGO, folder=None, resolution="720x480", runtime="1:40:00", name="Old.mkv"):
    """A movie already in Radarr with a file (in `folder`, default Radarr's own name)."""
    movie = fake.add_movie(fake.lookup_tmdb(lookup["tmdbId"]), str(fake.root), 1)
    stored = fake.movies[movie["id"]]
    if folder:
        stored["path"] = str(fake.root / folder)
    Path(stored["path"]).mkdir(parents=True)
    old = Path(stored["path"]) / name
    old.write_bytes(b"o" * 90)
    stored["hasFile"] = True
    stored["movieFile"] = {"path": str(old), "size": 90,
                           "mediaInfo": {"resolution": resolution, "runTime": runtime}}
    return stored, old


def ask_off(conn, **more):
    settings.save(conn, {"ask_before_replace": False, **more})


def start(config, setup, tmp_path, monkeypatch, lookup=LEGO, label="The Lego Movie", seconds=6000, **seed_args):
    probe(monkeypatch, seconds=seconds)
    catalog = [lookup]
    rip, eid = make_rip(setup, config, label=label, disc_label=label.upper().replace(" ", "_"), seconds=seconds)
    services, fake = services_with(config, tmp_path, FakeRadarr(tmp_path, catalog))
    movie, old = seed(fake, lookup, **seed_args)
    return rip, eid, services, fake, movie, old


def run(services, rip):
    asyncio.run(filmimport.run_import(services, rip))


def test_replace_waits_when_ask_on(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "needs_replace_ok" and "already in Radarr" in row["message"]
    replace = json.loads(row["replace"])
    assert replace["checks"] == {"length": True, "recycle_bin": True, "resolution": True}
    assert replace["old"]["path"] == str(old) and replace["old"]["resolution"] == "720x480"
    assert replace["new"]["resolution"] == "720x480" and replace["reasons"] == {}
    assert "command" not in fake.calls and old.is_file()
    assert asyncio.run(filmimport.approve_replace(services, rip)) is None
    assert row_of(setup, rip)["state"] == "imported"
    assert not old.exists()


def test_replace_auto_when_ask_off_and_checks_pass(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert row["old_path"] == str(old) and row["old_folder"] == movie["path"]
    assert row["recycled_path"] == str(fake.recycle / "Old.mkv") and (fake.recycle / "Old.mkv").is_file()
    assert Path(row["new_path"]).name == "The Lego Movie.mkv" and Path(row["new_path"]).is_file()


def test_length_mismatch_waits(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch, runtime="2:00:00")
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    replace = json.loads(row["replace"])
    assert row["state"] == "needs_replace_ok"
    assert replace["checks"]["length"] is False and "length" in replace["reasons"]
    assert replace["checks"]["resolution"] is True and "command" not in fake.calls


def test_missing_old_value_fails_that_check(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    movie["movieFile"]["mediaInfo"] = {}
    movie["runtime"] = 0
    ask_off(setup)
    run(services, rip)
    replace = json.loads(row_of(setup, rip)["replace"])
    assert replace["checks"]["length"] is False and replace["checks"]["resolution"] is False
    assert replace["reasons"]["length"] and replace["reasons"]["resolution"]


def test_downgrade_always_waits(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch, resolution="1920x1080")
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    replace = json.loads(row["replace"])
    assert row["state"] == "needs_replace_ok"
    assert replace["checks"]["resolution"] is False and "resolution" in replace["reasons"]
    assert old.is_file() and "command" not in fake.calls
    assert asyncio.run(filmimport.approve_replace(services, rip)) is None  # the owner's press is the decision
    assert row_of(setup, rip)["state"] == "imported"


def test_no_recycle_bin_needs_typed_replace(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    fake.bin_set = False
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "needs_replace_ok" and json.loads(row["replace"])["checks"]["recycle_bin"] is False
    refusal = asyncio.run(filmimport.approve_replace(services, rip))
    assert refusal and "REPLACE" in refusal
    assert row_of(setup, rip)["state"] == "needs_replace_ok" and old.is_file()
    assert asyncio.run(filmimport.approve_replace(services, rip, "replace")) is not None
    assert asyncio.run(filmimport.approve_replace(services, rip, "REPLACE")) is None
    row = row_of(setup, rip)
    assert row["state"] == "imported" and row["recycled_path"] is None


def test_folder_tidied_after_replace(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(
        config, setup, tmp_path, monkeypatch, lookup=ELF, label="Elf", seconds=5820, folder="Elf")
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    target = fake.root / "Elf (2003)"
    assert fake.movies[movie["id"]]["path"] == str(target)
    assert row["new_path"] == str(target / "Elf.mkv") and Path(row["new_path"]).is_file()
    assert not (fake.root / "Elf").exists()
    assert row["old_folder"] == str(fake.root / "Elf")
    assert encodes.get(setup, row["encode_id"])["imported_path"] == row["new_path"]


def test_tidy_off_keeps_folder(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(
        config, setup, tmp_path, monkeypatch, lookup=ELF, label="Elf", seconds=5820, folder="Elf")
    ask_off(setup, tidy_folders_on_replace=False)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert fake.movies[movie["id"]]["path"] == str(fake.root / "Elf")
    assert Path(row["new_path"]).parent == fake.root / "Elf"


def test_undo_new_film(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config, extras=[(1, "extra.mkv")])
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    film_imports.update(setup, rip, NOW, candidates=[{"tmdb_id": 1}])
    folder = Path(row["new_path"]).parent
    assert (folder / "extras").is_dir()
    output = Path(encodes.get(setup, eid)["output"])
    assert not output.exists()
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    row = row_of(setup, rip)
    assert row["state"] == "needs_identify" and json.loads(row["candidates"]) == [{"tmdb_id": 1}]
    assert output.is_file() and not folder.exists()
    assert fake.movies == {}
    enc = encodes.get(setup, eid)
    assert enc["imported_path"] is None and enc["imported_at"] is None


def test_undo_replace_restores_old_file_and_folder(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = start(
        config, setup, tmp_path, monkeypatch, lookup=ELF, label="Elf", seconds=5820, folder="Elf")
    ask_off(setup)
    run(services, rip)
    assert row_of(setup, rip)["state"] == "imported"
    output = Path(encodes.get(setup, eid)["output"])
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    row = row_of(setup, rip)
    assert row["state"] == "needs_identify"
    assert old.is_file() and old.read_bytes() == b"o" * 90
    assert not (fake.root / "Elf (2003)").exists() and not (fake.recycle / "Old.mkv").exists()
    stored = fake.movies[movie["id"]]
    assert stored["path"] == str(fake.root / "Elf") and stored["movieFile"]["path"] == str(old)
    assert output.is_file()
    assert any(c["name"] == "RescanMovie" and c["movieId"] == movie["id"] for c in fake.commands.values())
    assert encodes.get(setup, eid)["imported_path"] is None


def test_undo_replace_when_bin_emptied(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    ask_off(setup)
    run(services, rip)
    (fake.recycle / "Old.mkv").unlink()  # Radarr emptied its recycle bin
    message = asyncio.run(filmundo.undo(services, rip))
    assert message.startswith("Undone.")
    assert "The old file is no longer in Radarr's recycle bin, so only the new file was taken back." in message
    assert Path(encodes.get(setup, eid)["output"]).is_file()
    assert not old.exists()
    assert row_of(setup, rip)["state"] == "needs_identify" and fake.movies[movie["id"]]["hasFile"] is False


def test_undo_refused_unless_imported(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    run(services, rip)  # waits for the owner
    assert asyncio.run(filmundo.undo(services, rip)) not in (None, "Undone.")
    assert row_of(setup, rip)["state"] == "needs_replace_ok" and old.is_file()
    assert asyncio.run(filmimport.approve_replace(services, rip + 99)) not in (None, "")


def test_skip_deletes_rip_folder(config, setup, tmp_path, monkeypatch):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    folder = config.downloads_dir / str(rip)
    assert folder.is_dir()
    assert asyncio.run(filmundo.skip(services, rip)) is None
    assert not folder.exists() and row_of(setup, rip)["state"] == "skipped"
    film_imports.update(setup, rip, NOW, state="importing")
    assert asyncio.run(filmundo.skip(services, rip)) is not None


def test_invisible_recycle_bin_needs_typed_replace(config, setup, tmp_path, monkeypatch):
    rip, _, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    fake.recycle.rmdir()  # Radarr reports a bin path this app can't see
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    replace = json.loads(row["replace"])
    assert row["state"] == "needs_replace_ok" and replace["checks"]["recycle_bin"] is False
    assert "isn't visible to the ripper" in replace["reasons"]["recycle_bin"] and str(fake.recycle) in replace["reasons"]["recycle_bin"]
    assert asyncio.run(filmimport.approve_replace(services, rip)) is not None
    assert asyncio.run(filmimport.approve_replace(services, rip, "REPLACE")) is None
    row = row_of(setup, rip)
    assert row["state"] == "imported" and row["recycled_path"] is None


def test_undo_retry_after_midway_failure(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = start(
        config, setup, tmp_path, monkeypatch, lookup=ELF, label="Elf", seconds=5820, folder="Elf")
    ask_off(setup)
    run(services, rip)
    assert row_of(setup, rip)["state"] == "imported"
    output = Path(encodes.get(setup, eid)["output"])
    fake.fail_once.add("command")  # RescanMovie fails after the files were moved
    first = asyncio.run(filmundo.undo(services, rip))
    assert first.startswith("Undo stopped") and row_of(setup, rip)["state"] == "imported"
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert output.is_file() and old.is_file()
    assert sorted(p.name for p in (fake.root / "Elf").iterdir()) == ["Old.mkv"]
    assert not (fake.root / "Elf (2003)").exists()
    assert row_of(setup, rip)["state"] == "needs_identify"


def test_undo_keeps_movie_owner_already_had(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    mine = fake.add_movie(fake.lookup_tmdb(137106), str(fake.root), 1)  # in Radarr, no file
    run(services, rip)
    assert row_of(setup, rip)["state"] == "imported"
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert mine["id"] in fake.movies and "delete_movie" not in fake.calls
    assert Path(encodes.get(setup, eid)["output"]).is_file()
    assert fake.movies[mine["id"]]["hasFile"] is False


def test_undo_refuses_to_overwrite(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    output = Path(encodes.get(setup, eid)["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"other")
    message = asyncio.run(filmundo.undo(services, rip))
    assert message == f"Undo stopped: {output} already exists"
    assert row_of(setup, rip)["state"] == "imported" and output.read_bytes() == b"other"
    assert fake.movies


def test_skip_refuses_imported_and_reports_delete_failure(config, setup, tmp_path, monkeypatch):
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    film_imports.ensure(setup, rip, NOW)
    film_imports.update(setup, rip, NOW, state="imported")
    assert asyncio.run(filmundo.skip(services, rip)) == "Use Undo first."
    film_imports.update(setup, rip, NOW, state="failed")

    def boom(path, ignore_errors=False, onerror=None):
        raise PermissionError("denied")

    monkeypatch.setattr(filmundo.shutil, "rmtree", boom)
    message = asyncio.run(filmundo.skip(services, rip))
    assert message.startswith("Couldn't delete ") and "denied" in message
    assert row_of(setup, rip)["state"] == "failed"


def test_undo_replace_same_filename(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch, name="The Lego Movie.mkv")
    ask_off(setup)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported" and row["new_path"] == row["old_path"] == str(old)
    output = Path(encodes.get(setup, eid)["output"])
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert output.read_bytes() == b"e" * 200
    assert old.read_bytes() == b"o" * 90 and not (fake.recycle / old.name).exists()


def test_undo_retry_removes_empty_folder_of_added_movie(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, _ = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    folder = Path(row_of(setup, rip)["new_path"]).parent
    fake.fail_once.add("delete_movie")
    assert asyncio.run(filmundo.undo(services, rip)).startswith("Undo stopped")
    assert folder.is_dir() and row_of(setup, rip)["new_path"] is None
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert not folder.exists()


def test_choose_different_film_after_replace_wait_clears_old_fields(config, setup, tmp_path, monkeypatch):
    from .fake_radarr import LION_1994
    rip, eid, services, fake, movie, old = start(config, setup, tmp_path, monkeypatch)
    fake.catalog.append(LION_1994)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "needs_replace_ok" and row["old_path"] == str(old)
    assert asyncio.run(filmimport.choose_film(services, rip, "8587")) is None
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    assert row["old_path"] is None and row["old_folder"] is None and json.loads(row["replace"]) == {}
    assert row["tmdb_id"] == 8587 and row["added_by_us"] == 1
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert [m["tmdbId"] for m in fake.movies.values()] == [137106]
    assert old.read_bytes() == b"o" * 90 and fake.movies[movie["id"]]["movieFile"]["path"] == str(old)


def test_choose_refused_after_file_imported(config, setup, tmp_path, monkeypatch):
    probe(monkeypatch)
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported"
    film_imports.update(setup, rip, NOW, state="failed", step="extras")  # failed after the file went in
    message = "This rip's file is already in the library; press Undo first."
    assert asyncio.run(filmimport.choose_film(services, rip, "137106")) == message
    row = row_of(setup, rip)
    assert row["state"] == "failed" and row["new_path"]
    film_imports.update(setup, rip, NOW, new_path=None)  # only the encode records the import
    assert asyncio.run(filmimport.choose_film(services, rip, "137106")) == message


def _tidy_setup(config, setup, tmp_path, monkeypatch):
    monkeypatch.setattr(filmimport, "FOLDER_MOVE_POLL_SECONDS", 0.0)
    rip, eid, services, fake, movie, old = start(
        config, setup, tmp_path, monkeypatch, lookup=ELF, label="Elf", seconds=5820, folder="Elf")
    ask_off(setup)
    return rip, eid, services, fake, movie, old


def test_tidy_waits_for_radarr_async_move(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = _tidy_setup(config, setup, tmp_path, monkeypatch)
    fake.move_delay = 3
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "imported", row["error"]
    target = fake.root / "Elf (2003)"
    assert row["new_path"] == str(target / "Elf.mkv") and Path(row["new_path"]).is_file()
    assert not fake._pending_moves and not (fake.root / "Elf").exists()


def test_undo_waits_for_radarr_async_folder_restore(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = _tidy_setup(config, setup, tmp_path, monkeypatch)
    run(services, rip)
    assert row_of(setup, rip)["state"] == "imported"
    output = Path(encodes.get(setup, eid)["output"])
    fake.move_delay = 3
    assert asyncio.run(filmundo.undo(services, rip)) == "Undone."
    assert output.is_file() and old.read_bytes() == b"o" * 90
    assert not (fake.root / "Elf (2003)").exists()
    assert fake.movies[movie["id"]]["path"] == str(fake.root / "Elf")


def test_tidy_move_timeout_fails_readably(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = _tidy_setup(config, setup, tmp_path, monkeypatch)
    monkeypatch.setattr(filmimport, "FOLDER_MOVE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(filmimport, "FOLDER_MOVE_POLL_SECONDS", 0.01)
    fake.move_delay = 10**6
    run(services, rip)
    row = row_of(setup, rip)
    assert row["state"] == "failed" and row["error"] == "Radarr didn't finish moving the film's folder."
    assert row["step"] == "unmonitor"


def test_undo_move_timeout_stops_with_row_unchanged(config, setup, tmp_path, monkeypatch):
    rip, eid, services, fake, movie, old = _tidy_setup(config, setup, tmp_path, monkeypatch)
    run(services, rip)
    monkeypatch.setattr(filmimport, "FOLDER_MOVE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(filmimport, "FOLDER_MOVE_POLL_SECONDS", 0.01)
    fake.move_delay = 10**6
    message = asyncio.run(filmundo.undo(services, rip))
    assert message.startswith("Undo stopped: Radarr didn't finish moving the film's folder.")
    assert row_of(setup, rip)["state"] == "imported"


def test_skip_refuses_while_an_encode_is_active(config, setup, tmp_path, monkeypatch):
    rip, eid = make_rip(setup, config)
    services, fake = services_with(config, tmp_path)
    other = encodes.create(setup, rip_id=rip, title=1, role="extra", source=Path("x.mkv"), disc_type="dvd",
                           source_seconds=300, source_bytes=1, source_audio=2, preset="standard", now=NOW)
    assert other is not None  # queued
    folder = config.downloads_dir / str(rip)
    message = "An encode of this rip is still running; cancel it first."
    assert asyncio.run(filmundo.skip(services, rip)) == message
    assert folder.is_dir()
    encodes.start(setup, other, NOW)  # running
    assert asyncio.run(filmundo.skip(services, rip)) == message
    encodes.finish(setup, other, "done", NOW, output="o", output_bytes=1)
    assert asyncio.run(filmundo.skip(services, rip)) is None and not folder.exists()
