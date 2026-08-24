from pathlib import Path

import pytest

from triage_agent.auth_store import UserStore

PASSWORD = "correct-horse-battery-staple"


def _store(tmp_path: Path) -> UserStore:
    return UserStore(tmp_path / "users.sqlite3")


def test_create_and_verify_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_user("admin", PASSWORD)

    assert store.verify("admin", PASSWORD) is True


def test_verify_rejects_wrong_password(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_user("admin", PASSWORD)

    assert store.verify("admin", "wrong-password-here") is False


def test_verify_rejects_unknown_user(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.verify("ghost", PASSWORD) is False


def test_create_user_rejects_duplicate_username(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_user("admin", PASSWORD)

    with pytest.raises(ValueError, match="already exists"):
        store.create_user("admin", PASSWORD)


def test_create_user_rejects_short_password(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="at least"):
        store.create_user("admin", "short")


def test_set_password_changes_credentials(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_user("admin", PASSWORD)

    store.set_password("admin", "a-new-better-password")

    assert store.verify("admin", PASSWORD) is False
    assert store.verify("admin", "a-new-better-password") is True


def test_set_password_rejects_unknown_user(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="Unknown user"):
        store.set_password("ghost", PASSWORD)


def test_delete_and_list_usernames(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_user("alice", PASSWORD)
    store.create_user("bob", PASSWORD)

    assert store.list_usernames() == ["alice", "bob"]

    store.delete_user("alice")

    assert store.list_usernames() == ["bob"]


def test_delete_user_rejects_unknown_user(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="Unknown user"):
        store.delete_user("ghost")
