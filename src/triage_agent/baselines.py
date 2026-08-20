"""Human-approved visual baselines (Milestone 3.3 — Visual regression).

Policy (see ROADMAP.md §3.3):
- a first capture is only ever `pending` — it is never treated as a usable
  baseline until an explicit `approve()` call;
- approval records page ID, viewport, hash, capture time, and operator label,
  appended to an audit log that is never rewritten or deleted;
- replacing an approved baseline is just another capture + approve cycle —
  the audit log keeps every prior approval, so "replacement" always leaves a
  record;
- approved baselines live under their own `approved/` subdirectory, distinct
  from wherever a caller stores incident/current-run screenshots (this
  module never touches that directory).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

BaselineStatus = Literal["pending", "approved"]

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class BaselineRecord:
    page_id: str
    viewport_id: str
    sha256: str
    captured_at: datetime
    status: BaselineStatus
    operator_label: str | None = None
    approved_at: datetime | None = None


class BaselineStore:
    """Filesystem-backed store for human-approved visual baselines."""

    def __init__(self, root: Path) -> None:
        self._approved_dir = root / "approved"
        self._pending_dir = root / "pending"
        self._audit_log = root / "audit.jsonl"

    def capture(
        self,
        *,
        page_id: str,
        viewport_id: str,
        png_bytes: bytes,
        captured_at: datetime,
    ) -> BaselineRecord:
        slot = _slot(page_id, viewport_id)
        record = BaselineRecord(
            page_id=page_id,
            viewport_id=viewport_id,
            sha256=hashlib.sha256(png_bytes).hexdigest(),
            captured_at=captured_at,
            status="pending",
        )
        _write_atomically(self._pending_dir, f"{slot}.png", png_bytes)
        metadata = json.dumps(_to_json(record)).encode()
        _write_atomically(self._pending_dir, f"{slot}.json", metadata)
        return record

    def pending(self, *, page_id: str, viewport_id: str) -> BaselineRecord | None:
        slot = _slot(page_id, viewport_id)
        metadata_path = self._pending_dir / f"{slot}.json"
        if not metadata_path.exists():
            return None
        return _from_json(json.loads(metadata_path.read_text()))

    def approve(
        self,
        *,
        page_id: str,
        viewport_id: str,
        operator_label: str,
        approved_at: datetime,
    ) -> BaselineRecord:
        if not operator_label.strip():
            raise ValueError("operator_label must not be blank")
        candidate = self.pending(page_id=page_id, viewport_id=viewport_id)
        if candidate is None:
            raise ValueError(
                f"No pending baseline capture for page={page_id!r} viewport={viewport_id!r}"
            )

        slot = _slot(page_id, viewport_id)
        pending_png = self._pending_dir / f"{slot}.png"
        record = BaselineRecord(
            page_id=page_id,
            viewport_id=viewport_id,
            sha256=candidate.sha256,
            captured_at=candidate.captured_at,
            status="approved",
            operator_label=operator_label,
            approved_at=approved_at,
        )
        _write_atomically(self._approved_dir, f"{slot}.png", pending_png.read_bytes())
        with self._audit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_json(record)) + "\n")

        pending_png.unlink(missing_ok=True)
        (self._pending_dir / f"{slot}.json").unlink(missing_ok=True)
        return record

    def current(self, *, page_id: str, viewport_id: str) -> BaselineRecord | None:
        matches = self.history(page_id=page_id, viewport_id=viewport_id)
        return matches[-1] if matches else None

    def history(self, *, page_id: str, viewport_id: str) -> tuple[BaselineRecord, ...]:
        if not self._audit_log.exists():
            return ()
        records = (
            _from_json(json.loads(line)) for line in self._audit_log.read_text().splitlines()
        )
        return tuple(
            record
            for record in records
            if record.page_id == page_id and record.viewport_id == viewport_id
        )

    def load_approved_png(self, *, page_id: str, viewport_id: str) -> bytes:
        slot = _slot(page_id, viewport_id)
        return (self._approved_dir / f"{slot}.png").read_bytes()


def _slot(page_id: str, viewport_id: str) -> str:
    for name, value in (("page_id", page_id), ("viewport_id", viewport_id)):
        if not _IDENTIFIER_PATTERN.match(value):
            raise ValueError(f"Invalid {name}: {value!r}")
    return f"{page_id}__{viewport_id}"


def _to_json(record: BaselineRecord) -> dict[str, str | None]:
    return {
        "page_id": record.page_id,
        "viewport_id": record.viewport_id,
        "sha256": record.sha256,
        "captured_at": record.captured_at.isoformat(),
        "status": record.status,
        "operator_label": record.operator_label,
        "approved_at": record.approved_at.isoformat() if record.approved_at else None,
    }


def _from_json(data: dict[str, str | None]) -> BaselineRecord:
    captured_at = data["captured_at"]
    approved_at = data["approved_at"]
    raw_status = data["status"]
    assert captured_at is not None
    if raw_status == "pending":
        status: BaselineStatus = "pending"
    elif raw_status == "approved":
        status = "approved"
    else:
        raise ValueError(f"Invalid baseline status: {raw_status!r}")
    return BaselineRecord(
        page_id=str(data["page_id"]),
        viewport_id=str(data["viewport_id"]),
        sha256=str(data["sha256"]),
        captured_at=datetime.fromisoformat(captured_at),
        status=status,
        operator_label=data["operator_label"],
        approved_at=datetime.fromisoformat(approved_at) if approved_at else None,
    )


def _write_atomically(directory: Path, name: str, data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=f"{name}-", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(directory / name)
    finally:
        temporary_path.unlink(missing_ok=True)
