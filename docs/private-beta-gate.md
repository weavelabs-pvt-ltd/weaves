# Private-beta release gate (Phase 6 slice 6.10)

`scripts/private_beta_gate.py` is a **read-only report**. It checks the repository, local
evidence files and, optionally, operator-collected production state, and prints one line per
item: `pass`, `fail`, `blocked-on-owner` (an owner approval or decision is outstanding) or
`unverified` (production evidence was not supplied). It changes nothing, runs no cloud
command, and never connects to production itself. **It approves nothing: public signup stays
off, and inviting outsiders is a human decision.**

```sh
python scripts/private_beta_gate.py                       # repository and evidence only
python scripts/private_beta_gate.py --github              # also one read-only GitHub API GET
python scripts/private_beta_gate.py --production-state gate-state.json --json
```

Exit status: 0 every item passes, 1 at least one fails, 2 nothing fails but items are
blocked on the owner or unverified.

## Evidence files

Default directory `.local/evidence/` (git-ignored; `--evidence-dir` overrides):

| File | Produced by | Needed age |
|---|---|---|
| `dr-drill.json` | `python scripts/dr_drill.py --output .local/evidence/dr-drill.json` (local Docker, about a minute) | at most 30 days |
| `release-compat.json` | `python scripts/release_rehearsal.py run --output .local/evidence/release-compat.json`, or the CI artifact | at most 90 days |

## Optional production state (operator action, read-only)

An operator runs the collector on the VM and hands the JSON to the gate. The collector reads
an allow-list of non-secret `runtime.env` names (the email allowlist is reduced to "set or
not"), the age of the newest dump, and `platform_control.py status`; it prints no secret.

```sh
gcloud compute ssh weaves-personal --project weaves-510819 \
  --account shivamsawarn15@gmail.com --zone asia-south1-a --tunnel-through-iap \
  --command 'sudo python3 -' < scripts/gate_collect_production.py > gate-state.json
```

State older than 2 days is treated as unverified. Review `gate-state.json` before sharing
it; it contains no secrets but does name settings.

## Items

| Id | Item | Source | Today |
|---|---|---|---|
| R01 | the eight runbooks exist and are not stubs | repo | pass once this slice merges |
| R02 | `docs/supported-capacity.md` declares the slots, limits mode and running cap that `compose.yaml` defaults to | repo | pass |
| R03 | `pair.json` requires the T0 to T5 controls and T6-I03 (no planned, no skipped) | repo | pass |
| R04 | rollback floor (`min_rollback_revision`, schema version) is valid | repo | pass |
| R05 | rollback rehearsal evidence passed, at most 90 days old | evidence | fail until a manifest is supplied |
| R06 | local DR drill passed, at most 30 days old | evidence | fail until the drill is run |
| R07 | tombstones, restore reconciliation, kill switch present and wired | repo | pass |
| R08 | checked-in compose keeps public registration off and organization creation default false | repo | pass |
| O02 | alert policies and recipients (6.5) | repo | **blocked-on-owner** |
| O03 | staging environment for real restore drills (6.6) | repo | **blocked-on-owner** |
| P01 | `WEAVES_LIMITS_MODE=enforce` | production | blocked-on-owner while `observe` |
| P02 | account allowlist set and `WEAVES_PUBLIC_REGISTRATION` not true | production | needs state |
| P03 | runtime `signup_enabled` is false | production | needs state |
| P04 | newest on-host backup under 26 hours old | production | needs state |
| P05 | fresh off-host encrypted backup (6.1) | production | **blocked-on-owner** |
| P06 | capacity doc matches the deployed slot count and running cap | production | needs state |
| G01 | branch protection on `main` with required checks | GitHub | blocked-on-owner while off |

Owner approvals outstanding (from the Phase 6 design): off-host backups (bucket, write-only
VM service account, VM restart), alerts (channels and recipients), staging, branch protection,
the move from `observe` to `enforce`. When one is approved and built, its item turns into a
real check and the corresponding runbook section marked "When ... is approved" is rewritten.

The gate deliberately does not turn `signup_enabled` off, does not edit `runtime.env` and
does not trigger a drill. P03 failing means "an operator should run `platform_control.py set
--signup-enabled false`"; the command is in
[runbooks/kill-switch-and-quarantine.md](runbooks/kill-switch-and-quarantine.md).
