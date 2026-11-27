import os
import sqlite3
from pathlib import Path

import pytest

from disc_ripper import db
from disc_ripper.config import load_config


def test_load_config_defaults():
    cfg = load_config({})
    assert cfg.config_dir == Path("/config")
    assert cfg.sysfs_root == Path("/sys")
    assert cfg.dev_root == Path("/dev")
    assert cfg.db_path == Path("/config") / "disc-ripper.db"
    assert cfg.secret_key_path == Path("/config") / "secret_key"


def test_load_config_from_env(tmp_path):
    cfg = load_config(
        {
            "CONFIG_DIR": str(tmp_path / "c"),
            "SYSFS_ROOT": str(tmp_path / "s"),
            "DEV_ROOT": str(tmp_path / "d"),
        }
    )
    assert cfg.config_dir == tmp_path / "c"
    assert cfg.sysfs_root == tmp_path / "s"
    assert cfg.dev_root == tmp_path / "d"


def test_connect_uses_wal(config):
    assert not config.config_dir.exists()
    conn = db.connect(config.db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert config.config_dir.is_dir()
    finally:
        conn.close()


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_migrate_fresh_db(config):
    conn = db.connect(config.db_path)
    try:
        assert db.migrate(conn) == 12
        assert {"settings", "drives"} <= _tables(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    finally:
        conn.close()


def test_migrate_is_idempotent(config):
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn)
        conn.execute("INSERT INTO settings (key, value) VALUES ('k', '1')")
        conn.commit()
        assert db.migrate(conn) == 12
        assert conn.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "1"
    finally:
        conn.close()


def test_load_config_step2_fields(tmp_path):
    cfg = load_config({"WORK_DIR": str(tmp_path / "w"), "BUSY_SECRET": "s3cret"})
    assert cfg.work_dir == tmp_path / "w"
    assert cfg.busy_secret == "s3cret"
    assert cfg.run_background is True
    defaults = load_config({})
    assert defaults.work_dir == Path("/work")
    assert defaults.busy_secret == ""


def test_migration_0002_from_step1_db(config):
    from importlib import resources

    conn = db.connect(config.db_path)
    try:
        sql = (resources.files("disc_ripper") / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 1;\nCOMMIT;")
        conn.execute(
            "INSERT INTO drives (id, id_source, label, first_seen, last_seen) "
            "VALUES ('port:ata1:X', 'port', 'Blu-ray', 't', 't')"
        )
        conn.commit()

        assert db.migrate(conn) == 12
        row = conn.execute("SELECT * FROM drives").fetchone()
        assert row["media_state"] == "unknown"
        assert row["disc_type"] is None
        assert row["disc_label"] == ""
        assert row["label"] == "Blu-ray"

        conn.execute(
            "INSERT INTO jobs (drive_id, kind, state, created_at) VALUES ('port:ata1:X', 'test', 'queued', 't')"
        )
        conn.execute("UPDATE drives SET id = 'serial:new' WHERE id = 'port:ata1:X'")
        conn.commit()
        assert conn.execute("SELECT drive_id FROM jobs").fetchone()[0] == "serial:new"
    finally:
        conn.close()


def test_busy_secret_trimmed():
    # Pasting into the Unraid field can add a trailing space or newline; HTTP headers drop them.
    assert load_config({"BUSY_SECRET": "  s3cret \n"}).busy_secret == "s3cret"


def test_migration_0003_prefills_offsets(config):
    from importlib import resources

    conn = db.connect(config.db_path)
    try:
        folder = resources.files("disc_ripper") / "migrations"
        for name, version in (("0001_initial.sql", 1), ("0002_media_jobs.sql", 2)):
            sql = (folder / name).read_text(encoding="utf-8")
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        for i, model in enumerate(["BD-RE BH10LS38", "iHAS124   F", "iHAS124 Y", "DVD-RW XYZ"]):
            conn.execute(
                "INSERT INTO drives (id, id_source, model, first_seen, last_seen) VALUES (?, 'port', ?, 't', 't')",
                (f"d{i}", model),
            )
        conn.commit()
        assert db.migrate(conn) == 12
        offsets = [r[0] for r in conn.execute("SELECT read_offset FROM drives ORDER BY id")]
        assert offsets == [667, 6, 0, 0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"discs", "cd_rips", "albums", "album_releases"} <= tables
    finally:
        conn.close()


def test_load_config_music_dirs(tmp_path):
    cfg = load_config({})
    assert cfg.music_dir == Path("/data/MEDIA/Music")
    assert cfg.downloads_dir == Path("/data/downloads/disc-ripper")
    cfg = load_config({"MUSIC_DIR": str(tmp_path / "m"), "DOWNLOADS_DIR": str(tmp_path / "d")})
    assert (cfg.music_dir, cfg.downloads_dir) == (tmp_path / "m", tmp_path / "d")


def test_migration_0004_tables(config):
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn)
        assert {"video_discs", "video_rips"} <= _tables(conn)
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(drives)")}
        assert cols["video_hold"][4] == "0"
    finally:
        conn.close()


def test_migration_0005(config):
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn)
        assert "encodes" in _tables(conn)
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(encodes)")}
        assert {"rip_id", "title", "preset_id", "preset_name", "preset_settings", "preset_fingerprint", "kind", "state",
                "source_audio", "speed", "output_bytes"} <= set(cols)
        assert cols["source_audio"][4] == "-1"
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(encodes)")}
        assert len(indexes) >= 2
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO encodes (rip_id, title, role, source, disc_type, source_seconds, source_bytes, "
                "preset_id, state, created_at) VALUES (1, 1, 'main', 'x', 'bluray', 1, 1, 1, 'bogus', 't')")
    finally:
        conn.close()


def _old_db(config, version=1):
    from importlib import resources

    conn = db.connect(config.db_path)
    sql = (resources.files("disc_ripper") / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
    conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    return conn


def test_migrate_backs_up_before_upgrading(config):
    conn = _old_db(config)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    backup = config.config_dir / "backups" / "disc-ripper-v1.db"
    assert backup.exists()
    old = sqlite3.connect(backup)
    try:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        old.close()


def test_migrate_keeps_five_backups(config):
    folder = config.config_dir / "backups"
    folder.mkdir(parents=True)
    for v in range(1, 8):
        stamp = folder / f"disc-ripper-v{v}.db"
        stamp.write_bytes(b"x")
        os.utime(stamp, (1_000_000 + v, 1_000_000 + v))
    conn = _old_db(config)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    names = sorted(p.name for p in folder.iterdir())
    assert len(names) == 5
    assert "disc-ripper-v1.db" in names  # the one just made is the newest
    assert "disc-ripper-v7.db" in names and "disc-ripper-v2.db" not in names


def test_migrate_no_backup_when_up_to_date(config):
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn)
        db.migrate(conn)
    finally:
        conn.close()
    assert not (config.config_dir / "backups").exists()


def test_failing_backup_blocks_migration(config, monkeypatch):
    conn = _old_db(config)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(db, "_backup_before_upgrade", boom)
    try:
        with pytest.raises(OSError):
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()
