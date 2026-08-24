"""SQLite-backed admin user accounts (username + salted, hashed password)."""

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_ITERATIONS = 200_000
_ALGORITHM = "sha256"
MIN_PASSWORD_LENGTH = 12


class UserStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    def create_user(self, username: str, password: str) -> None:
        username = username.strip()
        if not username:
            raise ValueError("Username is required")
        _validate_password(password)
        salt = secrets.token_hex(16)
        try:
            self._connection.execute(
                "INSERT INTO users(username, salt, password_hash, created_at) VALUES(?, ?, ?, ?)",
                (username, salt, _hash(password, salt), datetime.now(UTC).isoformat()),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"User {username!r} already exists") from None

    def set_password(self, username: str, password: str) -> None:
        _validate_password(password)
        salt = secrets.token_hex(16)
        cursor = self._connection.execute(
            "UPDATE users SET salt = ?, password_hash = ? WHERE username = ?",
            (salt, _hash(password, salt), username),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown user {username!r}")

    def delete_user(self, username: str) -> None:
        cursor = self._connection.execute("DELETE FROM users WHERE username = ?", (username,))
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown user {username!r}")

    def list_usernames(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT username FROM users ORDER BY username"
        ).fetchall()
        return [row["username"] for row in rows]

    def verify(self, username: str, password: str) -> bool:
        row = self._connection.execute(
            "SELECT salt, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            # Hash anyway so a nonexistent username doesn't respond faster
            # than a wrong password would.
            _hash(password, secrets.token_hex(16))
            return False
        return secrets.compare_digest(_hash(password, row["salt"]), row["password_hash"])

    def close(self) -> None:
        self._connection.close()


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        _ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()
