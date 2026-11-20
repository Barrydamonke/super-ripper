import asyncio
import errno
import json

import pytest

from disc_ripper import db, drives, jobs, services
from disc_ripper.web import dashboard_routes

SERIAL = b"\x05\x80\x00\x01A"
KEY = services.drive_key("serial:A")


@pytest.fixture
def dash(logged_in_client, config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=SERIAL)
    assert logged_in_client.post("/drives/rescan").status_code == 200
    conn = db.connect(config.db_path)
    yield logged_in_client, conn
    conn.close()


class FakeDrive:
    def __init__(self, error=None):
        self.error = error
        self.ejected = 0

    def eject(self):
        if self.error:
            raise self.error
        self.ejected += 1

    def close_tray(self):
        pass


def test_root_is_dashboard(dash):
    client, _ = dash
    r = client.get("/")
    assert r.status_code == 200
    assert 'sse-connect="/events"' in r.text
    assert f'id="drive-{KEY}"' in r.text
    assert f'sse-swap="drive-{KEY}"' in r.text
    assert 'sse-swap="guard"' in r.text
    assert "/static/htmx-ext-sse.js" in r.text
    assert "No schedule could be read" in r.text


def test_dashboard_shows_media(dash):
    client, conn = dash
    drives.set_media(conn, "serial:A", "disc", "bluray", "THE_MATRIX", "t")
    r = client.get("/")
    assert "Disc in" in r.text and "Blu-ray" in r.text and "THE_MATRIX" in r.text


def test_dashboard_escapes_label(dash):
    client, conn = dash
    drives.set_media(conn, "serial:A", "disc", "data", "<script>x</script>", "t")
    r = client.get("/")
    assert "&lt;script&gt;x" in r.text
    assert "<script>x" not in r.text


def test_disabled_drive_not_on_dashboard(dash):
    client, conn = dash
    drives.update_drive(conn, "serial:A", label="", enabled=False)
    assert f'id="drive-{KEY}"' not in client.get("/").text


def test_queue_test_job(dash):
    client, conn = dash
    r = client.post("/jobs/test", data={"drive_id": "serial:A", "minutes": "20", "size_gb": "0"})
    assert r.status_code == 200
    assert "Queued" in r.text
    assert "<html" not in r.text
    [row] = conn.execute("SELECT * FROM jobs").fetchall()
    assert json.loads(row["params"]) == {"duration_s": 1200, "size_gb": 0.0}
    assert (row["kind"], row["needs_bytes"], row["uses_drive"]) == ("test", 0, 0)


def test_queue_test_job_size(dash):
    client, conn = dash
    client.post("/jobs/test", data={"drive_id": "serial:A", "minutes": "5", "size_gb": "12.5"})
    assert conn.execute("SELECT needs_bytes FROM jobs").fetchone()[0] == 12_500_000_000


@pytest.mark.parametrize(
    "minutes,size",
    [("0", "0"), ("361", "0"), ("x", "0"), ("20", "-1"), ("20", "501"), ("20", "nan")],
)
def test_queue_test_job_validation(dash, minutes, size):
    client, conn = dash
    r = client.post("/jobs/test", data={"drive_id": "serial:A", "minutes": minutes, "size_gb": size})
    assert r.status_code == 400
    assert 'class="error"' in r.text
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_queue_test_job_unknown_drive(dash):
    client, _ = dash
    r = client.post("/jobs/test", data={"drive_id": "serial:nope", "minutes": "5", "size_gb": "0"})
    assert r.status_code == 404


def test_cancel_and_retry_routes(dash):
    client, conn = dash
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 60, "size_gb": 0}, now="t")
    r = client.post(f"/jobs/{j}/cancel")
    assert r.status_code == 200
    assert jobs.get(conn, j)["state"] == "cancelled"
    assert "Retry" in r.text
    r = client.post(f"/jobs/{j}/retry")
    assert r.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0] == 1
    assert client.post("/jobs/9999/cancel").status_code == 404


