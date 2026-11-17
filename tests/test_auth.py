import pytest

from disc_ripper import auth, db


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def test_hash_and_verify():
    stored = auth.hash_password("correct horse")
    assert stored.startswith("scrypt$16384$8$1$")
    assert auth.verify_password("correct horse", stored) is True
    assert auth.verify_password("wrong horse", stored) is False
    assert auth.verify_password("x", "garbage") is False
    assert auth.verify_password("x", "scrypt$1$2$3$!!$!!") is False


def test_hashes_are_salted():
    assert auth.hash_password("same") != auth.hash_password("same")


def test_secret_created_once(tmp_path):
    path = tmp_path / "cfg" / "secret_key"
    first = auth.load_or_create_secret(path)
    assert path.exists()
    assert len(first) >= 32
    assert auth.load_or_create_secret(path) == first


def test_session_round_trip():
    sessions = auth.SessionManager("s" * 40)
    assert sessions.read(sessions.issue(3)) == 3
    assert sessions.read("tampered") is None
    assert sessions.read(None) is None
    assert auth.SessionManager("other" * 10).read(sessions.issue(3)) is None


def test_session_expiry():
    sessions = auth.SessionManager("s" * 40)
    token = sessions.issue(1)
    sessions.MAX_AGE = -1
    assert sessions.read(token) is None


def test_set_password_bumps_version(conn):
    assert auth.is_password_set(conn) is False
    assert auth.current_session_version(conn) == 0
    assert auth.set_password(conn, "first-password") == 1
    assert auth.is_password_set(conn) is True
    assert auth.set_password(conn, "second-password") == 2
    assert auth.check_login(conn, "second-password") == 2
    assert auth.check_login(conn, "first-password") is None


def test_check_login_without_password(conn):
    assert auth.check_login(conn, "anything") is None


def test_short_password_rejected(conn):
    with pytest.raises(ValueError):
        auth.set_password(conn, "short")
    assert auth.is_password_set(conn) is False
