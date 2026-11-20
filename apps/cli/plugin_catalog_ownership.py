"""Report or explicitly activate descriptor ownership after reader rollout."""

import argparse
import json
import os

import psycopg

from infrastructure.database.plugin_ownership import (
    activate_ownership,
    ownership_report,
)


def main() -> None:
    """Default to a read-only plan; mutations require an explicit apply command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reader-image", help="Verified API/worker image@sha256 digest"
    )
    parser.add_argument("--max-records", type=int, default=10000)
    args = parser.parse_args()
    if args.apply and not args.reader_image:
        parser.error("--apply requires the verified --reader-image rollback target")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL must identify the operator-selected database")
    with psycopg.connect(database_url) as connection:
        if args.apply:
            report = activate_ownership(
                connection, reader_image=args.reader_image, max_records=args.max_records
            )
        else:
            connection.execute("SET TRANSACTION READ ONLY")
            report = ownership_report(connection)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
