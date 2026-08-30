"""Generate the checked-in OpenAPI contract without runtime initialization."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
from app.factory import create_schema_app


OUTPUT = BACKEND_DIR / "openapi.json"


def rendered_openapi() -> str:
    return json.dumps(
        create_schema_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_openapi()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"OpenAPI snapshot is out of date: {OUTPUT}")
            return 1
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
