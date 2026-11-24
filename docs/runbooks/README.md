# Runbooks

Production is one VM (`weaves-personal`, project `weaves-510819`). These runbooks describe
**current reality**: backups exist only on the VM disk plus 7-day disk snapshots, there are
no alerts, no staging and no branch protection. Sections headed "When ... is approved" say
what changes once the owner approves off-host backups (6.1), alerts (6.5) or staging (6.6).

| Runbook | Use it when |
|---|---|
| [incident-response.md](incident-response.md) | something is broken or suspicious; start here (VM shell, logs, metrics, triage) |
| [kill-switch-and-quarantine.md](kill-switch-and-quarantine.md) | stop work platform-wide or for one tenant |
| [release-and-rollback.md](release-and-rollback.md) | a deploy went wrong; what rollback can and cannot do |
| [disaster-recovery.md](disaster-recovery.md) | database or VM loss; honest recovery limits; the local drill |
| [tenant-recovery.md](tenant-recovery.md) | one organization lost data (no selective restore tool exists) |
| [tenant-deletion-and-erasure.md](tenant-deletion-and-erasure.md) | export, delete and erase a tenant; grace, purge, tombstones |
| [secrets-rotation.md](secrets-rotation.md) | `runtime.env` secrets; losing the Fernet key loses stored secrets |
| [connector-app-registration.md](connector-app-registration.md) | OAuth apps for connectors (owner action) |

Related: [../supported-capacity.md](../supported-capacity.md),
[../private-beta-gate.md](../private-beta-gate.md),
[../../infra/gcp/singlevm/README.md](../../infra/gcp/singlevm/README.md).

Conventions: commands are copy-pasteable on the VM after the setup block in
incident-response.md; scripts' options were checked against their `--help`; VM paths come
from the singlevm README and are confirmed with `dc ps`. Anything that creates or changes
cloud resources is called out and needs the owner's approval at the time. Never put a secret
in a command line, ticket or chat.