def test_eject(dash):
    client, _ = dash
    fake = FakeDrive()
    client.app.state.services.drive_factory = lambda path: fake
    r = client.post("/dashboard/eject", data={"drive_id": "serial:A"})
    assert r.status_code == 200
    assert fake.ejected == 1


def test_eject_error_shown(dash):
    client, _ = dash
    fake = FakeDrive(OSError(errno.EBUSY, "Device or resource busy"))
    client.app.state.services.drive_factory = lambda path: fake
    r = client.post("/dashboard/eject", data={"drive_id": "serial:A"})
    assert "Eject failed: Device or resource busy" in r.text


def test_busy_endpoint_unset_secret(client):
    r = client.get("/api/busy", headers={"X-Shutdown-Secret": "anything"})
    assert r.status_code == 503
    assert r.json() == {"error": "BUSY_SECRET not set"}


def test_busy_endpoint_auth(config):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from disc_ripper.app import create_app

    with TestClient(create_app(replace(config, busy_secret="s3cret")), follow_redirects=False) as c:
        assert c.get("/api/busy").status_code == 401
        assert c.get("/api/busy", headers={"X-Shutdown-Secret": "wrong"}).status_code == 401
        r = c.get("/api/busy", headers={"X-Shutdown-Secret": "s3cret"})
        assert r.status_code == 200  # no login cookie needed
        assert r.json()["busy"] is False
        assert r.json()["phase"] == "no_schedule"


def test_events_requires_login(client):
    r = client.get("/events")
    assert r.status_code == 303


def test_event_stream_initial_burst(dash):
    client, _ = dash
    svc = client.app.state.services

    class FakeRequest:
        async def is_disconnected(self):
            return True

    async def main():
        chunks = []
        async for chunk in dashboard_routes.event_stream(svc, FakeRequest()):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(main())
    assert chunks[0].startswith("event: guard\n")
    assert chunks[1].startswith(f"event: drive-{KEY}\n")
    assert len(chunks) == 2
    assert svc.broker.subscriber_count == 0


def test_event_stream_forwards_and_pings(dash, monkeypatch):
    client, _ = dash
    svc = client.app.state.services
    monkeypatch.setattr(dashboard_routes, "KEEPALIVE_SECONDS", 0.05)

    class FakeRequest:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 4

    async def main():
        chunks = []
        stream = dashboard_routes.event_stream(svc, FakeRequest())
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) == 2:
                svc.broker.publish("guard", "<p>new</p>")
        return chunks

    chunks = asyncio.run(main())
    assert "event: guard\ndata: <p>new</p>\n\n" in chunks
    assert ": ping\n\n" in chunks


def test_live_card_does_not_contain_the_form(dash):
    # The card is replaced every second while a job runs; a form inside it would reset as you type.
    client, conn = dash
    row = conn.execute("SELECT * FROM drives WHERE id = 'serial:A'").fetchone()
    from datetime import datetime

    html = dashboard_routes.render_drive_card(conn, row, datetime.now().astimezone(), config=client.app.state.config)
    assert 'name="minutes"' not in html
    page = client.get("/").text
    assert page.count('name="minutes"') == 1
    form_at = page.index('name="minutes"')
    wrapper_end = page.index("</div>", page.index(f'sse-swap="drive-{KEY}"'))
    assert form_at > wrapper_end


from disc_ripper import cd_rips  # noqa: E402
from disc_ripper.cdtoc import Toc, musicbrainz_disc_id  # noqa: E402

CD_TOC = Toc(first=1, last=3, offsets=(0, 15750, 31500), leadout=47250, data_tracks=frozenset())


class TocDrive:
    def read_toc(self):
        return CD_TOC


def _audio_disc(client, conn):
    drives.set_media(conn, "serial:A", "disc", "audio", "", "t")
    client.app.state.services.drive_factory = lambda path: TocDrive()


def test_audio_disc_shows_rip_button(dash):
    client, conn = dash
    _audio_disc(client, conn)
    html = client.get("/").text
    assert 'hx-post="/dashboard/rip"' in html
    assert "Audio CD" in html


