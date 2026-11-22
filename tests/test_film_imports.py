import pytest

from disc_ripper import db, film_imports, settings

NOW = "2026-01-01T00:00:00"


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_0007(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 7
    assert {"rip_id", "state", "step", "candidates", "tmdb_id", "movie_id", "title", "year", "root_folder",
            "encode_id", "replace", "old_path", "old_folder", "recycled_path", "new_path", "extras", "message",
            "error", "created_at", "updated_at", "imported_at"} <= _cols(conn, "film_imports")
    assert {"imported_path", "imported_at"} <= _cols(conn, "encodes")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(film_imports)")}
    assert any("tmdb" in i for i in idx)
    with pytest.raises(Exception):
        conn.execute("INSERT INTO film_imports (rip_id, state, created_at, updated_at) VALUES (1,'bogus','t','t')")


def test_radarr_settings_defaults(conn):
    v = settings.get_all(conn)
    assert v["radarr_url"] == "" and v["radarr_api_key"] == ""
    assert v["ask_before_replace"] is True and v["tidy_folders_on_replace"] is True
    assert {s.key: s for s in settings.SETTINGS}["radarr_api_key"].kind == "secret"
    assert {s.key: s for s in settings.SETTINGS}["radarr_url"].group == "Radarr"


def test_ensure_get_update(conn):
    assert film_imports.get(conn, 5) is None
    row = film_imports.ensure(conn, 5, NOW)
    assert row["state"] == "identifying" and row["candidates"] == "[]"
    assert film_imports.ensure(conn, 5, "later")["created_at"] == NOW
    film_imports.update(conn, 5, "t2", state="needs_identify", candidates=[{"tmdb_id": 1}], replace={"a": 1})
    row = film_imports.get(conn, 5)
    assert row["state"] == "needs_identify" and row["updated_at"] == "t2"
    assert row["candidates"] == '[{"tmdb_id": 1}]' and row["replace"] == '{"a": 1}'


def test_claim_one_per_film(conn):
    film_imports.ensure(conn, 1, NOW)
    film_imports.ensure(conn, 2, NOW)
    assert film_imports.claim(conn, 1, 603, NOW) is True
    assert film_imports.claim(conn, 2, 603, NOW) is False
    assert film_imports.get(conn, 2)["state"] == "identifying"
    assert film_imports.claim(conn, 2, 604, NOW) is True
    film_imports.update(conn, 1, NOW, state="imported")
    assert film_imports.claim(conn, 2, 603, NOW) is True


def test_recent_and_active(conn):
    for i in (1, 2, 3, 4):
        film_imports.ensure(conn, i, NOW)
    film_imports.update(conn, 1, NOW, state="imported", imported_at="2026-01-01")
    film_imports.update(conn, 2, NOW, state="in_jellyfin", imported_at="2026-01-03")
    film_imports.update(conn, 3, NOW, state="importing")
    film_imports.update(conn, 4, NOW, state="waiting_other", tmdb_id=9)
    assert [r["rip_id"] for r in film_imports.recent(conn)] == [2, 1]
    assert [r["rip_id"] for r in film_imports.recent(conn, 1)] == [2]
    assert film_imports.active_rip_ids(conn) == {1, 3}
    assert [r["rip_id"] for r in film_imports.waiting_for(conn, 9)] == [4]
    assert film_imports.waiting_for(conn, 8) == []
