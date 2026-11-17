"""Admin password, session secret and signed session cookies."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from itsdangerous import BadData, URLSafeTimedSerializer

from . import settings

MIN_PASSWORD_LENGTH = 8
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
_HASH_KEY = "internal.auth.password_hash"
_VERSION_KEY = "internal.auth.session_version"


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=64 * 1024 * 1024
    )


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _scrypt(password, salt, _N, _R, _P)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        actual = _scrypt(password, salt, int(n), int(r), int(p))
    except (ValueError, binascii.Error, OverflowError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def load_or_create_secret(path: Path) -> str:
    if path.exists():
        value = path.read_text(encoding="ascii").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="ascii")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


class SessionManager:
    COOKIE_NAME = "dr_session"
    MAX_AGE = 30 * 24 * 3600

    def __init__(self, secret: str):
        self._serializer = URLSafeTimedSerializer(secret, salt="disc-ripper-session")

    def issue(self, session_version: int) -> str:
        return self._serializer.dumps({"v": session_version})

    def read(self, token: str | None) -> int | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.MAX_AGE)
        except BadData:
            return None
        version = data.get("v") if isinstance(data, dict) else None
        return version if isinstance(version, int) else None


def is_password_set(conn: sqlite3.Connection) -> bool:
    return bool(settings.get_internal(conn, _HASH_KEY))


def current_session_version(conn: sqlite3.Connection) -> int:
    return int(settings.get_internal(conn, _VERSION_KEY, 0))


def set_password(conn: sqlite3.Connection, password: str) -> int:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    version = current_session_version(conn) + 1
    settings.set_internal(conn, _HASH_KEY, hash_password(password))
    settings.set_internal(conn, _VERSION_KEY, version)
    return version


def check_login(conn: sqlite3.Connection, password: str) -> int | None:
    stored = settings.get_internal(conn, _HASH_KEY)
    if not stored or not verify_password(password, stored):
        return None
    return current_session_version(conn)