def test_rip_route_queues_job(dash):
    client, conn = dash
    _audio_disc(client, conn)
    r = client.post("/dashboard/rip", data={"drive_id": "serial:A"})
    assert r.status_code == 200
    [job] = conn.execute("SELECT * FROM jobs WHERE kind = 'rip_cd'").fetchall()
    assert json.loads(job["params"])["disc_id"] == musicbrainz_disc_id(CD_TOC)
    assert "Queued" in r.text
    assert "3 tracks" in r.text


def test_rip_route_refuses_non_audio(dash):
    client, conn = dash
    drives.set_media(conn, "serial:A", "disc", "dvd", "MOVIE", "t")
    client.app.state.services.drive_factory = lambda path: TocDrive()
    r = client.post("/dashboard/rip", data={"drive_id": "serial:A"})
    assert r.status_code == 400
    assert "Only audio CDs can be ripped" in r.text
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_rip_again_route(dash):
    client, conn = dash
    _audio_disc(client, conn)
    disc_id = musicbrainz_disc_id(CD_TOC)
    old = jobs.enqueue(conn, "serial:A", "rip_cd", {"disc_id": disc_id}, uses_drive=True, now="t")
    cd_rips.create(conn, old, disc_id, "t")
    jobs.start(conn, old, "t")
    jobs.finish(conn, old, "done", "t")
    cd_rips.update(conn, old, state="filed", summary="Nirvana — Nevermind (1991)")
    cd_rips.remember_disc(conn, disc_id, CD_TOC.to_json(), 3, "serial:A", "t")

    r = client.post("/dashboard/rip", data={"drive_id": "serial:A"})
    assert "Already ripped" in r.text and "Nirvana — Nevermind (1991)" in r.text
    assert 'name="again"' in r.text
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    r = client.post("/dashboard/rip", data={"drive_id": "serial:A", "again": "1"})
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0] == 1


def test_card_links_recent_rip_to_music_page(dash):
    client, conn = dash
    j = jobs.enqueue(conn, "serial:A", "rip_cd", {"disc_id": "x"}, uses_drive=True, now="t")
    cd_rips.create(conn, j, "x", "t")
    jobs.start(conn, j, "t")
    jobs.finish(conn, j, "done", "t")
    cd_rips.update(conn, j, state="needs_look", summary="Unknown album · 9 tracks")
    html = client.get("/").text
    assert f'href="/music/{j}"' in html
    assert "needs a look" in html


def test_rip_again_never_queues_twice(dash):
    client, conn = dash
    _audio_disc(client, conn)
    for _ in range(3):
        client.post("/dashboard/rip", data={"drive_id": "serial:A", "again": "1"})
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'rip_cd'").fetchone()[0] == 1


def test_rip_buttons_disable_themselves(dash):
    client, conn = dash
    _audio_disc(client, conn)
    html = client.get("/").text
    rip_button = html[html.index('hx-post="/dashboard/rip"') - 200: html.index('hx-post="/dashboard/rip"') + 300]
    assert 'hx-disabled-elt="this"' in rip_button


def test_old_disc_not_shown_after_tray_empties(dash):
    client, conn = dash
    old_toc = Toc(first=1, last=5, offsets=(0, 1, 2, 3, 4), leadout=99999, data_tracks=frozenset())
    old = musicbrainz_disc_id(old_toc)
    j = jobs.enqueue(conn, "serial:A", "rip_cd", {"disc_id": old}, uses_drive=True, now="t")
    cd_rips.create(conn, j, old, "t")
    jobs.start(conn, j, "t")
    jobs.finish(conn, j, "done", "t")
    cd_rips.update(conn, j, state="filed", summary="Old Album")
    cd_rips.remember_disc(conn, old, old_toc.to_json(), 5, "serial:A", "t")
    drives.set_media(conn, "serial:A", "empty", None, "", "t1")
    drives.set_media(conn, "serial:A", "disc", "audio", "", "t2")  # a new disc, no insert event yet
    html = client.get("/").text
    assert "Already ripped" not in html
    assert 'hx-post="/dashboard/rip"' in html


