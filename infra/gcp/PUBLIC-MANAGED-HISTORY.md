# Personal public deployment

Project: `weaves-510819`; account: `shivamsawarn15@gmail.com`; region:
`asia-south1`. Every gcloud command must specify these explicitly; the developer's
globally active account belongs to a different project.

## Routing and access

| Host | DNS type | Target |
| --- | --- | --- |
| `weave-studio.in` (`@`) | A | `34.8.203.157` |
| `app.weave-studio.in` | A | `34.8.203.157` |
| `api.weave-studio.in` | A | `34.8.203.157` |
| `www.weave-studio.in` | CNAME | `weave-studio.in` |

Edit the existing `@`, `app` and `api` records in GoDaddy; TTL 600 is suitable.
Keep `www` as a CNAME to the apex. The active Google-managed HTTPS certificate
covers the app/API names. An additional certificate
`weaves-website-managed-cert` covers the apex and `www`, and requires their DNS
to point at this load balancer before it becomes active. The URL map sends the
apex, `www` and app host to the frontend, and the API host to the API service.
The serverless NEGs follow service
traffic, with no revision tag configured.

The previous API DNS address `34.149.255.80` still has working forwarding rules.
Keep that address until DNS propagation has completed. The stale app address
`8.232.100.207` must be replaced before the app's normal URL works.
The apex's previous address `216.198.79.1` must also be replaced.

The main domain always renders the public marketing website. Its two Sign in
links navigate to `https://app.weave-studio.in/#signin`. The app root renders
sign-in when logged out, then the workspace after authentication. Auth home
links return to the main website. These behaviors use runtime frontend settings
`WEAVES_WEBSITE_ORIGIN` and `WEAVES_APP_ORIGIN`; no second frontend service or
additional load balancer is needed.

Public organization onboarding is disabled. Existing password accounts work.
Historical note: at the time of this record SMTP and SSO were not configured;
email codes have since been removed and Google sign-in is the only method. Do not assume an open
HTTPS endpoint means anonymous access to workspace data.

## Runtime and spending

- API: 1 CPU, 1 GiB; frontend: 1 CPU, 256 MiB. Both scale to zero and are capped
  at three service/revision instances. A cap limits scaling, not monthly spend.
- Worker: one continuously running instance, 1 CPU, 512 MiB.
- PostgreSQL: existing zonal `db-g1-small`, 10 GB SSD, automated backups.
- External application load balancer, reserved IPv4, managed HTTPS certificate,
  Artifact Registry and Secret Manager.
- Budget allowance: ₹8,000–₹12,000/month before tax and AI usage. Actual costs
  depend on requests, outbound traffic, database storage and retained backups.
- The retired private testing VM's disk and archive snapshot are retained and
  continue to incur storage charges. Its data is separate from Cloud SQL.

## Deploy and verify

Build and push immutable linux/amd64 backend and frontend image tags from the
tested repositories, then export `BACKEND_IMAGE` and `FRONTEND_IMAGE` and run:

```sh
bash infra/gcp/deploy-public.sh
```

This preserves runtime secrets, service accounts and Cloud SQL wiring. It stages
both services at zero traffic with the `public-release` tag and updates the
existing worker with the current `python -m apps.worker.main` entrypoint. Worker
deployment uses the v2 REST API through
`update_worker.py`, avoiding the local gcloud worker deploy command's optional
Python gRPC dependency. Worker changes take effect immediately; API/frontend
traffic does not change until explicitly promoted. Only use this sequence for
backward-compatible schema and queue changes.

Run `scripts/e2e_smoke.py` with the staged API base URL and credentials supplied
securely through environment variables. It verifies login, an actual queued
task, worker completion and the persisted transcript/execution ID. Browser-check
the staged frontend against the staged API, then promote the exact verified
revisions using `gcloud run services update-traffic --to-revisions=REVISION=100`.
Recheck login and task execution through the public domains after promotion.

Do not print Secret Manager values, access tokens or full service environment
specifications into logs. Local verification credentials are mode 0600 at
`~/.config/weaves-public/login.json`; they are not repository files.

## Backups and rollback

Before the 2026-11-15 rollout:

- Cloud SQL backup `1794756196281` completed successfully, description
  `before-public-cleanup-deployment`.
- The private VM created `/opt/weaves/backups/weaves-20261115T153051Z.sql.gz`.
- Full disk snapshot `weaves-personal-before-public-20261115` reached `READY`.
- After the managed rollout passed verification, `weaves-personal` was stopped
  and its local IAP tunnel was closed. The disk, dumps and snapshot were kept.

