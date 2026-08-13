"""Maintenance-only sparse detection reconciliation before Phase 2 cutover.

Stop every backend/worker process that can write legacy identity or suppression
tables before running this command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.detection_state_reconciliation import reconcile_detection_state  # noqa: E402


def _main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 detection-state cutover reconciliation")
    parser.add_argument("--db-url", default=None)
    parser.add_argument(
        "--confirm-legacy-writer-stopped",
        action="store_true",
        help="required acknowledgement that all legacy writer processes are stopped",
    )
    args = parser.parse_args()
    if not args.confirm_legacy_writer_stopped:
        parser.error("--confirm-legacy-writer-stopped is required")
    database_url = args.db_url or get_settings().resolved_database_url
    summary = reconcile_detection_state(
        database_url, legacy_writer_stopped=True
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    _main()
