"""In-memory admin session tokens issued after a successful login.

ponytail: sessions live only in process memory, so a restart logs everyone
out (they just log back in). Move sessions onto UserStore's SQLite file if
rolling restarts kicking people out becomes disruptive.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_TTL = timedelta(hours=12)


@dataclass(slots=True)
class _Session:
    username: str
    expires_at: datetime


class SessionStore:
    def __init__(self, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._sessions: dict[str, _Session] = {}

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _Session(username, datetime.now(UTC) + self._ttl)
        return token

    def lookup(self, token: str) -> str | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at < datetime.now(UTC):
            del self._sessions[token]
            return None
        return session.username

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)
