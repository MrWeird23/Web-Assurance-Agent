#!/usr/bin/env python3
"""Manual visual baseline approval (ROADMAP Milestone 6, step 5).

Wraps the existing BaselineStore.pending()/approve() so an operator can
approve a pending screenshot capture from the terminal without writing
throwaway Python. Never auto-approves: every capture stays "pending" (and
unused as a comparison baseline) until a human runs this with --approve.

Usage:
    # List every pending capture waiting for review.
    python scripts/approve_baseline.py --baseline-dir data/baselines --list

    # Look at data/baselines/pending/<page-id>__<viewport-id>.png yourself,
    # then approve it:
    python scripts/approve_baseline.py --baseline-dir data/baselines \
        --page-id home --viewport-id desktop --operator "leonel"

--baseline-dir defaults to $TRIAGE_VISUAL_BASELINE_DIRECTORY.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from triage_agent.baselines import BaselineStore  # noqa: E402


def _list_pending(root: Path) -> int:
    pending_dir = root / "pending"
    captures = sorted(pending_dir.glob("*.json")) if pending_dir.is_dir() else []
    if not captures:
        print("No pending baseline captures.")
        return 0
    for metadata_path in captures:
        page_id, _, viewport_id = metadata_path.stem.partition("__")
        png_path = metadata_path.with_suffix(".png")
        print(f"{page_id} / {viewport_id} -> {png_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--baseline-dir",
        type=Path,
        default=Path(os.environ.get("TRIAGE_VISUAL_BASELINE_DIRECTORY", "")) or None,
        help="Root of the baseline store (default: $TRIAGE_VISUAL_BASELINE_DIRECTORY).",
    )
    parser.add_argument("--list", action="store_true", help="List pending captures and exit.")
    parser.add_argument("--page-id")
    parser.add_argument("--viewport-id")
    parser.add_argument("--operator", help="Your name/handle, recorded in the audit log.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm you have visually reviewed the pending screenshot yourself.",
    )
    args = parser.parse_args(argv)

    if not args.baseline_dir:
        parser.error("--baseline-dir (or $TRIAGE_VISUAL_BASELINE_DIRECTORY) is required")

    if args.list:
        return _list_pending(args.baseline_dir)

    if not (args.page_id and args.viewport_id and args.operator):
        parser.error("--page-id, --viewport-id, and --operator are required to approve")

    store = BaselineStore(args.baseline_dir)
    pending = store.pending(page_id=args.page_id, viewport_id=args.viewport_id)
    if pending is None:
        print(f"No pending capture for page={args.page_id!r} viewport={args.viewport_id!r}")
        return 1

    print(f"Pending capture: sha256={pending.sha256} captured_at={pending.captured_at}")
    if not args.yes:
        print("Have you visually reviewed the pending screenshot? Re-run with --yes to confirm.")
        return 1

    record = store.approve(
        page_id=args.page_id,
        viewport_id=args.viewport_id,
        operator_label=args.operator,
        approved_at=datetime.now(UTC),
    )
    print(f"Approved {record.page_id}/{record.viewport_id} as new baseline (by {args.operator}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
