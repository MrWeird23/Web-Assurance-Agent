import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from triage_agent.baselines import BaselineStore

CAPTURED_AT = datetime(2026, 1, 1, 9, 0, 0)
APPROVED_AT = datetime(2026, 1, 1, 10, 0, 0)


def test_first_capture_is_pending_and_not_yet_a_usable_baseline(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)

    record = store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )

    assert record.status == "pending"
    assert record.operator_label is None
    assert record.approved_at is None
    assert store.pending(page_id="home", viewport_id="desktop") == record
    assert store.current(page_id="home", viewport_id="desktop") is None


def test_approving_records_operator_label_capture_time_and_hash(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )

    record = store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )

    assert record.status == "approved"
    assert record.operator_label == "alice"
    assert record.approved_at == APPROVED_AT
    assert record.captured_at == CAPTURED_AT
    assert record.sha256 == hashlib.sha256(b"first-png").hexdigest()
    assert store.current(page_id="home", viewport_id="desktop") == record
    assert store.load_approved_png(page_id="home", viewport_id="desktop") == b"first-png"


def test_approving_consumes_the_pending_capture(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )

    store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )

    assert store.pending(page_id="home", viewport_id="desktop") is None


def test_approving_without_a_pending_capture_is_rejected(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)

    with pytest.raises(ValueError, match="No pending baseline capture"):
        store.approve(
            page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
        )


def test_approving_with_a_blank_operator_label_is_rejected(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )

    with pytest.raises(ValueError, match="operator_label"):
        store.approve(
            page_id="home", viewport_id="desktop", operator_label="   ", approved_at=APPROVED_AT
        )


def test_replacing_an_approved_baseline_requires_a_new_capture_and_approval(
    tmp_path: Path,
) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )
    store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )

    replacement_capture = datetime(2026, 2, 1, 9, 0, 0)
    store.capture(
        page_id="home",
        viewport_id="desktop",
        png_bytes=b"second-png",
        captured_at=replacement_capture,
    )

    # Still serving the first approved baseline until the replacement is approved.
    still_current = store.current(page_id="home", viewport_id="desktop")
    assert still_current is not None
    assert still_current.sha256 == hashlib.sha256(b"first-png").hexdigest()

    replacement_approved = datetime(2026, 2, 1, 10, 0, 0)
    store.approve(
        page_id="home",
        viewport_id="desktop",
        operator_label="bob",
        approved_at=replacement_approved,
    )

    current = store.current(page_id="home", viewport_id="desktop")
    assert current is not None
    assert current.operator_label == "bob"
    assert store.load_approved_png(page_id="home", viewport_id="desktop") == b"second-png"


def test_replacement_leaves_the_prior_approval_in_the_audit_history(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )
    store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )
    store.capture(
        page_id="home",
        viewport_id="desktop",
        png_bytes=b"second-png",
        captured_at=datetime(2026, 2, 1, 9, 0, 0),
    )
    store.approve(
        page_id="home",
        viewport_id="desktop",
        operator_label="bob",
        approved_at=datetime(2026, 2, 1, 10, 0, 0),
    )

    history = store.history(page_id="home", viewport_id="desktop")

    assert [record.operator_label for record in history] == ["alice", "bob"]


def test_baselines_for_different_viewports_are_independent(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"desktop-png", captured_at=CAPTURED_AT
    )
    store.capture(
        page_id="home", viewport_id="mobile", png_bytes=b"mobile-png", captured_at=CAPTURED_AT
    )
    store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )

    assert store.current(page_id="home", viewport_id="desktop") is not None
    assert store.current(page_id="home", viewport_id="mobile") is None
    assert store.pending(page_id="home", viewport_id="mobile") is not None


def test_approved_baselines_and_pending_captures_are_stored_under_separate_paths(
    tmp_path: Path,
) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )
    store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )

    assert (tmp_path / "approved" / "home__desktop.png").exists()
    assert not (tmp_path / "pending" / "home__desktop.png").exists()


def test_store_state_persists_across_instances(tmp_path: Path) -> None:
    first_store = BaselineStore(tmp_path)
    first_store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"first-png", captured_at=CAPTURED_AT
    )
    first_store.approve(
        page_id="home", viewport_id="desktop", operator_label="alice", approved_at=APPROVED_AT
    )

    second_store = BaselineStore(tmp_path)

    record = second_store.current(page_id="home", viewport_id="desktop")
    assert record is not None
    assert record.operator_label == "alice"
    assert second_store.load_approved_png(page_id="home", viewport_id="desktop") == b"first-png"


@pytest.mark.parametrize("bad_id", ["../etc", "home/desktop", "Home", "home_desktop", ""])
def test_rejects_unsafe_or_malformed_identifiers(tmp_path: Path, bad_id: str) -> None:
    store = BaselineStore(tmp_path)

    with pytest.raises(ValueError):
        store.capture(
            page_id=bad_id, viewport_id="desktop", png_bytes=b"png", captured_at=CAPTURED_AT
        )
