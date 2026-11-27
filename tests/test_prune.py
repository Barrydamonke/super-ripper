import os
from datetime import datetime, timedelta, timezone

import pytest

from disc_ripper import db, drives, prune


@pytest.fixture
def conn(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    c = db.connect(config.db_path)
    db.migrate(c)
    drives.sync(c, drives.discover(config.sysfs_root), "t0")
    yield c
    c.close()


NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=31)).isoformat()
RECENT = (NOW - timedelta(days=5)).isoformat()


def job(conn, state="done", finished=OLD, kind="rip_cd", params="{}"):
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (drive_id, kind, state, params, created_at, finished_at) "
            "VALUES ('serial:A', ?, ?, ?, ?, ?)", (kind, state, params, OLD, finished))
    return cur.lastrowid


def log_file(config, name, age_days=40):
    path = config.config_dir / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    stamp = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def exists(conn, job_id):
    return conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None


def test_old_finished_job_is_pruned_with_its_logs(conn, config):
    j = job(conn, "failed")
    main, extra = log_file(config, f"job-{j}.log"), log_file(config, f"job-{j}-cyanrip.log")
    assert prune.prune_old(conn, config.config_dir, NOW) == (1, 2)
    assert not exists(conn, j) and not main.exists() and not extra.exists()


def test_recent_job_is_kept(conn, config):
    j = job(conn, "done", RECENT)
    log = log_file(config, f"job-{j}.log", 5)
    assert prune.prune_old(conn, config.config_dir, NOW) == (0, 0)
    assert exists(conn, j) and log.exists()


@pytest.mark.parametrize("state", ["running", "queued", "waiting_space"])
def test_unfinished_jobs_are_never_touched(conn, config, state):
    j = job(conn, state, OLD)
    log = log_file(config, f"job-{j}.log")
    prune.prune_old(conn, config.config_dir, NOW)
    assert exists(conn, j) and log.exists()


def test_referenced_jobs_are_kept(conn, config):
    cd = job(conn)
    conn.execute("INSERT INTO cd_rips (job_id, disc_id, state, updated_at) VALUES (?, 'd', 'filed', 't')", (cd,))
    rip = job(conn, kind="rip_title", params="{}")
    conn.execute("UPDATE jobs SET params = json_set(params, '$.rip_id', ?) WHERE id = ?", (rip, rip))
    conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, created_at, updated_at) "
                 "VALUES (?, 'f', 'ripped', 't', 't')", (rip,))
    retry = job(conn, "failed", kind="rip_title", params=f'{{"rip_id": {rip}, "title": 1}}')
    unknown = job(conn, "failed", kind="rip_title", params="{}")
    plain = job(conn, kind="rip_cd")
    logs = {j: log_file(config, f"job-{j}.log") for j in (cd, rip, retry, unknown)}
    conn.commit()
    prune.prune_old(conn, config.config_dir, NOW)
    for j in (cd, rip, retry, unknown):
        assert exists(conn, j) and logs[j].exists()
    assert not exists(conn, plain)
    assert conn.execute("SELECT COUNT(*) FROM video_rips").fetchone()[0] == 1


def test_old_encode_and_import_logs(conn, config):
    with conn:
        for eid, state in ((1, "done"), (2, "running")):
            conn.execute("INSERT INTO encodes (id, rip_id, title, role, source, disc_type, source_seconds, "
                         "source_bytes, preset_name, state, created_at) "
                         "VALUES (?, 9, ?, 'main', 's', 'dvd', 1, 1, 'standard', ?, 't')", (eid, eid, state))
        conn.execute("INSERT INTO film_imports (rip_id, state, created_at, updated_at) "
                     "VALUES (9, 'in_jellyfin', 't', 't')")
        conn.execute("INSERT INTO film_imports (rip_id, state, created_at, updated_at) "
                     "VALUES (10, 'importing', 't', 't')")
    old_enc, run_enc, gone_enc = (log_file(config, f"encode-{i}.log") for i in (1, 2, 3))
    new_enc = log_file(config, "encode-4.log", 2)
    done_imp, busy_imp = log_file(config, "import-9.log"), log_file(config, "import-10.log")
    app, backup = log_file(config, "app.log"), log_file(config, "app.log.1")
    prune.prune_old(conn, config.config_dir, NOW)
    assert not old_enc.exists() and not gone_enc.exists() and not done_imp.exists()
    assert run_enc.exists() and new_enc.exists() and busy_imp.exists()
    assert app.exists() and backup.exists()


def test_corrupt_finish_time_is_skipped(conn, config):
    j = job(conn, finished="not a date")
    prune.prune_old(conn, config.config_dir, NOW)
    assert exists(conn, j)