@pytest.mark.parametrize(
    "state,text,css",
    [
        ("tray_open", "Tray open", "tray-open"),
        ("empty", "Tray closed", "tray-closed"),
        ("not_ready", "Tray closed", "tray-closed"),
        ("disc", "Tray closed", "tray-closed"),
        ("unknown", "Tray unknown", "tray-unknown"),
    ],
)
def test_tray_indicator(dash, state, text, css):
    client, conn = dash
    drives.set_media(conn, "serial:A", state, "audio" if state == "disc" else None, "", "t")
    html = client.get("/").text
    head = html[html.index('class="card-head"'):html.index("</div>", html.index('class="card-head"'))]
    assert f'class="tray {css}"' in head
    assert text in head


# -- video disc card -----------------------------------------------------------

def _title(n, seconds, size=int(38.2e9), chapters=28, source=None):
    return {"number": n, "seconds": seconds, "size": size, "chapters": chapters,
            "source": source or f"0080{n}.mpls", "output_name": f"t{n:02d}.mkv", "segments": "1", "name": ""}


def _video_disc(conn, titles, held=(), label="SOME_FILM", fp="fp1", disc_type="bluray"):
    from disc_ripper import video_rips

    drives.set_media(conn, "serial:A", "disc", disc_type, label, "t")
    video_rips.save_scan(conn, fp, disc_type, label, {"label": label, "titles": titles}, 120, list(held),
                         "serial:A", "t")
    return fp


DECOY = [_title(0, 8164), _title(1, 8162), _title(4, 750, size=int(2.1e9), chapters=1, source="00020.mpls")]
EPISODES = [_title(n, 2700, size=int(4e9), chapters=8, source=f"0000{n}.mpls") for n in range(4)]


def _page(client):
    return client.get("/").text


def _rip_params(conn):
    return [json.loads(x["params"]) for x in conn.execute("SELECT params FROM jobs WHERE kind='rip_title' ORDER BY id")]


def test_held_card_lists_titles(dash):
    client, conn = dash
    _video_disc(conn, DECOY, held=["two titles of about the same length (decoy playlists?)"])
    text = _page(client)
    assert text.count("2:16:04") == 1 and "2:16:02" in text and "0:12:30" in text
    assert "38.2 GB" in text and "2.1 GB" in text
    assert "Held: two titles of about the same length" in text
    assert 'hx-post="/dashboard/video/rip"' in text and ">Rip<" in text
    assert 'name="main"' in text


def test_rip_button_queues_main_and_extras(dash):
    client, conn = dash
    _video_disc(conn, DECOY, held=["x"])
    r = client.post("/dashboard/video/rip", data={"drive_id": "serial:A", "main": "0", "extras": ["4"]})
    assert r.status_code == 200 and "<html" not in r.text
    assert [(p["title"], p["role"]) for p in _rip_params(conn)] == [(0, "main"), (4, "extra")]


def test_rip_refused_shows_error(dash):
    client, conn = dash
    _video_disc(conn, DECOY, held=["x"])
    r = client.post("/dashboard/video/rip", data={"drive_id": "serial:A", "main": "9"})
    assert "start the rip" in r.text and "scan again by re-inserting" in r.text
    r = client.post("/dashboard/video/rip", data={"drive_id": "nope", "main": "0"})
    assert r.status_code == 404


def test_tv_card_has_no_main_column_until_as_film(dash):
    client, conn = dash
    _video_disc(conn, EPISODES, held=["looks like a TV disc"], disc_type="dvd")
    text = _page(client)
    assert 'name="main"' not in text and "Treat as a film" in text
    client.post("/dashboard/video/rip", data={"drive_id": "serial:A", "main": "", "extras": ["0", "1"]})
    assert [(p["title"], p["role"]) for p in _rip_params(conn)] == [(0, "title"), (1, "title")]
    conn.execute("DELETE FROM jobs")
    conn.commit()
    r = client.post("/dashboard/video/as-film", data={"drive_id": "serial:A", "value": "1"})
    assert 'name="main"' in r.text and "Treat as TV" in r.text
    r = client.post("/dashboard/video/as-film", data={"drive_id": "serial:A", "value": "0"})
    assert 'name="main"' not in r.text


