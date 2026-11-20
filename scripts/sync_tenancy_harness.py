"""Lock and vendor the portable integration fixture into the frontend checkout."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def main():
    """Keep both CI entrypoints identical without floating cross-repo references."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "tenancy_harness"
    target = args.frontend.resolve() / "tenancy_harness"
    if not (args.frontend / "package.json").is_file():
        raise SystemExit("Target must be the explicit frontend checkout")
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.iterdir())
        if p.is_file() and p.name not in {"bundle.lock.json", "README.md"}
    }
    lock = {"version": 1, "owner": "weaves/tenancy_harness", "files": files}
    (source / "bundle.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    target.mkdir(exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)
    print("Locked and synchronized portable tenancy harness.")


if __name__ == "__main__":
    main()
