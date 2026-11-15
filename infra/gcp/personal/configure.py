"""Generate host-local personal deployment credentials without logging secrets."""

import base64
import os
import secrets
import shlex
from pathlib import Path

path = Path("/opt/weaves/runtime.env")
if not path.exists():
    os.umask(0o077)
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "WEAVES_SECRET_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            os.urandom(32)
        ).decode(),
        "WEAVES_SSO_SESSION_SECRET": secrets.token_hex(32),
        "WEAVES_ORGANIZATION_ONBOARDING_TOKEN": secrets.token_hex(32),
        "WEAVES_OWNER_EMAIL": "shivamsawarn15@gmail.com",
        "WEAVES_OWNER_PASSWORD": secrets.token_urlsafe(24),
        "WEAVES_OWNER_ORGANIZATION": "Weaves Personal",
        "WEAVES_OWNER_NAME": "Shivam Sawarn",
    }
    path.write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    )
    path.chmod(0o600)
print("Personal runtime credentials are ready in /opt/weaves/runtime.env (root only).")
