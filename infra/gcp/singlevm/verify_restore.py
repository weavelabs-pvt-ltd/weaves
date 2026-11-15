"""Compare every restored application row with the source SQL export."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
from collections.abc import Iterable


def copy_fingerprints(lines: Iterable[str]) -> dict[str, tuple[int, str]]:
    """Hash sorted PostgreSQL COPY rows, preserving every column value."""
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in lines:
        if current:
            if line.rstrip("\r\n") == "\\.":
                current = ""
            else:
                blocks[current].append(line.rstrip("\r\n"))
        elif line.startswith("COPY ") and line.rstrip().endswith("FROM stdin;"):
            current = line.strip()
            blocks[current] = []
    if not blocks:
        raise ValueError("SQL export contains no COPY blocks")
    return {
        table: (len(rows), hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest())
        for table, rows in blocks.items()
    }


def main() -> None:
    """Require a byte-equivalent copy of the exported application data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--database", default="weaves")
    args = parser.parse_args()
    with gzip.open(args.source, "rt") as source:
        expected = copy_fingerprints(source)
    actual_dump = subprocess.check_output(
        [
            "docker",
            "compose",
            "--project-directory",
            "/opt/weaves/singlevm",
            "--env-file",
            "/opt/weaves/singlevm/runtime.env",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "--data-only",
            "--no-owner",
            "--no-acl",
            "-U",
            "weaves",
            "-d",
            args.database,
        ],
        text=True,
    )
    actual = copy_fingerprints(actual_dump.splitlines(keepends=True))
    if actual != expected:
        raise RuntimeError("Restored data does not exactly match the source export")
    print(
        json.dumps(
            {
                "restored_data_matches": True,
                "tables": {key.split()[1]: value[0] for key, value in actual.items()},
            }
        )
    )


if __name__ == "__main__":
    main()