Previous service revisions are `weaves-api-00007-kl6` and
`weaves-frontend-00004-2cj`. Restore traffic to these exact revisions for an
application rollback. The old worker revision is `weaves-worker-00003-xpt`.
Database recovery is a separate operation: preserve current data first, and
restore a backup into a separate instance for inspection before any cutover.
Never overwrite Cloud SQL with the private VM database; they contain separate
organizations and histories.

## Verified 2026-11-15 release

- Backend image tag: `public-20261115-152642-rbac`; frontend image tag:
  `public-20261115-152642`, in this project's `weaves` Artifact Registry repo.
- API revision: `weaves-api-00009-boc`, 100% traffic.
- Frontend revision: `weaves-frontend-00005-muj`, 100% traffic.
- Worker revision: `weaves-worker-00006-rmt`, one instance, current entrypoint.
- Real API smoke checks passed against the staged API and public API: authenticated
  administrator login, task queuing, worker completion and persisted transcript.
- Browser sign-in and all seven main pages passed against staged services and
  the intended HTTPS domain routed to the reserved IP with a browser DNS override.
  No JavaScript errors or failed HTTP responses were observed.
- Health returned 200; unauthenticated Agents, Tasks and Skills returned 401;
  public organization onboarding returned 404; HTTP redirects to HTTPS.
- The upgrade repairs organization-wide owner bindings from deployments made
  before Tasks/Skills existed. Six RBAC regression checks, strict mypy across
  273 files, Ruff lint/format, size checks and frontend lint/syntax checks pass.

At rollout verification, public DNS still pointed `app` to `8.232.100.207` and
`api` to `34.149.255.80`. The normal app hostname remains unavailable until the
GoDaddy change above is applied. The temporary UI is
`https://weaves-frontend-oa4yqtljzq-el.a.run.app/#signin`; its API calls already
use the public API hostname. Existing users/passwords are preserved. The local
public verification credential file contains the CI administrator account;
it does not reset the personal owner's password.

## Website/app domain split

The follow-up frontend image is `frontend:public-20261115-domains` in the same
registry, revision `weaves-frontend-00007-cas`. It adds the domain-specific
behavior above. The deployment helper and frontend CI preserve the origin
settings on future releases. Six browser regression cases cover main/app/www
routing, both Sign in buttons, stored sessions, auth home navigation and old
sign-in links with query parameters. The staged image also passed a browser
check of website → app-domain sign-in → all seven main pages using the real
public API. The browser forwarded frontend requests to the staged image for
that check; this did not verify an issued apex HTTPS certificate.

The previous frontend revision for this change is `weaves-frontend-00005-muj`.
Roll back frontend traffic to it if needed; API and worker were not changed by
the domain split.

The new frontend revision now serves 100% of traffic. Through the load balancer,
the app root displayed sign-in with normal TLS certificate validation, and the
real administrator login and all seven main pages passed. The main website →
app Sign in navigation also passed with the browser routed to the reserved IP;
that separate marketing check bypassed certificate validation because the apex
certificate is still provisioning. This is not a claim that apex HTTPS is ready.
Authoritative DNS now points `api` at `34.8.203.157`; `@` and `app` still need
their GoDaddy updates, then the apex certificate and normal DNS access must be
checked again.

The follow-up DNS check confirmed both GoDaddy authoritative nameservers now
return `34.8.203.157` for the apex, app and API. The app domain passed a fresh
browser check with HTTP 200, a visible sign-in form and no JavaScript errors.
Some recursive resolvers still cached the old apex address `216.198.79.1` at
that check, and the apex certificate was still `PROVISIONING`. DNS edits are
complete; apex certificate activation and cache expiry remain external waits.
The correct app hostname is `app.weave-studio.in`; `app-weave-studio.in` is a
different, unconfigured domain.

By 2026-11-15 16:03 UTC the apex/www certificate became `ACTIVE`. The load
balancer returned HTTP 200 with normal TLS validation for apex, www and app
when directed to the GCP IP. Google and Cloudflare public DNS both resolved the
apex correctly. The developer Mac's system resolver still cached the old apex
IP. Unprivileged `dscacheutil -flushcache` did not clear the mDNSResponder cache;
the protected mDNSResponder reload required an administrator password and was
not performed. If the browser still shows the old website, refresh the local
DNS cache with administrator authentication and restart the browser:

```sh
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```
