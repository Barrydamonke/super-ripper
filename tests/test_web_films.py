import json
from pathlib import Path

import pytest

from disc_ripper import db, encodes, jobs, video_rips

FP = "fp1"
CONFIRM = "Delete the raw rip? It can't be undone; the disc is the backup."


@pytest.fixture
def films(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    logged_in_client.post("/drives/rescan")
    conn = db.connect(config.db_path)
    yield logged_in_client, conn, config
    conn.close()


def make_rip(conn, config, *, label="THE_LEGO_MOVIE", disc_type="bluray", names=("film_t01.mkv",), fp=FP):
    scan = {"titles": [{"number": n, "seconds": 6035, "audio": 2} for n in range(len(names))]}
    video_rips.save_scan(conn, fp, disc_type, label, json.dumps(scan), 120, [], "serial:A", "2026-11-21T10:00:00+00:00")
    job = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": None, "title": 0}, uses_drive=True, now="t")
    with conn:
        conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, created_at, updated_at) "
                     "VALUES (?, ?, 'ripped', '2026-11-21T10:00:00+00:00', 't')", (job, fp))
        conn.execute("UPDATE jobs SET params = ? WHERE id = ?", (json.dumps({"rip_id": job, "title": 0}), job))
    for n, name in enumerate(names):
        path = config.downloads_dir / str(job) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 1000)
        video_rips.record_file(conn, job, n, path, "main" if n == 0 else "extra", "t")
    return job


def queue(conn, rip, title=0, preset="standard", state="queued", error=None, output=None):
    path = json.loads(conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip,)).fetchone()[0])[str(title)]["path"]
    eid = encodes.create(conn, rip_id=rip, title=title, role="main", source=Path(path), disc_type="bluray",
                         source_seconds=6035.0, source_bytes=1000, source_audio=2, preset=preset, now="t")
    if state != "queued":
        encodes.start(conn, eid, "t")
        if state != "running":
            encodes.finish(conn, eid, state, "2026-11-21T10:00:00+00:00", error=error, output=output,
                           output_bytes=500 if output else None)
    return eid


def test_films_page_lists_rips_files_and_encodes(films):
    client, conn, config = films
    rip = make_rip(conn, config, names=("film_t01.mkv", "extras_t02.mkv"))
    queue(conn, rip, 0)
    r = client.get("/films")
    assert r.status_code == 200
    assert 'sse-swap="films"' in r.text and 'sse-connect="/events"' in r.text
    for text in ("THE_LEGO_MOVIE", "Blu-ray", "film_t01.mkv", "extras_t02.mkv", "1:40:35", "Queued", "(rough)",
                 f"/jobs/{rip}/log", "Encode all", "Standard"):
        assert text in r.text, text


def test_films_empty(films):
    client, _, _ = films
    assert "No DVD or Blu-ray rips yet." in client.get("/films").text


def test_encode_button_queues(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    r = client.post("/films/encode", data={"rip_id": rip, "title": 0, "preset": "fast"})
    assert r.status_code == 200 and "<html" not in r.text
    [row] = conn.execute("SELECT * FROM encodes").fetchall()
    assert (row["preset"], row["state"]) == ("fast", "queued")
    r = client.post("/films/encode", data={"rip_id": rip, "title": 0, "preset": "fast"})
    assert r.status_code == 409 and "already has an encode" in r.text
    assert client.post("/films/encode", data={"rip_id": rip, "title": 0, "preset": "bogus"}).status_code == 400


def test_encode_all(films):
    client, conn, config = films
    rip = make_rip(conn, config, names=("a.mkv", "b.mkv"))
    r = client.post("/films/encode-all", data={"rip_id": rip, "preset": "copy"})
    assert "Queued 2 of 2" in r.text
    assert conn.execute("SELECT COUNT(*) FROM encodes").fetchone()[0] == 2
    r = client.post("/films/encode-all", data={"rip_id": rip, "preset": "copy"})
    assert "Queued 0 of 2" in r.text and "already has an encode" in r.text


def test_failed_encode_shows_reason_retry_and_log(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    eid = queue(conn, rip, state="failed", error="HandBrake exited 3")
    r = client.get("/films")
    assert "HandBrake exited 3" in r.text
    assert f'hx-post="/encodes/{eid}/retry"' in r.text and f'href="/encodes/{eid}/log"' in r.text
    r = client.post(f"/encodes/{eid}/retry")
    assert r.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM encodes WHERE state = 'queued'").fetchone()[0] == 1
    assert client.post(f"/encodes/{eid}/retry").status_code == 409  # a newer one is already queued
    assert client.post("/encodes/999/retry").status_code == 404


def test_cancel_queued_encode(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    eid = queue(conn, rip)
    assert client.post(f"/encodes/{eid}/cancel").status_code == 200
    assert conn.execute("SELECT state FROM encodes WHERE id = ?", (eid,)).fetchone()[0] == "cancelled"


def test_delete_raw_now_confirm_and_refusal(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    raw = Path(json.loads(conn.execute("SELECT files FROM video_rips").fetchone()[0])["0"]["path"])
    r = client.post("/films/delete-raw", data={"rip_id": rip, "title": 0})
    assert r.status_code == 409 and "until an encode has finished" in r.text and raw.exists()
    out = raw.parent / "film_t01 - Standard.mkv"
    out.write_bytes(b"y" * 500)
    queue(conn, rip, state="done", output=str(out))
    page = client.get("/films").text
    assert f'hx-confirm="{CONFIRM}"' in page and "Delete raw now" in page and "film_t01 - Standard.mkv" in page
    r = client.post("/films/delete-raw", data={"rip_id": rip, "title": 0})
    assert r.status_code == 200 and not raw.exists() and out.exists()
    assert "raw deleted" in client.get("/films").text


def test_encode_log_route(films):
    client, _, config = films
    assert client.get("/encodes/7/log").status_code == 404
    assert client.get("/encodes/7/log").text == "No log for encode #7"
    (config.config_dir / "logs").mkdir(parents=True, exist_ok=True)
    (config.config_dir / "logs" / "encode-7.log").write_text("hello log", encoding="utf-8")
    r = client.get("/encodes/7/log")
    assert r.status_code == 200 and r.text == "hello log" and r.headers["content-type"].startswith("text/plain")


def test_dashboard_shows_encode_panel(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    queue(conn, rip)
    r = client.get("/")
    assert 'sse-swap="encodes"' in r.text and "Queued: #" in r.text and "film_t01.mkv" in r.text


def test_nav_has_films(films):
    client, _, _ = films
    assert 'href="/films"' in client.get("/").text


def test_films_escape_disc_text(films):
    client, conn, config = films
    rip = make_rip(conn, config, label="<script>x</script>", names=("a&b.mkv",))
    queue(conn, rip, state="failed", error="<i>boom</i>")
    r = client.get("/films")
    assert "<script>x" not in r.text and "&lt;script&gt;x" in r.text
    assert "a&amp;b.mkv" in r.text
    assert "<i>boom" not in r.text


def test_services_render_for_sse(films):
    client, conn, config = films
    rip = make_rip(conn, config)
    queue(conn, rip)
    services = client.app.state.services
    assert "Rip " in services._render_films() and "Queued: #" in services._render_encodes()
