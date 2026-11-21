# Public deployment

The personal public environment now runs entirely on one VM. Follow the
[single-VM operations guide](singlevm/README.md).

| Item | Current value |
| --- | --- |
| Project | `weaves-510819` |
| Account | `shivamsawarn15@gmail.com` |
| VM | `weaves-personal`, `asia-south1-a`, `e2-medium` (4 GB RAM) |
| Reserved regional IPv4 | `8.231.88.4` (`weaves-vm-ip`) |
| Storage | 30 GB balanced persistent disk |
| Website | `https://weave-studio.in` |
| Sign-in and workspace | `https://app.weave-studio.in` |
| API | `https://api.weave-studio.in` |

GoDaddy A records `@`, `app` and `api` all point to `8.231.88.4`, TTL 600.
`www` is a CNAME to `weave-studio.in`. Caddy serves HTTPS and renews its
certificates directly; the main website's Sign in buttons open the app domain.

The previous Cloud SQL instance, Cloud Run API/frontend/worker pool, load
balancer, forwarding rules, global addresses and associated routing resources
were retired. Their [deployment history](PUBLIC-MANAGED-HISTORY.md) is archival;
do not apply its old DNS values or managed deployment commands.

On 2026-11-15 the owner explicitly requested a complete reset. Both old VM
application database volumes were removed. PostgreSQL initialized a fresh
cluster and applied all 18 migrations. There is one initial owner and workspace;
agents, workflows, tasks and skills are empty. Local demo accounts and sample
work are disabled across API/worker restarts. Built-in connector descriptors,
role presets, model defaults and an approval policy remain as system defaults.

New owner login details are stored locally at
`~/.config/weaves-public/login.json` with mode 0600. Public organization
onboarding stays disabled. Google sign-in is the only sign-in method (see `singlevm/README.md`).

Budget for light personal use: approximately ₹3,500–₹4,500/month before taxes,
AI API usage and domain renewal. This is a planning range, not a spending cap.
Backups, registry storage and secrets can incur small additional usage charges.
