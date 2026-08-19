import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from triage_agent.baselines import BaselineStore

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "approve_baseline.py"
_SPEC = importlib.util.spec_from_file_location("approve_baseline", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
approve_baseline = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(approve_baseline)


def test_list_reports_no_pending_captures_when_store_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = approve_baseline.main(["--baseline-dir", str(tmp_path), "--list"])

    assert exit_code == 0
    assert "No pending" in capsys.readouterr().out


def test_list_reports_each_pending_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home",
        viewport_id="desktop",
        png_bytes=b"fake-png",
        captured_at=datetime.now(UTC),
    )

    exit_code = approve_baseline.main(["--baseline-dir", str(tmp_path), "--list"])

    assert exit_code == 0
    assert "home / desktop" in capsys.readouterr().out


def test_approve_refuses_without_yes_confirmation(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"fake-png", captured_at=datetime.now(UTC)
    )

    exit_code = approve_baseline.main(
        [
            "--baseline-dir",
            str(tmp_path),
            "--page-id",
            "home",
            "--viewport-id",
            "desktop",
            "--operator",
            "leonel",
        ]
    )

    assert exit_code == 1
    assert store.current(page_id="home", viewport_id="desktop") is None


def test_approve_with_yes_records_the_baseline(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home", viewport_id="desktop", png_bytes=b"fake-png", captured_at=datetime.now(UTC)
    )

    exit_code = approve_baseline.main(
        [
            "--baseline-dir",
            str(tmp_path),
            "--page-id",
            "home",
            "--viewport-id",
            "desktop",
            "--operator",
            "leonel",
            "--yes",
        ]
    )

    assert exit_code == 0
    approved = store.current(page_id="home", viewport_id="desktop")
    assert approved is not None
    assert approved.status == "approved"
    assert approved.operator_label == "leonel"


def test_approve_reports_missing_pending_capture(tmp_path: Path) -> None:
    exit_code = approve_baseline.main(
        [
            "--baseline-dir",
            str(tmp_path),
            "--page-id",
            "home",
            "--viewport-id",
            "desktop",
            "--operator",
            "leonel",
            "--yes",
        ]
    )

    assert exit_code == 1
