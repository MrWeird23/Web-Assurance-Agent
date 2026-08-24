from datetime import timedelta

from triage_agent.sessions import SessionStore


def test_create_and_lookup_round_trips() -> None:
    store = SessionStore()
    token = store.create("admin")

    assert store.lookup(token) == "admin"


def test_lookup_rejects_unknown_token() -> None:
    store = SessionStore()

    assert store.lookup("not-a-real-token") is None


def test_lookup_expires_old_sessions() -> None:
    store = SessionStore(ttl=timedelta(seconds=-1))
    token = store.create("admin")

    assert store.lookup(token) is None
    # Expired session is dropped, not just masked.
    assert store.lookup(token) is None


def test_revoke_invalidates_a_session() -> None:
    store = SessionStore()
    token = store.create("admin")

    store.revoke(token)

    assert store.lookup(token) is None


def test_revoke_unknown_token_is_a_no_op() -> None:
    store = SessionStore()

    store.revoke("not-a-real-token")
