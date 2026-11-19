from datetime import datetime, timezone

import pytest

from disc_ripper import busy, db, drives, jobs
from disc_ripper.schedule import GuardState

END = datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc)
GRACE_END = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    c = db.connect(config.db_path)
    db.migrate(c)
    drives.sync(c, drives.discover(config.sysfs_root), "t0")
    yield c
    c.close()


def running_job(conn, label=""):
    if label:
        drives.update_drive(conn, "serial:A", label=label, enabled=True)
    j = jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    jobs.start(conn, j, "t")
    jobs.set_progress(conn, j, 0.1, "", "2026-11-18T23:30:00+00:00")
    return j


def test_idle_not_busy(conn):
    report = busy.busy_report(conn, GuardState("in_window", END, GRACE_END))
    assert report == {
        "busy": False, "phase": "in_window", "scheduledEnd": "2026-11-18T23:00:00+00:00",
        "graceEndsAt": "2026-11-19T02:00:00+00:00", "jobs": [],
    }


@pytest.mark.parametrize("phase", ["in_window", "draining"])
def test_running_job_busy(conn, phase):
    j = running_job(conn, label="Blu-ray")
    report = busy.busy_report(conn, GuardState(phase, END, GRACE_END))
    assert report["busy"] is True
    assert report["jobs"] == [
        {"id": j, "drive": "Blu-ray", "kind": "test", "estimatedFinish": "2026-11-18T23:30:00+00:00"}
    ]


def test_drive_name_falls_back_to_model(conn):
    running_job(conn)
    report = busy.busy_report(conn, GuardState("in_window", END, GRACE_END))
    assert report["jobs"][0]["drive"] == "BD-RE WH16NS40"


def test_off_hours_never_busy(conn):
    running_job(conn)
    report = busy.busy_report(conn, GuardState("off_hours", END, GRACE_END))
    assert report["busy"] is False
    assert len(report["jobs"]) == 1


def test_no_schedule_busy_if_running(conn):
    running_job(conn)
    report = busy.busy_report(conn, GuardState("no_schedule", None, None))
    assert report["busy"] is True
    assert report["scheduledEnd"] is None and report["graceEndsAt"] is None


def test_queued_job_does_not_hold(conn):
    jobs.enqueue(conn, "serial:A", "test", {}, now="t")
    assert busy.busy_report(conn, GuardState("in_window", END, GRACE_END))["busy"] is False
