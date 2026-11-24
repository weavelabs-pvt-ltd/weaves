"""Runs inside a backend image: store or read back an encrypted secret.

Fed to ``python -`` through ``docker exec`` so the image under test uses its own
secret store and the shared encryption key (never printed). Modes:

    put   ORG WORKSPACE VALUE  -> prints the new secret reference
    check ORG WORKSPACE REF VALUE -> prints ok, or exits 1 when it cannot be read
"""

import os
import sys

from infrastructure.database.store import ProductPostgresStore
from infrastructure.secrets.store import EncryptedPostgresSecretStore
from shared.request_context import reset_request_principal, set_request_identity


def main(argv: list[str]) -> int:
    """Perform the requested secret operation as one tenant-scoped principal."""
    mode, org, workspace = argv[0], argv[1], argv[2]
    store = ProductPostgresStore(os.environ["DATABASE_URL"], app_role="")
    secrets = EncryptedPostgresSecretStore(
        store, os.environ["WEAVES_SECRET_ENCRYPTION_KEY"]
    )
    token = set_request_identity("rehearsal-probe", org, workspace)
    try:
        if mode == "put":
            print(secrets.put(argv[3], purpose="model_provider", owner_ref="rehearsal"))
            return 0
        ok = secrets.get(argv[3]) == argv[4]
        print("ok" if ok else "mismatch")
        return 0 if ok else 1
    except Exception as error:  # report the class only; messages may be sensitive
        print(type(error).__name__)
        return 1
    finally:
        reset_request_principal(token)
        store.close()


sys.exit(main(sys.argv[1:]))
