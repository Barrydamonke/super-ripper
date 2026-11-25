"""A MakeMKV job that stalls is retried by itself after a few minutes, a couple of times."""

import asyncio
import json

from disc_ripper import drives, jobs, videorip
from disc_ripper.services import Services

from .test_videorip_scan import setup  # noqa: F401

STALLED = videorip.stalled_message(10)


def failed_job(conn, kind="scan_video", error=STALLED, params=None, queued_with=None):
    params = params or {"sr_name": "sr0", "disc_type": "dvd", "label": "FILM_LABEL", "from_insert": True}
    job_id = jobs.enqueue(conn, "serial:A", kind, params, uses_drive=True, now="t0")
    if queued_with:  # e.g. extras queued together with the main title
        jobs.enqueue(conn, "serial:A", kind, queued_with, uses_drive=True, now="t0")
    jobs.start(conn, job_id, "t0")
    jobs.finish(conn, job_id, "failed", "t1", error=error)
    return job_id


def end(config, job_id, during_wait=None):
    services = Services(config)
    services.auto_retry_wait = 0.2

    async def main():
        await services.job_ended(job_id)
        if during_wait:
            during_wait()
        while services._auto_retries:
            await asyncio.sleep(0.02)

    asyncio.run(main())
    return services


def newer_jobs(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id > ? ORDER BY id", (job_id,)).fetchall()


def test_stalled_scan_is_retried_after_the_wait(config, setup):
    j = failed_job(setup)
    end(config, j)
    [retry] = newer_jobs(setup, j)
    assert retry["kind"] == "scan_video" and retry["state"] == "queued"
    assert json.loads(retry["params"])["auto_retry"] == 1
    assert "Retrying automatically at" in jobs.get(setup, j)["error"]


def test_gives_up_after_two_automatic_retries(config, setup):
    params = {"sr_name": "sr0", "disc_type": "dvd", "label": "FILM_LABEL", "from_insert": True, "auto_retry": 2}
    j = failed_job(setup, params=params)
    end(config, j)
    assert newer_jobs(setup, j) == []
    assert "Retrying automatically" not in jobs.get(setup, j)["error"]


def test_other_failures_are_not_retried(config, setup):
    j = failed_job(setup, error="MakeMKV could not read the disc.")
    end(config, j)
    assert newer_jobs(setup, j) == []


def test_no_retry_once_the_owner_acted(config, setup):
    j = failed_job(setup)
    end(config, j, during_wait=lambda: jobs.retry(setup, j, "t2"))  # the owner pressed Retry
    assert len(newer_jobs(setup, j)) == 1  # only the owner's


def test_no_retry_after_the_disc_came_out(config, setup):
    j = failed_job(setup)
    end(config, j, during_wait=lambda: drives.set_media(setup, "serial:A", "empty", None, "", "t2"))
    assert newer_jobs(setup, j) == []


def test_stalled_rip_is_retried_while_its_disc_is_in(config, setup):
    drives.set_media(setup, "serial:A", "disc", "dvd", "FILM_LABEL", "t1")
    setup.execute(
        "INSERT INTO video_discs (fingerprint, disc_type, label, scan, minlength, held, drive_id, last_seen) "
        "VALUES ('fp1', 'dvd', 'FILM_LABEL', '{\"titles\": []}', 120, '[]', 'serial:A', 't1')")
    setup.commit()
    j = failed_job(setup, kind="rip_title", params={"fingerprint": "fp1", "title": 0, "rip_id": 1, "role": "main"},
                   queued_with={"fingerprint": "fp1", "title": 3, "rip_id": 1, "role": "extra"})
    end(config, j)
    extra, retry = newer_jobs(setup, j)  # the extra queued with it doesn't count as the owner acting
    assert retry["kind"] == "rip_title" and json.loads(retry["params"])["auto_retry"] == 1
