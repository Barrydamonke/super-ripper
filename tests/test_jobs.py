import json

import pytest

from disc_ripper import db, drives, jobs

SERIAL = b"\x05\x80\x00\x01A"


@pytest.fixture
def conn(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=SERIAL)
    add_sg(config.sysfs_root, 2, sr=1, vpd_serial=b"\x05\x80\x00\x01B")
    c = db.connect(config.db_path)
    db.migrate(c)
    drives.sync(c, drives.discover(config.sysfs_root), "t0")
    drives.update_drive(c, "serial:A", label="Blu-ray", enabled=True)
    yield c
    c.close()


def state(conn, job_id):
    return jobs.get(conn, job_id)["state"]


def test_enqueue_and_next_job_order(conn):
    a1 = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 60}, needs_bytes=5, now="t1")
    a2 = jobs.enqueue(conn, "serial:A", "test", {}, now="t2")
    jobs.enqueue(conn, "serial:B", "test", {}, now="t3")
    row = jobs.get(conn, a1)
    assert json.loads(row["params"]) == {"duration_s": 60}
    assert (row["state"], row["needs_bytes"], row["uses_drive"], row["created_at"]) == ("queued", 5, 0, "t1")
    assert jobs.next_job(conn, "serial:A")["id"] == a1
    jobs.wait_space(conn, a1, "Waiting for cache space")
    assert jobs.next_job(conn, "serial:A")["id"] == a1  # waiting jobs keep their place
    jobs.start(conn, a1, "t4")
    assert jobs.next_job(conn, "serial:A")["id"] == a2
    assert jobs.get(conn, a1)["message"] == ""


def test_finish_and_progress(conn):
    j = jobs.enqueue(conn, "serial:A", "test", {}, now="t1")
    jobs.start(conn, j, "t2")
    jobs.set_progress(conn, j, 0.5, "half", "2026-11-18T22:00:00+00:00")
    row = jobs.get(conn, j)
    assert (row["progress"], row["message"], row["estimated_finish"], row["started_at"]) == (
        0.5, "half", "2026-11-18T22:00:00+00:00", "t2",
    )
    jobs.finish(conn, j, "failed", "t3", error="boom")
    row = jobs.get(conn, j)
    assert (row["state"], row["finished_at"], row["error"]) == ("failed", "t3", "boom")


def test_cancel_queued_states(conn):
    q = jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    r = jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    d = jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    jobs.start(conn, r, "t")
    jobs.start(conn, d, "t")
    jobs.finish(conn, d, "done", "t")
    assert jobs.cancel_queued(conn, q, "t2") == "cancelled"
    assert state(conn, q) == "cancelled"
    assert jobs.get(conn, q)["finished_at"] == "t2"
    assert jobs.cancel_queued(conn, r, "t") == "running"
    assert jobs.cancel_queued(conn, d, "t") == "finished"
    assert jobs.cancel_queued(conn, 999, "t") == "not_found"


def test_retry_copies_job(conn):
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 60}, needs_bytes=7, uses_drive=True, now="t")
    assert jobs.retry(conn, j, "t2") is None  # still queued
    jobs.start(conn, j, "t")
    jobs.finish(conn, j, "interrupted", "t")
    new = jobs.retry(conn, j, "t2")
    row = jobs.get(conn, new)
    assert new != j
    assert (row["state"], row["kind"], json.loads(row["params"]), row["needs_bytes"], row["uses_drive"]) == (
        "queued", "test", {"duration_s": 60}, 7, 1,
    )
    jobs.finish(conn, new, "done", "t3")
    assert jobs.retry(conn, new, "t4") is None


def test_mark_interrupted(conn):
    j = jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    q = jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    jobs.start(conn, j, "t")
    assert jobs.mark_interrupted(conn, "t9") == 1
    row = jobs.get(conn, j)
    assert (row["state"], row["message"], row["finished_at"]) == (
        "interrupted", "Interrupted by a restart or shutdown.", "t9",
    )
    assert state(conn, q) == "queued"


def test_drive_jobs_grouping(conn):
    ids = [jobs.enqueue(conn, "serial:A", "test", {}, now="t") for _ in range(9)]
    for j in ids[:7]:
        jobs.start(conn, j, "t")
        jobs.finish(conn, j, "done", "t")
    jobs.start(conn, ids[7], "t")
    grouped = jobs.drive_jobs(conn, "serial:A")
    assert grouped.running["id"] == ids[7]
    assert [r["id"] for r in grouped.queued] == [ids[8]]
    assert [r["id"] for r in grouped.recent] == list(reversed(ids[2:7]))


def test_running_jobs_and_bytes(conn):
    a = jobs.enqueue(conn, "serial:A", "test", {}, needs_bytes=10, now="t")
    jobs.enqueue(conn, "serial:B", "test", {}, needs_bytes=99, now="t")
    jobs.start(conn, a, "t")
    assert jobs.running_bytes(conn) == 10
    [row] = jobs.running_jobs(conn)
    assert (row["id"], row["drive_label"], row["drive_model"]) == (a, "Blu-ray", "BD-RE WH16NS40")
