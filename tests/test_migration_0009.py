import json

from disc_ripper import db, settings


def build_v8(conn):
    for number, sql in db._migrations():
        if number <= 8:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")


def put(conn, key, value):
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def test_old_default_moves_to_zero(config):
    conn = db.connect(config.db_path)
    build_v8(conn)
    put(conn, "raw_keep_hours", 24)
    db.migrate(conn)
    assert settings.get(conn, "raw_keep_hours") == 0


def test_a_chosen_value_is_kept(config):
    conn = db.connect(config.db_path)
    build_v8(conn)
    put(conn, "raw_keep_hours", 48)
    db.migrate(conn)
    assert settings.get(conn, "raw_keep_hours") == 48
