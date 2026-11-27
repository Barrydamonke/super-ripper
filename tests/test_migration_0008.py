"""Migration 0008: presets become rows, encodes point at them, old choices map to ids."""

import json
import re
from pathlib import Path

import pytest

from disc_ripper import db, presets, settings

OLD_DEFAULT = (
    "-sel:all,+sel:video,+sel:(eng|nolang),+sel:(audio&single),-sel:(audio&special),"
    "+sel:(subtitle&forced),-sel:mvcvideo,=100:all,-10:eng"
)
NOW = "2026-01-01T00:00:00+00:00"
SQL = Path(db.__file__).parent / "migrations" / "0008_presets.sql"


def put_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def old_encode(conn, rip, preset, state, **extra):
    cols = {"rip_id": rip, "title": 1, "role": "main", "source": f"/d/{rip}/f.mkv", "disc_type": "bluray",
            "source_seconds": 6000.0, "source_bytes": 30_000_000_000, "source_audio": 2, "preset": preset,
            "state": state, "created_at": NOW, **extra}
    conn.execute(f"INSERT INTO encodes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))


def build_v7(conn, *, selection=OLD_DEFAULT):
    for number, sql in db._migrations():
        if number <= 7:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    put_setting(conn, "fast_quality", 21)
    put_setting(conn, "encode_after_rip", "fast")
    put_setting(conn, "video_track_selection", selection)
    conn.execute("INSERT INTO drives (id, id_source, first_seen, last_seen, encode_choice) "
                 "VALUES ('serial:A', 'serial', 't', 't', 'copy')")
    conn.execute("INSERT INTO jobs (id, drive_id, kind, state, params, created_at) "
                 "VALUES (5, 'serial:A', 'rip_title', 'done', '{}', 't')")
    conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, encode_preset, created_at, updated_at) "
                 "VALUES (5, 'fp', 'ripped', 'standard', 't', 't')")
    old_encode(conn, 5, "standard", "done", speed=0.4, output_bytes=9_000_000_000, finished_at=NOW,
               output="/d/5/f - Standard.mkv")
    old_encode(conn, 6, "fast", "queued")
    conn.commit()


@pytest.fixture
def migrated(config):
    conn = db.connect(config.db_path)
    build_v7(conn)
    assert db.migrate(conn) == 12
    presets.fill_fingerprints(conn)
    yield conn
    conn.close()


def test_presets_seeded_with_fast_quality(migrated):
    rows = {p.id: p for p in presets.list_presets(migrated)}
    assert [rows[i].name for i in (1, 2, 3)] == ["Standard", "Fast", "Copy only"]
    assert rows[2].settings.quality_dvd == 21 == rows[2].settings.quality_bluray
    assert all(p.fingerprint == presets.fingerprint(p.settings) for p in rows.values())
    stored = migrated.execute("SELECT fingerprint FROM presets").fetchall()
    assert all(r[0] for r in stored)  # fill_fingerprints wrote them


def test_old_rows_mapped(migrated):
    enc = migrated.execute(
        "SELECT preset_id, preset_name, preset_settings, preset_fingerprint, kind FROM encodes ORDER BY id").fetchall()
    assert [(r[0], r[1], r[4]) for r in enc] == [(1, "Standard", "film"), (2, "Fast", "film")]
    assert enc[0][3] == presets.fingerprint(presets.PresetSettings.from_dict(json.loads(enc[0][2])))
    assert enc[1][2] is None and enc[1][3] is None  # queued: no snapshot, it follows its preset until it starts
    assert json.loads(migrated.execute("SELECT settings FROM presets WHERE id = 2").fetchone()[0])["quality_bluray"] == 21
    assert migrated.execute("SELECT encode_choice FROM drives").fetchone()[0] == "3"
    assert migrated.execute("SELECT encode_preset FROM video_rips").fetchone()[0] == "1"
    assert presets.default_choice(migrated) == "2"
    assert settings.get(migrated, "video_track_selection") == settings.DEFAULT_VIDEO_SELECTION
    assert migrated.execute(
        "SELECT count(*) FROM settings WHERE key IN ('encode_after_rip','fast_quality')").fetchone()[0] == 0


def test_encodes_keep_their_data_and_indexes(migrated):
    done = migrated.execute("SELECT * FROM encodes WHERE id = 1").fetchone()
    assert (done["state"], done["speed"], done["output_bytes"], done["output"]) == (
        "done", 0.4, 9_000_000_000, "/d/5/f - Standard.mkv")
    names = {r[1] for r in migrated.execute("PRAGMA index_list(encodes)")}
    assert {"encodes_state", "encodes_rip_title", "encodes_active_file"} <= names
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []


def test_hand_edited_selection_kept(config):
    conn = db.connect(config.db_path)
    build_v7(conn, selection="-sel:all,+sel:video,+sel:eng")
    db.migrate(conn)
    assert settings.get(conn, "video_track_selection") == "-sel:all,+sel:video,+sel:eng"
    conn.close()


def test_fresh_database_gets_the_three_presets(config):
    conn = db.connect(config.db_path)
    db.migrate(conn)
    assert [p.name for p in presets.list_presets(conn)] == ["Standard", "Fast", "Copy only"]
    assert presets.default_choice(conn) == "1"
    assert presets.list_presets(conn)[1].settings.quality_bluray == 24
    conn.close()


def test_sql_seeds_equal_the_python_seeds():
    rows = re.findall(r"^\((\d), '([^']+)', '(\{.*?\})', NULL", SQL.read_text(encoding="utf-8"), re.M)
    assert [(int(i), name, json.loads(body)) for i, name, body in rows] == [
        (i, name, settings_) for i, (name, settings_) in presets.SEEDS.items()]
