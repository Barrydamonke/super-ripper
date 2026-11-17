from pathlib import Path

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
        assert db.migrate(conn) == 1
        assert {"settings", "drives"} <= _tables(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_is_idempotent(config):
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn)
        conn.execute("INSERT INTO settings (key, value) VALUES ('k', '1')")
        conn.commit()
        assert db.migrate(conn) == 1
        assert conn.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "1"
    finally:
        conn.close()
