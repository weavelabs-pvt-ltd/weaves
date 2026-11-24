# Runbook: secrets rotation

Production secrets live in one root-only file on the VM, `/opt/weaves/singlevm/runtime.env`
(mode 0600). It is not in git and **not in any backup except a full-disk snapshot** (there is
no off-VM escrow today). Shell and `dc` helper: [incident-response.md](incident-response.md)
section 0.

Rules for every rotation: never type a secret on a command line (it lands in `ps` and shell
history), never paste one into a ticket or chat, never `cat` the file in a shared terminal.
Edit with `sudoedit /opt/weaves/singlevm/runtime.env` or the Python heredoc pattern below,
and take a root-only copy first: `sudo cp -p /opt/weaves/singlevm/runtime.env
/opt/weaves/singlevm/runtime.env.bak-$(date -u +%Y%m%dT%H%M%SZ)` (delete the copy once the
rotation is verified; it holds the old secrets). Values are read once at process start, so
every change ends with `dc up -d product-api product-worker` (compose recreates the
containers whose environment changed).

## What is in `runtime.env`

| Variable | Used for | Rotate when | Impact |
|---|---|---|---|
| `WEAVES_SECRET_ENCRYPTION_KEY` | Fernet key that encrypts every stored secret (provider keys, connector tokens) | only if compromised | **see the warning below** |
| `WEAVES_BROWSER_SESSION_SECRET` (at least 32 bytes) | HMAC for browser CSRF tokens | after a restore, or if exposed | open browser tabs must reload; no data effect |
| `POSTGRES_PASSWORD` | database role `weaves` | if exposed | API/worker restart |
| `WEAVES_GOOGLE_CLIENT_ID` / `_SECRET` | Google sign-in | if exposed | sign-in fails until updated |
| GitHub/Slack/Atlassian app secrets and the GitHub App private key | connector OAuth | if exposed | connectors unavailable until updated ([connector-app-registration.md](connector-app-registration.md)) |
| `WEAVES_METRICS_TOKEN` (optional) | bearer for the internal metrics route | if exposed | none |
| `BACKEND_IMAGE`, `FRONTEND_IMAGE` | deployed digests | never by hand | managed by `apply_release.py` |

## Fernet key: losing it loses stored secrets

`WEAVES_SECRET_ENCRYPTION_KEY` encrypts the `product_secrets` table. There is **one** key and
no re-encryption or multi-key support in the code: each row only records a 12-character
fingerprint of the key that sealed it (`key_id`). Consequences:

* If the key is lost or replaced, every stored provider key and connector token becomes
  undecryptable. Tenants must re-enter provider keys and re-authorize connectors; nothing
  else in the database is affected. A database backup is useless for those secrets without
  the matching key.
* **Do not "rotate" it as routine hygiene.** Changing the value without re-encrypting is the
  same as losing it. There is no in-place rotation command; a real rotation needs new code
  (decrypt with old, write with new) that does not exist yet.
* Keep the key where a disk loss cannot take it too. The migration record says the key is
  also held as Secret Manager secret `weaves-fernet-key` (version 2 is the current runtime
  key). That is not verified by this repository. Verify by comparing fingerprints, which
  never prints the key:

```sh
# on the VM
sudo grep '^WEAVES_SECRET_ENCRYPTION_KEY=' /opt/weaves/singlevm/runtime.env | cut -d= -f2- | tr -d '\n' | sha256sum | cut -c1-12
# on your laptop (needs Secret Manager access; prints a fingerprint only)
gcloud secrets versions access latest --secret weaves-fernet-key --project weaves-510819 \
  --account shivamsawarn15@gmail.com | tr -d '\n' | sha256sum | cut -c1-12
# key_id of every stored secret: it is the same 12-character fingerprint, so one row set should match the VM value
dc exec -T postgres psql -U weaves -d weaves -c "SELECT key_id, count(*) FROM product_secrets GROUP BY 1;"
```

If the two fingerprints differ, the escrow is wrong: tell the owner immediately; fix it by
storing the current key as a new version (owner decision, cloud change) rather than guessing.
When slice 6.1 is approved, escrow of `runtime.env` with two key custodians replaces this.

