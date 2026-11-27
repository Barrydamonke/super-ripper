import asyncio
import shutil

import pytest

from disc_ripper import film_imports, jobs, video_rips
from disc_ripper.services import Services

from .test_web_films import FP, films, make_rip, queue  # noqa: F401


def services_for(config):
    return Services(config)


def make_work_file(config, job_id):
    path = config.work_dir / str(job_id) / "title_t00.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    return path


def count(conn, table, rip):
    col = "id" if table == "jobs" else "rip_id"
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (rip,)).fetchone()[0]


def test_remove_rip_deletes_folder_rows_and_work(films):
    client, conn, config = films
    rip = make_rip(conn, config, names=("film_t01.mkv", "extras_t02.mkv"))
    out = config.downloads_dir / str(rip) / "out.mkv"
    out.write_bytes(b"y")
    eid = queue(conn, rip, 0, state="done", output=str(out))
    (config.downloads_dir / str(rip) / video_rips.PARTIAL).mkdir()
    work_file = make_work_file(config, rip)
    enc_work = config.work_dir / f"encode-{eid}" / "tmp.mkv"
    enc_work.parent.mkdir(parents=True)
    enc_work.write_bytes(b"z")
    film_imports.ensure(conn, rip, "t")
    with conn:
        conn.execute("UPDATE film_imports SET state = 'skipped' WHERE rip_id = ?", (rip,))

    assert asyncio.run(services_for(config).remove_rip(rip)) is None
    assert not (config.downloads_dir / str(rip)).exists()
    assert not work_file.parent.exists() and not enc_work.parent.exists()
    assert count(conn, "video_rips", rip) == 0 and count(conn, "encodes", rip) == 0
    assert count(conn, "film_imports", rip) == 0
    assert count(conn, "jobs", rip) == 1
    assert conn.execute("SELECT COUNT(*) FROM video_discs WHERE fingerprint = ?", (FP,)).fetchone()[0] == 1


def test_remove_clears_move_failed_banner(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    with conn:
        conn.execute("UPDATE video_rips SET state = 'move_failed', message = ? WHERE rip_id = ?",
                     ("Couldn't move to downloads: boom", rip))
        conn.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (rip,))
    make_work_file(config, rip)
    before = client.get("/").text
    assert "move to downloads: boom" in before and "remove it on the Films page" in before
    r = client.post("/films/remove", data={"rip_id": rip})
    assert r.status_code == 200
    assert "move to downloads" not in client.get("/").text


def _refused(films, text, setup):
    client, conn, config = films
    rip = make_rip(conn, config)
    setup(conn, rip)
    assert asyncio.run(services_for(config).remove_rip(rip)) == text.format(rip=rip)
    assert count(conn, "video_rips", rip) == 1
    assert (config.downloads_dir / str(rip)).exists()


def test_remove_refused_while_ripping(films):
    def go(conn, rip):
        with conn:
            conn.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (rip,))
    _refused(films, "Rip {rip} is still ripping; cancel it on the drive's card first.", go)


def test_remove_refused_while_encoding(films):
    _refused(films, "Rip {rip} is still encoding; cancel the encode first.",
             lambda conn, rip: queue(conn, rip, 0, state="running"))


@pytest.mark.parametrize("state", ["importing", "identifying"])
def test_remove_refused_while_importing(films, state):
    def go(conn, rip):
        film_imports.ensure(conn, rip, "t")
        with conn:
            conn.execute("UPDATE film_imports SET state = ? WHERE rip_id = ?", (state, rip))
    _refused(films, "Rip {rip} is being imported; wait for it to finish.", go)


def test_remove_refused_while_import_lock_held(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    services = services_for(config)

    async def go():
        async with services.import_lock(rip):
            return await services.remove_rip(rip)

    assert asyncio.run(go()) == f"Rip {rip} is being imported; wait for it to finish."
    assert count(conn, "video_rips", rip) == 1


def test_remove_cancels_queued_encodes_and_jobs(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    with conn:
        conn.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (rip,))
    queued_job = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": rip, "title": 1}, uses_drive=True, now="t")
    waiting_job = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": rip, "title": 2}, uses_drive=True, now="t")
    with conn:
        conn.execute("UPDATE jobs SET state = 'waiting_space' WHERE id = ?", (waiting_job,))
    eid = queue(conn, rip, 0)
    assert asyncio.run(services_for(config).remove_rip(rip)) is None
    states = {r["id"]: r["state"] for r in conn.execute("SELECT id, state FROM jobs")}
    assert states[queued_job] == "cancelled" and states[waiting_job] == "cancelled"
    assert conn.execute("SELECT COUNT(*) FROM encodes WHERE id = ?", (eid,)).fetchone()[0] == 0


def test_remove_imported_film_keeps_library_file(films, tmp_path):
    client, conn, config = films
    rip = make_rip(conn, config)
    lib = tmp_path / "library" / "Film (2020)" / "Film (2020).mkv"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"film")
    film_imports.ensure(conn, rip, "t")
    with conn:
        conn.execute("UPDATE film_imports SET state = 'imported', new_path = ? WHERE rip_id = ?", (str(lib), rip))
    assert "stays in your library" in client.get("/films").text
    assert asyncio.run(services_for(config).remove_rip(rip)) is None
    assert lib.read_bytes() == b"film"
    assert count(conn, "film_imports", rip) == 0


def test_delete_failure_keeps_rows(films, monkeypatch):
    client, conn, config = films
    rip = make_rip(conn, config)

    def boom(path, *a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(shutil, "rmtree", boom)
    result = asyncio.run(services_for(config).remove_rip(rip))
    assert result.startswith("Couldn't delete ") and "denied" in result
    assert count(conn, "video_rips", rip) == 1


def test_reinsert_after_remove_is_not_already_ripped(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    assert video_rips.blocking_rip(conn, FP, config.downloads_dir, config.work_dir) is not None
    assert asyncio.run(services_for(config).remove_rip(rip)) is None
    assert video_rips.blocking_rip(conn, FP, config.downloads_dir, config.work_dir) is None


def test_web_remove_button_and_post(films):
    client, conn, config = films
    rip = make_rip(conn, config, label="MY_DISC")
    text = client.get("/films").text
    assert 'hx-post="/films/remove"' in text and "hx-confirm" in text
    assert f"Remove rip {rip} (MY_DISC)? Its folder in downloads is deleted" in text
    r = client.post("/films/remove", data={"rip_id": rip})
    assert r.status_code == 200 and f"Rip {rip} removed." in r.text
    with conn:
        conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, created_at, updated_at) "
                     "VALUES (?, ?, 'ripped', '2026-11-21T10:00:00+00:00', 't')", (rip, FP))
        conn.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (rip,))
    r = client.post("/films/remove", data={"rip_id": rip})
    assert r.status_code == 409 and "still ripping" in r.text
