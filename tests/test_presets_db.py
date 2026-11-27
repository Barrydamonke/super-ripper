"""Presets in the database: create, update, delete rules, choices, snapshots and history."""

import json
from pathlib import Path

import pytest

from disc_ripper import db, encodes, presets
from disc_ripper.presets import PresetError, PresetSettings

NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def seed(i):
    return PresetSettings.from_dict(presets.SEEDS[i][1])


def settings_from(i, **changes):
    return PresetSettings.from_dict(presets.SEEDS[i][1] | changes)


def queue(conn, preset_id, name="X", rip=1, title=1, disc_type="bluray"):
    return encodes.create(conn, rip_id=rip, title=title, role="main", source=Path(f"/d/{rip}/f.mkv"),
                          disc_type=disc_type, source_seconds=6000.0, source_bytes=30_000_000_000,
                          source_audio=2, preset_id=preset_id, preset_name=name, now=NOW)


def test_create_update_delete_and_uses(conn):
    pid = presets.create_preset(conn, "Anime", settings_from(1, audio_languages=["jpn", "eng"]), NOW)
    with pytest.raises(PresetError) as dup:
        presets.create_preset(conn, "anime", seed(1), NOW)
    assert dup.value.field == "name"
    presets.update_preset(conn, pid, "Anime 2", settings_from(1, quality_bluray=22), NOW)
    got = presets.get_preset(conn, pid)
    assert got.name == "Anime 2" and got.settings.quality_bluray == 22
    assert got.fingerprint == presets.fingerprint(got.settings)
    eid = queue(conn, pid, "Anime 2")
    assert presets.delete_preset(conn, pid) == "Anime 2 is in use by a queued encode; choose another preset there first."
    presets.set_default_choice(conn, "1")
    assert "After ripping" in presets.uses(conn, 1)
    assert encodes.start(conn, eid, NOW)
    assert presets.uses(conn, pid) == ["a running encode"]
    encodes.finish(conn, eid, "done", NOW, output="/o.mkv", output_bytes=1, speed=1.0)
    assert presets.delete_preset(conn, pid) is None
    row = encodes.get(conn, eid)
    assert row["preset_name"] == "Anime 2" and row["preset_id"] is None


def test_renaming_to_own_name_in_other_case_is_allowed(conn):
    presets.update_preset(conn, 1, "STANDARD", seed(1), NOW)
    assert presets.get_preset(conn, 1).name == "STANDARD"
    with pytest.raises(PresetError):
        presets.update_preset(conn, 2, "standard", seed(2), NOW)


def test_in_use_by_drive_and_rip(conn):
    conn.execute("INSERT INTO drives (id, id_source, label, first_seen, last_seen, encode_choice) "
                 "VALUES ('serial:A', 'serial', 'Drive 2', 't', 't', '3')")
    conn.execute("INSERT INTO jobs (id, drive_id, kind, state, params, created_at) "
                 "VALUES (5, 'serial:A', 'rip_title', 'running', '{}', 't')")
    conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, encode_preset, created_at, updated_at) "
                 "VALUES (5, 'fp', 'ripping', '3', 't', 't')")
    conn.commit()
    assert presets.uses(conn, 3) == ["the Drive 2 dropdown", "a rip in progress"]
    assert presets.delete_preset(conn, 3) == (
        "Copy only is in use by the Drive 2 dropdown and a rip in progress; choose another preset there first.")


def test_last_preset_cannot_be_deleted(conn):
    assert presets.delete_preset(conn, 2) is None and presets.delete_preset(conn, 3) is None
    assert "last preset" in presets.delete_preset(conn, 1)
    assert presets.get_preset(conn, 1) is not None
    assert presets.delete_preset(conn, 99) == "Unknown preset."


def test_default_preset_cannot_be_deleted(conn):
    presets.set_default_choice(conn, "2")
    assert "After ripping" in presets.delete_preset(conn, 2)
    presets.set_default_choice(conn, "off")
    assert presets.delete_preset(conn, 2) is None


def test_parse_choice_and_choices(conn):
    assert presets.parse_choice(conn, "2") is None and presets.parse_choice(conn, "off") is None
    assert presets.parse_choice(conn, "99") == "Unknown preset."
    assert presets.parse_choice(conn, "off", allow_off=False) == "Unknown preset."
    assert presets.parse_choice(conn, "02") == "Unknown preset." and presets.parse_choice(conn, "") == "Unknown preset."
    assert presets.choices(conn)[-1] == ("off", "Off") and presets.choices(conn)[0] == ("1", "Standard")
    assert [v for v, _ in presets.choices(conn)] == ["1", "2", "3", "off"]


def test_default_choice(conn):
    assert presets.default_choice(conn) == "1"
    assert presets.set_default_choice(conn, "99") == "Unknown preset." and presets.default_choice(conn) == "1"
    assert presets.set_default_choice(conn, "off") is None and presets.default_choice(conn) == "off"