def test_tick_extra_mid_rip_queues_job(dash):
    client, conn = dash
    _video_disc(conn, DECOY, held=["x"])
    client.post("/dashboard/video/rip", data={"drive_id": "serial:A", "main": "0"})
    conn.execute("UPDATE jobs SET state = 'running'")
    conn.commit()
    text = _page(client)
    assert 'hx-trigger="change"' in text and 'hx-post="/dashboard/video/extra"' in text
    rip_id = conn.execute("SELECT rip_id FROM video_rips").fetchone()[0]
    r = client.post("/dashboard/video/extra", data={"drive_id": "serial:A", "rip_id": rip_id, "title": "4"})
    assert r.status_code == 200 and "(queued)" in r.text
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='rip_title'").fetchone()[0] == 2
    r = client.post("/dashboard/video/extra", data={"drive_id": "serial:A", "rip_id": rip_id, "title": "4"})
    assert "already queued or ripped" in r.text


def test_hold_button_while_scanning(dash):
    client, conn = dash
    drives.set_media(conn, "serial:A", "disc", "dvd", "X", "t")
    job = jobs.enqueue(conn, "serial:A", "scan_video", {"sr_name": "sr0"}, uses_drive=True, now="t")
    conn.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (job,))
    conn.commit()
    assert 'hx-post="/dashboard/video/hold"' in _page(client)
    r = client.post("/dashboard/video/hold", data={"drive_id": "serial:A"})
    assert r.status_code == 200
    assert conn.execute("SELECT video_hold FROM drives").fetchone()[0] == 1


def test_move_again_button(dash):
    client, conn = dash
    fp = _video_disc(conn, DECOY)
    job = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": 0, "fingerprint": fp, "title": 0, "role": "main"},
                       uses_drive=True, now="t")
    conn.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job,))
    conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, main_title, message, created_at, updated_at) "
                 "VALUES (?, ?, 'move_failed', 0, 'Could not move: disk full', 't', 't')", (job, fp))
    conn.commit()
    text = _page(client)
    assert "Could not move: disk full" in text and 'hx-post="/dashboard/video/move"' in text
    r = client.post("/dashboard/video/move", data={"drive_id": "serial:A", "rip_id": job})
    assert r.status_code == 200 and "<html" not in r.text


def test_ripped_disc_offers_rip_again_and_done_line(dash, config):
    client, conn = dash
    fp = _video_disc(conn, DECOY, held=["already ripped (downloads/7/)"])
    job = jobs.enqueue(conn, "serial:A", "rip_title",
                       {"rip_id": 7, "fingerprint": fp, "title": 0, "role": "main"}, uses_drive=True, now="t")
    conn.execute("UPDATE jobs SET state = 'done', id = 7 WHERE id = ?", (job,))
    conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, main_title, files, created_at, updated_at) "
                 "VALUES (7, ?, 'ripped', 0, ?, 't', 't')",
                 (fp, json.dumps({"0": {"path": "/downloads/7/SOME_FILM_t00.mkv", "role": "main"}})))
    conn.commit()
    (config.downloads_dir / "7").mkdir()
    text = " ".join(_page(client).split())
    assert "Already ripped (downloads/7/)" in text and "Rip again" in text
    assert "#7 done → downloads/7/SOME_FILM_t00.mkv (2:16:04, 38.2 GB)" in text


def test_card_escapes_disc_text(dash):
    client, conn = dash
    _video_disc(conn, DECOY, held=["<b>x</b>"], label="<script>x</script>")
    text = _page(client)
    assert "&lt;script&gt;x" in text and "<script>x" not in text and "<b>x</b>" not in text


def test_makemkv_key_never_rendered(logged_in_client, config):
    from disc_ripper import settings

    conn = db.connect(config.db_path)
    settings.save(conn, {"makemkv_key": "T-SECRETKEY123"})
    assert settings.get(conn, "makemkv_key") == "T-SECRETKEY123"
    conn.close()
    r = logged_in_client.get("/settings")
    assert r.status_code == 200 and "T-SECRETKEY123" not in r.text