### If the key is compromised

1. Decide with the owner; this is customer-impacting. Pause work: `platform_control.py set
   --admissions-paused true` ([kill-switch-and-quarantine.md](kill-switch-and-quarantine.md)).
2. Generate a new key into a root-only file, never to the terminal:
   `dc exec -T product-api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | sudo tee /root/new-fernet.key >/dev/null && sudo chmod 600 /root/new-fernet.key`.
3. Escrow the **new** key (owner-held vault or Secret Manager new version) before using it,
   then `sudoedit /opt/weaves/singlevm/runtime.env` to replace the value from that file,
   `dc up -d product-api product-worker`, and `sudo shred -u /root/new-fernet.key`. Keep the
   old key in escrow until every tenant has re-entered secrets.
4. Announce that provider keys and connectors must be re-added; stored ciphertext from
   before the change stays unreadable (`SELECT key_id, count(*) FROM product_secrets GROUP BY 1`
   shows what is left).

## Session secret (also done after every restore)

```sh
sudo cp -p /opt/weaves/singlevm/runtime.env /opt/weaves/singlevm/runtime.env.bak-$(date -u +%Y%m%dT%H%M%SZ)
sudo python3 - <<'PY'
import re, secrets
path = "/opt/weaves/singlevm/runtime.env"
text = open(path).read()
line = "WEAVES_BROWSER_SESSION_SECRET=" + secrets.token_urlsafe(48)
if re.search(r"(?m)^WEAVES_BROWSER_SESSION_SECRET=", text):
    text = re.sub(r"(?m)^WEAVES_BROWSER_SESSION_SECRET=.*$", line, text)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
open(path, "w").write(text)
PY
cd /opt/weaves/singlevm && dc up -d product-api product-worker && curl -sS https://api.weave-studio.in/api/v0/health
```

(The secret is only used to derive CSRF tokens, so existing sign-ins survive; to end all
sign-ins run a restore reconcile or revoke accounts through the operator tools.)

## Database password

The role password is set inside PostgreSQL; changing `runtime.env` alone breaks the services.
Change both, in this order, in a quiet window (a short API/worker restart):

```sh
read -rs -p "new database password: " PW; echo; export PW
printf "ALTER USER weaves PASSWORD '%s';" "$PW" | dc exec -T postgres psql -U weaves -d weaves
sudo --preserve-env=PW python3 - <<'PY'
import os, re
path = "/opt/weaves/singlevm/runtime.env"
text = open(path).read()
text = re.sub(r"(?m)^POSTGRES_PASSWORD=.*$", "POSTGRES_PASSWORD=" + os.environ["PW"], text)
open(path, "w").write(text)
PY
unset PW
dc up -d product-api product-worker postgres
```

Use a long password of letters and digits only (no quotes or `$`). Verify `curl` health and
`dc logs --since 2m product-api`; if the services fail to connect, the file and the role
disagree, restore the `.bak` copy and repeat.

## Google sign-in and connector secrets

Rotate in the provider console (create the new client secret, keep the old one active),
write the new value with `sudoedit`, `dc up -d product-api product-worker`, test a sign-in
or connector flow, then delete the old secret in the provider console. Procedures for the
apps themselves: [connector-app-registration.md](connector-app-registration.md) and
`infra/gcp/singlevm/README.md` "Google sign-in runbook". Keep `WEAVES_ACCOUNT_ALLOWED_EMAILS`
set (without it anyone with a verified Google email can create an account).

## CI and deploy credentials

There is no stored deploy secret: GitHub Actions uses OIDC and Workload Identity Federation,
and `deploy.py` / `rollback_release.py` take a short-lived access token on stdin. Nothing to
rotate; if a token leaks it expires within the hour. Review who has repository admin and VM
OS Login access instead.

## After any rotation

* `dc ps`, health check, one test sign-in, one test task if provider keys were touched.
* `sudo rm /opt/weaves/singlevm/runtime.env.bak-*` once verified.
* Note the date and the reason in the ticket (not the value).