def test_history_by_fingerprint(conn):
    a, b = presets.fingerprint(seed(1)), presets.fingerprint(seed(2))
    for rip, fp, speed, size in ((1, a, 0.5, 9_000_000_000), (2, b, 4.0, 15_000_000_000), (3, a, 0.0, 0)):
        eid = queue(conn, 1, rip=rip)
        encodes.start(conn, eid, NOW)
        encodes.snapshot(conn, eid, "X", json.dumps(seed(1).to_dict()), fp)
        encodes.finish(conn, eid, "done", NOW, output="/o", output_bytes=size, speed=speed)
    speeds, sizes = encodes.history(conn, a, "bluray")
    assert speeds == [0.5] and sizes == [9_000_000_000 / 6000.0]  # the zero speed and zero size are left out
    assert encodes.history(conn, b, "bluray") == ([4.0], [15_000_000_000 / 6000.0])
    assert encodes.history(conn, a, "dvd") == ([], [])


def test_for_encode_prefers_snapshot_and_fill_fingerprints(conn):
    eid = queue(conn, 1, "Standard")
    live = presets.for_encode(conn, encodes.get(conn, eid))
    assert live[0] == "Standard" and live[1] == seed(1) and live[2] == presets.fingerprint(seed(1))
    encodes.snapshot(conn, eid, "Old name", json.dumps(seed(2).to_dict()), "abc")
    snap = presets.for_encode(conn, encodes.get(conn, eid))
    assert snap == ("Old name", seed(2), "abc")
    conn.execute("UPDATE encodes SET preset_fingerprint = NULL, preset_id = NULL WHERE id = ?", (eid,))
    conn.execute("UPDATE presets SET fingerprint = NULL")
    presets.fill_fingerprints(conn)
    assert encodes.get(conn, eid)["preset_fingerprint"] == presets.fingerprint(seed(2))
    assert all(r[0] for r in conn.execute("SELECT fingerprint FROM presets"))


def test_for_encode_none_when_preset_gone(conn):
    pid = presets.create_preset(conn, "Temp", seed(1), NOW)
    eid = queue(conn, pid, "Temp")
    conn.execute("DELETE FROM presets WHERE id = ?", (pid,))
    assert presets.for_encode(conn, encodes.get(conn, eid)) is None


def test_retry_with_deleted_preset_refused(conn):
    pid = presets.create_preset(conn, "Temp", seed(1), NOW)
    eid = queue(conn, pid, "Temp")
    encodes.start(conn, eid, NOW)
    encodes.finish(conn, eid, "failed", NOW, error="boom")
    assert presets.delete_preset(conn, pid) is None
    assert encodes.retry(conn, eid, NOW) is None
    ok = queue(conn, 1, rip=2)
    encodes.start(conn, ok, NOW)
    encodes.finish(conn, ok, "failed", NOW, error="boom")
    assert encodes.retry(conn, ok, NOW) is not None


def test_parse_choice_rejects_huge_ids(conn):
    assert presets.parse_choice(conn, "9" * 30) == "Unknown preset."


def test_list_presets_skips_invalid_row(conn):
    before = [p.name for p in presets.list_presets(conn)]
    with conn:
        conn.execute("UPDATE presets SET settings = ? WHERE id = 2", ('{"engine": "nope"}',))
    after = [p.name for p in presets.list_presets(conn)]
    assert after == [n for i, n in enumerate(before) if i != 1]
    assert ("2", "Fast") not in presets.choices(conn)


def test_get_preset_invalid_row_is_none(conn):
    with conn:
        conn.execute("UPDATE presets SET settings = ? WHERE id = 2", ('{"engine": "nope"}',))
    assert presets.get_preset(conn, 2) is None
    assert presets.get_preset(conn, 1) is not None


def test_in_use_by_interrupted_encode(conn):
    eid = queue(conn, 1, "Standard")
    with conn:
        conn.execute("UPDATE encodes SET state = 'interrupted' WHERE id = ?", (eid,))
    assert "an interrupted encode" in presets.uses(conn, 1)
    assert "interrupted encode" in presets.delete_preset(conn, 1)


@pytest.mark.parametrize("state,used", [("ripping", True), ("ripped", False), ("move_failed", True),
                                        ("failed", False)])
def test_in_use_by_unfinished_rip(conn, state, used):
    conn.execute("INSERT INTO drives (id, id_source, first_seen, last_seen) VALUES ('serial:A', 'serial', 't', 't')")
    conn.execute("INSERT INTO jobs (id, drive_id, kind, state, params, created_at) "
                 "VALUES (7, 'serial:A', 'rip_title', 'done', '{}', 't')")
    conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, encode_preset, created_at, updated_at) "
                 "VALUES (7, 'fp', ?, '3', 't', 't')", (state,))
    conn.commit()
    want = {"ripping": ["a rip in progress"],
            "move_failed": ["a rip waiting to be moved (Move again or Remove it on the Films page)"]}
    assert presets.uses(conn, 3) == (want[state] if used else [])
