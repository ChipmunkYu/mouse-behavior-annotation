"""Run one retention cleanup pass and print its JSON report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import database as db_mod  # noqa: E402
from app import models  # noqa: E402,F401
from app.cleanup import RetentionCleaner, run_retention_cleanup  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply backend retention rules once")
    parser.add_argument("--dry-run", action="store_true", help="report actions without side effects")
    args = parser.parse_args()
    settings = get_settings()
    db_mod.configure_engine(settings.resolved_database_url)
    db_mod.ensure_schema(settings.resolved_database_url)
    if args.dry_run:
        with db_mod.SessionLocal() as db:
            report = run_retention_cleanup(db, settings, dry_run=True)
    else:
        report = RetentionCleaner(db_mod.SessionLocal, settings, synchronous=True).run_once()
        if report is None:
            raise RuntimeError("cleanup did not run")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
