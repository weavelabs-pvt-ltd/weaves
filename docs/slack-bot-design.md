# Slack bot design: tag, create tasks, PR reviews, answers from connectors

Status: design for owner review, 2026-11-26. Docs only; no real Slack/GitHub calls were made. Code refs are to `origin/main` at `b50f4c3`.
External facts cite their source and read date; "UNVERIFIED" marks what I could not confirm from a primary page.

Requirement (owner, 2026-11-26): Weaves is a bot in a client's Slack workspace that answers when tagged (in thread), creates tasks, triggers GitHub PR
reviews, answers from org connectors (GitHub, Slack search, Jira, Notion, Datadog, custom MCP) and similar flows.

## 0. Summary

- The platform already has tasks/turns/worker, org limits, usage ledger, request ids, kill switch, purge, secrets and RLS. Missing is the
  Slack edge: installation record, signed events endpoint, inbox, identity map, reply path.
- Biggest risk is authorization and leakage: connector tokens are shared per org (the Slack connector acts as its installer), so section 4.1
  sets need-to-know defaults.
- The runtime has no tool loop and no write path (approvals backend removed in migration 31), so v1 is read-only answers, task creation and a
  Slack-only PR review.
- Non-Marketplace apps get 1 request/min and 15 messages on `conversations.replies`, so thread context is scarce.
- Recommended v1: slices S0 to S7 (section 6).
## 1. Current state (evidence)

### 1.1 Slack today is a read-only, user-token connector

| Fact | Evidence |
|---|---|
| OAuth start requests only **user** scopes `search:read,channels:read,groups:read,channels:history,groups:history`; no `scope=` (bot) parameter | `modules/integrations/http/oauth_start.py:78-84` |
| Callback reads only `payload["authed_user"]["access_token"]`; the bot token and `bot_user_id` Slack returns are ignored and not stored | `modules/integrations/http/oauth_slack.py:50-57`; `team_id`/`team_name` are only copied into a setup record, `:78-79` |
| Plugin supports one capability, `slack.messages.search`, via `search.messages` (+ optional `conversations.replies`, limit 10), restricted to configured channel names and quoting the query so operators cannot widen scope | `clients/integrations/slack.py:48-69, 85-112, 130-150`; seeded at `modules/integrations/bootstrap.py:212-240` |
| Channel list discovered with `conversations.list` using the user token | `modules/integrations/http/oauth_resources.py:165-190` |
| Setup docs list the user scopes and a Redirect URL only; no bot scopes, events or request URL | `infra/gcp/singlevm/CONNECTORS.md` section 2 "Slack app" |
| `WEAVES_SLACK_OAUTH_CLIENT_ID/_SECRET` reach API and worker; no signing-secret variable exists | `CONNECTORS.md` section 1 table |

The Slack app "Weaves" is registered (per owner): client id/secret on the server, signing secret offline, a bot
user with bot scopes `app_mentions:read, chat:write, channels:history, groups:history`, the user scopes above,
Event Subscriptions OFF. Nothing in the repo reads the signing secret yet.

### 1.2 No inbound chat or event triggers

`contracts/v1/triggers.py:1-33` defines one trigger type, `WorkflowTrigger.trigger_type: Literal["webhook"]`, authenticated by a stored secret digest. The
only public inbound route is `POST /api/v0/webhooks/{trigger_id}` (`modules/workflows/http/automation_routes.py:318-349`): secret header, `X-Event-ID`
idempotency, 202 with a `replayed` flag. A good pattern for "verify, store event id under a unique key, then act", but it has no HMAC and cannot verify
`X-Slack-Signature`.

### 1.3 The agent runtime

- `modules/agents/runtime_run_capabilities.py:42-98`: for each allowed capability, call it **once** with
  `payload={"query": task}` (`:127-134`); `ACTION_WRITE` capabilities are skipped with the comment "proposed
  explicitly and run only after approval" (`:67-69`). Output is bounded to 5 documents of 2,000 characters
  (`modules/integrations/context_search.py:24-44`). Budgets: `max_tool_calls` (`:70`) and `admission.admit_tool_call` (`:74`).
- The approval flow that comment refers to is gone: migration 31 removed `approval_policies`/`approval_requests`
  (`infrastructure/database/approvals_removal.py:1-27`). `jira.issues.comment` still exists as `ACTION_WRITE`
  (`modules/integrations/bootstrap.py:132-137`) but nothing can approve it. **Writes have no approval path today.**
- GitHub connector supports `github.issues.search` and `github.code.search` only
  (`clients/integrations/github.py:99-100`). It cannot read a PR diff or files. The installation token is minted
  with `contents`, `issues`, `pull_requests` all `read` (`clients/integrations/github_app_auth.py:72-77`), and the
  App is configured read-only with webhooks off (`CONNECTORS.md` GitHub App steps 5-6).

### 1.4 Platform pieces to reuse

| Piece | Where | Reuse |
|---|---|---|
| Task creation (task, thread, first message, queued turn), idempotency key, permission checks `TASKS_CREATE` + `AGENTS_RUN` | `modules/tasks/service.py:111-171`; admission at `:108` | Slack dispatcher calls `create_task` / `add_message` as the mapped member |
| Worker claims turns, heartbeats, finishes success/failure | `modules/tasks/worker_service.py:67-111, 332, 388` | unchanged; Slack reply is a separate tick |
| Worker tick registry: add a tick without editing the loop | `apps/worker/ticks.py:47-60, 192` | `slack_dispatch` and `slack_reply` ticks |
| Per-org limits, atomic admission, `observe`/`enforce`, `LimitExceeded` | `modules/limits/service.py:80-92`; `docs/phase-5-design.md` 5.1, section 3 defaults (running 2, queued 50, 600 req/min) | every Slack-created task passes the same admission |
| Usage ledger keyed by `agent_run_id`, states reserved/settled/released/unknown, purge step `usage` | `docs/DATA_MODEL.md` "Usage ledger (migration 27)"; `modules/auth/organization_purge.py:52` | no change; tag cost by origin |
| Request-id correlation (inbound `X-Request-Id` only when well-formed), ContextVars incl. `request_id/org_id/turn_id` | `shared/correlation.py:28-40`; `apps/api/error_mapping.py:13` | Slack `event_id` becomes the request id suffix |
| Kill switch/quarantine: `platform_controls` (5 s cache), `org_quarantines`; quarantine suspends the org and disables connector tokens | `docs/phase-6-design.md` 6.4; `modules/limits/service.py:86-92` | bot checks the same state |
| Tombstones: hash-chained JSONL (not a table) for purge, credential revocation, secret deletion | `docs/DATA_MODEL.md` "Tombstones (Phase 6.2)"; `modules/recovery/tombstones.py` | Slack token deletion appends a tombstone |
| RLS with named system purposes; global tables need an allow-list | `infrastructure/database/row_security.py:30-65` | new purpose `slack` |
| Secret store (Fernet) with `purpose` tag | `infrastructure/secrets/store.py:40,85,200`; `oauth_slack.py:66-68` uses `purpose="oauth_token"` | purpose `slack_bot_token` |
| Egress: operator-controlled allow-list for model hosts and `WEAVES_MCP_ALLOWED_HOSTS` | `clients/model_providers/egress_policy.py:1-36`; `clients/integrations/oauth.py:140` | fixed outbound hosts `slack.com`, `api.github.com` only; `response_url` must be checked against `hooks.slack.com` |
| Real-stack fakes pattern (fake Google OIDC) | `tenancy_harness/fake_google.py:1-30` | write `fake_slack.py` the same way |

### 1.5 Gaps the bot must close

No bot-token storage or `team_id -> org` mapping; no signed public endpoint with a global event-id inbox; no Slack-user to member mapping (tasks need a
principal, `tasks/service.py:127`); no reply path or Slack origin on tasks; no GitHub PR files/diff capability, no write approval path, and connector
reads take only a query string; model access is BYOK per org (managed AI is a future dependency).

## 2. User flows

Common rules for every flow: the Slack request is acked within 3 s after the inbox insert, all work is async;
the bot answers in the thread of the triggering message; the requester must be a **linked, active Weaves
member** with permission for the action (section 3.3); org suspended/quarantined/over limit gives a short
refusal (ephemeral where possible) and creates no task; nothing a Slack message says may widen permissions.

### F1. Tag to answer with connector data (v1)

- Trigger: `@Weaves <question>` in an allowlisted channel the bot was invited to, or in its thread (thread_ts = parent). DMs later.
- Steps: endpoint verifies and stores `app_mention`; dispatcher resolves team -> org, user -> member, channel policy -> agent; creates a task (title =
  first 80 chars, message = text minus the mention, idempotency key `slack:{team}:{event_id}`); worker runs the agent (connector reads once, as today);
  the `slack_reply` tick posts the assistant message in the thread.
- Who: linked member with `tasks.create` + `agents.run`; the sources used must satisfy the channel visibility rule (4.1).
- Bot posts: optional "working on it" reaction, then one threaded message: answer (about 3,000 characters max, longer text links to Weaves), a
  "Sources" list from the documents' `title`/`source_id`, and a task link.
- Failure: failed turn posts "I could not finish this (code, request id)"; no model provider posts "ask an admin to connect a model"; admission refusal
  posts the limit and retry-after; past `WEAVES_SLACK_REPLY_DEADLINE` (15 min, ASSUMPTION) one timeout note, result stays in Weaves; Slack post failures
  retry with backoff, then `undeliverable` (visible on the task).

### F2. Create a task from Slack (v1)

- Triggers: (a) tag with an intent word, `@Weaves task: <text>` (v1, same code path as F1); (b) message shortcut "Create Weaves task" (S5b); (c) `/weaves task
  <text>` (S5b; developer slash commands cannot be used inside threads, per Slack docs).
- Difference from F1: the task is created and the bot replies with title, link and "I'll post the result here" (watch mode, same thread).
- Failure: ambiguous intent falls back to F1 (answer), never to an unannounced write in another system.

### F3. PR review (two variants)

- Trigger: `@Weaves review https://github.com/org/repo/pull/123` (strict URL parse; repo must be in the GitHub connector's repositories).
- Variant A, Slack-only (v1, recommended): works with the existing read permissions. New capability `github.pr.review_context` reads `GET /pulls/{n}` and
  `/pulls/{n}/files` (diff truncated to a byte cap; GitHub caps the files list at 3,000, and which patches are omitted is UNVERIFIED, so tolerate missing
  patches). Because capabilities receive only `{"query": task}` (`runtime_run_capabilities.py:133`), the capability parses the PR URL from that text. The
  bot replies with a structured review (summary, risks, per-file notes, questions). Nothing is written to GitHub.
- Variant B, post on GitHub (later): requires **Pull requests: write** for `POST /pulls/{n}/reviews` (GitHub docs
  "Permissions required for GitHub Apps", read 2026-11-26) and an installer re-approval (section 5.2). Review is
  posted as `COMMENT` only (never `APPROVE`/`REQUEST_CHANGES`) from the App identity. It is a write: a Slack
  confirmation button is required (F5), and only repo admins or the requester who also has repo access may confirm.
  Optional automatic reviews on `pull_request` webhooks (`opened`, `ready_for_review`, `synchronize`) need
  webhooks switched on with `X-Hub-Signature-256` verification; each delivery needs an idempotency key
  (`X-GitHub-Delivery`) and a per-repo opt-in. Not v1.
- Failure: unreadable PR gets one generic "I cannot read that PR"; a large PR gets a partial-review notice listing what was skipped.

### F4. Status: "what is the agent doing" (v1, small)

- Trigger: `@Weaves status` in a task thread (the thread maps to a task via `slack_threads`), or `/weaves status`.
- Bot posts an ephemeral message (visible to the requester only, via `chat.postEphemeral`, which is not persisted
  and may not be delivered if the user is inactive) with state (queued/running/done), step label, elapsed time
  and a Weaves link. Data comes from `task_turns` and `tool_invocations` summaries, no content. Cancel button is later.

### F5. Approvals and confirmation for anything that writes (later, gated)

- Rule: the bot never performs an external write from a free-text instruction alone. A write is a **proposal**
  rendered as a Block Kit message with the exact action and parameters, a Confirm and a Cancel button, an expiry
  (default 30 min) and an allow-list of confirmers (the requester plus owners/admins, never a different channel
  member by default). The interaction payload is signature-verified, carries the proposal id, and is single-use
  (idempotent on `action_id`+proposal id). Slack's `response_url` is valid for 5 uses/30 min and `trigger_id` for
  3 s (interactivity docs, read 2026-11-26).
- Prerequisite not built: a proposal/confirm backend (the removed approvals code is not a base; see 1.3) plus
  per-capability write allow-lists. Applies to Jira comment, GitHub review/comment, and any future write.
- Failure: expired or double-clicked proposals show "already handled/expired"; confirmer without permission gets
  an ephemeral refusal and an audit event.

### Cut line

| In v1 | Later |
|---|---|
| F1 tag-to-answer incl. thread follow-ups by re-mention | F5 writes and confirmations; Jira comment from Slack |
| F2(a) create task by tag; watch mode result in thread | F2(b) message shortcut, F2(c) slash command |
| F3 variant A (Slack-only review) | F3 variant B; automatic PR webhooks |
| F4 status (ephemeral) | DM mode, `message.*` auto-follow-up without a mention, cancel button, App Home |
| Install, uninstall, kill switch, purge, limits, audit | Enterprise Grid org-wide install, Marketplace listing, managed AI |

## 3. Architecture

```
Slack --POST--> api.weave-studio.in/api/v0/slack/events  (Caddy -> product-api)
   verify v0 HMAC over raw body, 5 min window -> insert slack_event_inbox (unique event_id) -> 200  (<3 s)
worker tick slack_dispatch: claim inbox rows (purpose 'slack') -> resolve team/org/user/channel/agent
   -> bind tenant context -> TaskService.create_task / add_message (principal = mapped member)
worker (unchanged) runs the turn
worker tick slack_reply: finished turns with a slack_threads row and no reply -> chat.postMessage(thread_ts)
```

### 3.1 Endpoints

| Path | Purpose | Notes |
|---|---|---|
| `POST /api/v0/slack/events` | Events API: `url_verification`, `event_callback` | raw body read before JSON; compute `v0=` + HMAC-SHA256 of `v0:{ts}:{body}` with the signing secret, constant-time compare, reject if timestamp is more than 5 minutes off (Slack "Verifying requests from Slack", read 2026-11-26); `url_verification` returns the challenge only after the signature passes |
| `POST /api/v0/slack/interactions` | buttons, message shortcuts (form-encoded `payload=` JSON) | later (S5b, S8) |
| `POST /api/v0/slack/commands` | `/weaves` | later |
| `GET /api/v0/oauth/slack/install`, `.../install/callback` | bot install (distinct from the connector flow) | org Owner/Admin, signed-in, CSRF `state` row bound to org and principal (reuse `oauth_authorization_states`) |
| `GET/PUT /api/v0/organization/slack` | settings: channel allowlist, default agent, linked users, enable/disable | org Owner/Admin |

Caddy already proxies `api.weave-studio.in` to `product-api` (`CONNECTORS.md` header). Signing secret is a new
env `WEAVES_SLACK_SIGNING_SECRET` forwarded to API only (single quotes in `runtime.env`, never in Git). The
endpoint never logs bodies, is exempt from per-principal request limits but has its own global token bucket, and
returns 200 for unknown teams (no oracle) after verification.

### 3.2 Ack, retries, idempotency

Slack requires a 2xx within 3 seconds and retries up to 3 times (almost immediately, after 1 min, after 5 min) with `x-slack-retry-num`/`-reason`;
`x-slack-no-retry: 1` stops retries; subscriptions are temporarily disabled if more than 95% of deliveries fail in 60 minutes (Events API docs). `event_id`
is documented as globally unique, but **the docs give no explicit dedupe guidance, so we design it**: `slack_event_inbox(event_id PK, team_id, event_type,
received_at, state, attempts)` with `ON CONFLICT DO NOTHING`; a conflict acks 200 without work. A second guard `(team_id, channel, event_ts)` stops one
message producing two tasks under different event ids. Only ids and the mention text are kept, deleted 7 days after processing (ASSUMPTION), so the inbox
is not an archive. Dispatch is idempotent through the task idempotency key (`tasks/service.py:118,129-139`), so a crash before "mark done" replays safely.

### 3.3 Data model (migration 32, global tables; migration 33 reserved)

Migration head is 31 (`infrastructure/database/later_migrations.py:22-33`, `docs/HANDOFF.md` section 7). Number 29 is
unused (tombstones are file-based). Reserve **32** for S1/S2 and **33** for S6/S8 up front, as Phase 5 did, so parallel slices
do not collide. New module `infrastructure/database/slack_schema.py`, appended to `LATER_MIGRATIONS`.

| Table / collection | Scope | Key columns |
|---|---|---|
| `slack_installations` (relational, global) | one row per workspace | `team_id` UNIQUE (active), `enterprise_id`, `org_id`, `workspace_id`, `app_id`, `bot_user_id`, `bot_secret_ref`, `scopes`, `installed_by_principal_id`, `installed_by_slack_user`, `status` (active/revoked/uninstalled/suspended), `installed_at`, `revoked_at` |
| `slack_event_inbox` (global) | dedupe + queue | see 3.2; RLS only for purpose `slack` |
| `slack_threads` (product_records, org-scoped) | Slack thread to task | `team_id, channel_id, thread_ts, task_id, requester_principal_id, reply_state` |
| `slack_identities` (product_records, org-scoped) | Slack user to member | `slack_user_id, principal_id, linked_at, status` |
| `slack_channel_policies` (product_records, org-scoped) | allowlist | `channel_id, enabled, agent_id, visibility_rule` |

Global rows need no tenant context to look up a `team_id`, so they follow the waitlist/oauth-state pattern: new system purpose
`slack` in `row_security.py` seeing only `slack_installations` and `slack_event_inbox`; org-scoped collections get
`ownership.json` entries (a test fails otherwise, `docs/phase-5-design.md` "Common rules") and an org purge/export decision
in `modules/auth/organization_retention.py`.

### 3.4 Installation and workspace to org binding

- Install starts in Weaves (Settings, "Add to Slack"), never from Slack's directory in v1, so the **org is chosen by a signed-in Weaves
  Owner/Admin**, not inferred from Slack. Authorize URL: `scope=app_mentions:read,chat:write,channels:history,groups:history` and
  (optional) `user_scope=` empty; `state` row binds org+principal (pattern at `oauth_start.py:77-83`).
- Callback calls `oauth.v2.access` (same helper as `oauth_slack.py:34-38`), reads `access_token` (bot), `bot_user_id`, `team.id`, `app_id`,
  `enterprise`, `is_enterprise_install` (oauth.v2.access fields, Slack docs "Installing with OAuth", read 2026-11-26), stores the bot token via
  the secret store (`purpose="slack_bot_token"`, `owner_ref=team_id`), then writes the installation row. A second org trying to claim an
  active `team_id` is refused ("already connected", operator can free it); re-install by the same org updates in place.
- **Enterprise Grid:** for `is_enterprise_install: true`, `team` is null; v1 refuses org-wide installs and supports per-workspace installs
  (UNVERIFIED which events fire per workspace; Slack lists `team_access_granted/revoked` and `app_uninstalled_team` for org-wide apps only).
- Token rotation: leave OFF in v1. It is irreversible once enabled, gives 12-hour access tokens and single-use refresh tokens (Slack "Using token
  rotation", read 2026-11-26). If the owner turns it on later, the worker already refreshes rotated connector tokens (`CONNECTORS.md` 2.6), and bot
  refresh needs a `oauth.v2.exchange` step first; add a store-new-refresh-token-atomically step (S9).

### 3.5 Identity: Slack user to Weaves member, allowed users, agent selection

- Mapping by **explicit link**: first time an unlinked user addresses the bot, it replies ephemerally with a one-time link to
  `app.weave-studio.in/slack/link?token=...`; the signed-in Weaves member confirms; the row binds `(team_id, slack_user_id) -> principal`.
  Email matching is rejected for v1 (needs `users:read.email`, trusts Slack profile email, and a workspace guest could match).
- Allowed users (decision D2): default **linked members with `tasks.create` + `agents.run`**; bot-level admins in Weaves settings may disable
  individual links. Unlinked users can never create tasks.
- Channel allowlist: the bot only acts in channels with an enabled `slack_channel_policies` row (created by an admin; the bot cannot read
  channels it was not invited to, private channels require invite, Slack events docs for `app_mention`, read 2026-11-26). Mention in a non-listed
  channel gets one ephemeral "ask an admin to enable me here".
- Agent selection: one org default agent for Slack (v1); optional per-channel override field already in the policy row (D5). The agent must be
  published and have a model profile; `condition_only`/audience stay `WORKSPACE`.

### 3.6 Tasks and attribution

Tasks are created through `TaskService.create_task` as the **mapped member's principal** (so RBAC, `TASKS_CREATE`, `AGENTS_RUN`, and
audit use existing paths), with a new optional `origin` object on the task contract (JSON, no migration):
`{"kind":"slack","team_id","channel_id","thread_ts","slack_user_id","event_id"}`. Audit events carry `actor=principal` and the origin; the bot
itself never has a principal and never acts without a human requester. Scheduled/automatic runs are out of scope. Follow-ups in the same thread
append a message (`add_message`, `tasks/service.py:359`) instead of creating a new task.

### 3.7 Replies

- `chat.postMessage` with `thread_ts` = parent message `ts` (never a reply's ts). Text limit: keep under 4,000 characters (Slack truncates beyond
  40,000; `chat.postMessage` docs, read 2026-11-26); blocks only for Sources and buttons. mrkdwn from model output is sanitized: neutralize
  `<!channel>`, `<!here>`, `<@U..>` mentions and links not on the sources list, `parse=none`, `unfurl_links/media=false`.
- About 1 message/second per channel; at most 2 posts per request. Citations list connector document titles/ids only, never invented links.
- `slack_threads.reply_state`: `pending -> posted | undeliverable`; we store the returned `ts` and check before re-posting (Slack has no idempotency key; a
  rare duplicate is accepted).
- Thread context for follow-ups: fetch with `conversations.replies`. For commercially distributed apps outside the Marketplace this is
  Tier 1 (1 request/min) and a maximum of 15 messages per request, for new installs since 2025-05-29 and existing installs from 2026-03-03
  (Slack changelog 2025-05-29 and search summary, read 2026-11-26; the changelog text says existing installs are not affected while a search
  summary states 2026-03-03: **conflicting, UNVERIFIED**; plan for the stricter reading). Internal customer-built apps keep 50+/min and 1,000
  objects. Design: use the mention text only by default; thread fetch is one call per request, cached per thread for 5 minutes, serialized per
  workspace, and skipped (with a note) when rate limited.

### 3.8 Cost control

Every Slack-created task passes `admit_task` and, in the worker, `admit_tool_call` and usage reservation, exactly as web tasks. New: per-Slack-user
bucket (default 10 requests/10 min, ASSUMPTION), per-channel bucket (30/10 min), per-org Slack requests/hour (200). Refusals reply ephemerally
with retry-after. `usage_daily` already rolls up by org/agent; add `origin_kind` to the task for later reports (no new ledger column in v1).
BYOK today (`docs/DATA_MODEL.md` usage section): the customer's key pays. If managed AI arrives, the bot must not become a free proxy; it needs an
entitlement check before dispatch (D7).

### 3.9 Kill switch, quarantine, suspension

The dispatcher re-reads `platform_controls.admissions_paused`, org lifecycle state, `org_quarantines` and installation `status` at every stage. A
quarantined or suspended org: inbox rows are `dropped(reason)`; at most one neutral "Weaves is temporarily unavailable" per channel per hour.
`read_only` platform flag: answers allowed, task creation refused. Quarantine gains a step "installation status = suspended" (S7), restored by
`release-quarantine` through the recorded `changes` (6.4 pattern). `WEAVES_SLACK_ENABLED=false` makes the endpoint ack and drop everything.

### 3.10 Uninstall, revocation and deletion

- Subscribe to `app_uninstalled` and `tokens_revoked` (payload lists user ids per token kind, not token strings; ordering between the two
  events is not guaranteed; Slack docs, read 2026-11-26). On either: mark installation `uninstalled`/`revoked`, delete the bot token from the
  secret store (tombstone `secret_deleted`), stop dispatch, pending replies become `undeliverable`. A failed `chat.postMessage` with
  `account_inactive`/`invalid_auth`/`token_revoked` does the same (belt and braces, since events can be missed while the endpoint is down).
- Org deletion purge: add purge step `slack` (installation row, identities, policies, threads, inbox rows by org, secret) to
  `modules/auth/organization_purge.py` (`PURGE_ERASED` at `:52`), tombstone appended, and call `auth.revoke` best effort to invalidate the bot token
  at Slack. Export includes identities/policies/threads; Slack text lives only in normal task data.

### 3.11 Observability

Request id `slack-{event_id}` (valid per `shared/correlation.py:36`) is bound at ingest and stored on inbox row, task, turn and `slack_threads`
(Phase 5.5 correlation). Logs carry ids, event type, outcome and latency, never message text, tokens or `response_url`. Metrics (org id is never a
label): `slack_events_total{type,outcome}`, `slack_inbox_age_seconds`, `slack_ack_seconds`, `slack_reply_total{outcome}`, `slack_api_429_total{method}`.
Alerts: inbox age above 120 s, ack p95 above 1.5 s, signature failures spiking, Slack's disabled-subscription email.

## 4. Security and abuse

### 4.1 Authorization and data leakage (the main risk)

- **Who may trigger**: linked active members only; per-action permissions come from the member's Weaves role, not from Slack roles.
- **Connector tokens are org-shared**: a connector installation is workspace-scoped (`PluginInstallation.auth_ref`,
  `contracts/v1/plugins.py:127-131`), and the Slack search connector runs with the **installing user's** user token
  (`oauth_slack.py:50-57`), so any answer that uses it can quote messages from channels the asker cannot see. Defaults:
  1. The Slack bot's agent gets only connectors the org marks "allowed for Slack answers" (new flag, default off), so enabling the bot does not
     implicitly expose Jira/Datadog/private Slack search.
  2. Reply visibility rule on the channel policy: `public_channel` (default) answers only with connectors marked "shareable"; sensitive ones
     answer ephemerally or by DM to the asker; `private_channel` may use any enabled connector.
  3. The Slack connector's own channel list (`clients/integrations/slack.py:48-69`) stays authoritative: the bot never searches beyond it.
  4. Admin UI warns: answers in a channel are visible to everyone in it, including guests and Slack Connect members (D6).
- **Prompt injection** from Slack messages and PR content: today the agent has read-only connector calls with bounded outputs and no write path,
  so the worst outcome is a misleading or data-revealing answer. Mitigations: system prompt marks Slack text and PR diffs as untrusted data;
  connector results are never allowed to select other connectors or tools (no tool loop); strip/neutralize mentions and links in replies
  (3.7); do not include secrets or env in context; PR variant A cannot comment on GitHub. **With writes** (later) injection becomes serious: writes
  need typed parameters shown verbatim in the confirmation, a human confirmer who is not required to be the requester, per-capability allow-list,
  and no write whose parameters derive from fetched content without being displayed.
- **Rate limiting**: per Slack user, channel and org (3.8) plus a global ingest bucket; Slack's own cap is 30,000 events per workspace per hour.
- **Replay and forgery**: raw-body HMAC with 5-minute window, constant-time compare, `event_id` uniqueness, and timestamp stored; reject
  `x-slack-retry-num` abuse by deduping (retries are not special). Interaction payloads: signature, then proposal id and state checks. Verification
  token is not used (deprecated per Slack).
- **Secrets**: signing secret in `runtime.env`; bot token only in the Fernet store; none in logs, shell args or CI (AGENTS.md); `response_url` and
  `trigger_id` are never logged.
- **SSRF/egress**: outbound calls only to `slack.com/api` (fixed base URL, as `clients/integrations/slack.py:30`), `hooks.slack.com` for
  `response_url` (host must match exactly, https only, no redirects), and `api.github.com`. Attachments and `files.*` are not fetched in v1; links in
  Slack text are not followed by the server.
- **Cross-tenant**: events for an unbound `team_id` are acked and dropped; tenant context comes from the installation row before any tenant table is
  touched (RLS, 1.4).

### 4.2 Privacy, terms and sub-processors

Slack becomes a new inbound data source and the bot a processing path. Needed before external customers: privacy notice update (Slack message text
and user ids are processed to answer; retention; deletion on uninstall); sub-processor list adds Slack for delivery and the org's chosen model
provider for content (already BYOK); DPA/terms language that the customer authorizes the bot for specific channels. Slack's API Terms were updated
2025-05-29/2025-06-30 to restrict how apps store and reuse Slack data and (reported by secondary sources, UNVERIFIED against the primary text) to bar
bulk export, persistent copies/indexes and LLM training on API data (changelog 2025-05-29, Cloud Wars and Hunton write-ups, read 2026-11-26). The
Marketplace guidelines also say AI agent apps should not store Slack data obtained from the API and should store metadata and fetch in real time.
Design consequence: no Slack archive or index; store only mentions the user addressed to us plus our replies; thread context is fetched on demand and
not persisted as separate records. Legal review of the exact terms is a precondition for D7/Marketplace.

## 5. Slack app configuration

### 5.1 Per version (exact)

| Version | Bot scopes | User scopes | Events | Interactivity / commands | Other |
|---|---|---|---|---|---|
| v0 (registered today) | `app_mentions:read, chat:write, channels:history, groups:history` | `search:read, channels:read, groups:read, channels:history, groups:history` | none (OFF) | none | Redirect URL `https://api.weave-studio.in/api/v0/oauth/slack/callback` |
| v1 | same four (no change; re-install not needed for existing installs of the bot) | same (connector only) | Request URL `https://api.weave-studio.in/api/v0/slack/events`; bot events `app_mention`, `app_uninstalled`, `tokens_revoked` | none | add second redirect URL for bot install; bot token via `scope=`; "Always show my bot as online" off; public distribution on only when D8 says so |
| v1.1 | add `reactions:write` (ack emoji, optional) | none | add `message.channels`, `message.groups` only if no-mention follow-ups are chosen | Interactivity Request URL `.../slack/interactions` (needed for F5 and shortcut) | message shortcut "Create Weaves task" (no scope; UNVERIFIED) |
| v2 | add `commands` (slash command) and `im:history` + `im:write`/`message.im` if DMs | none | `message.im` | `/weaves` URL `.../slack/commands` | optional `chat:write.public` is **not** requested |

Scope notes: `chat:write.public` is avoided (bot posts only where invited); `users:read.email` avoided (3.5); the user scopes belong to the
connector and must not be merged into the bot install (Marketplace guideline: user scopes only when the action must be the user's). If the bot is
installed without the connector, the user-scope grant is not requested at all.

### 5.2 What customers must re-approve

Slack scopes are additive and removable only by revoking (Slack OAuth docs); adding one (v1.1 `reactions:write`, v2 `commands`/`im:*`) sends the
installer or admin through OAuth again, and workspaces with app approval need an admin to approve. Adding events needs no re-install unless a scope is
new (UNVERIFIED per event type). GitHub **variant B** needs the App owner to raise Pull requests from Read to Write; every existing installation then
shows a pending permission request the org owner must accept (until then tokens stay read-only); webhooks also need a secret. Send one coordinated
notice. (Jira write was already requested, `oauth_start.py:106`.)

### 5.3 Marketplace readiness and timing

Needed (Slack Marketplace guidelines): public landing page with privacy policy and support contact (reply within two business days), individually
justified least-privilege scopes, TLS 1.2+, signature verification, OAuth `state`, onboarding for users with no account yet, at least 10 active
workspaces and 20 monthly active users (28 days), and Socket Mode apps cannot be listed, so HTTP is the design. Timing: not before design partners
hit the 1/min thread limit or want discovery; owner and partner installs work without it.

## 6. Slice plan

Durations are an ASSUMPTION based on Phase 5/6 slices: 1.5 to 3 h of agent time each plus CI, 8 to 15 min real-stack runs. Every slice ends with
unit tests, PostgreSQL tests under the restricted role, a real-stack control against `tenancy_harness/fake_slack.py` (serves `oauth.v2.access`,
`chat.postMessage`, `conversations.replies`, `auth.test`, signs events with a test signing secret, can replay/duplicate/forge events), and, where a
model is involved, the budget-gated live run required by AGENTS.md. A **live smoke against the owner's dev workspace is run by the owner** after
S2, S4, S6.

| Slice | Goal | Files/modules | Migration | Contracts | Tests beyond common | Depends | Parallel with | Effort |
|---|---|---|---|---|---|---|---|---|
| S0 prelude | Config: `WEAVES_SLACK_SIGNING_SECRET`, `WEAVES_SLACK_ENABLED`, compose forwarding, `pair.json` T7-S* planned, fake Slack server skeleton, `modules/slack/` package, Slack client with fixed base URL | `shared/slack_config.py`, `clients/slack/`, `tenancy_harness/fake_slack.py`, `infra/gcp/singlevm/compose.yaml`, `CONNECTORS.md` | no | none | unit config; fake serves/verifies | none | none | 1.5 h |
| S1 install | Bot install flow, installation row, token in secret store, team to org binding, admin status view | `modules/slack/installation_*.py`, `modules/integrations/http/oauth_slack_install.py`, `slack_schema.py`, `row_security.py` purpose `slack`, `ownership.json` | **yes (32)** | `SlackInstallation` | PG: unique team_id, cross-org claim refused, RLS; harness: install via fake | S0 | S3 (design only) | 2.5 h |
| S2 events ingest | Signed endpoint, url_verification, inbox, dedupe, retries, kill switch checks | `modules/slack/http/events.py`, `modules/slack/inbox.py`, `apps/api/router.py` | **yes (32, same file as S1)** | event envelope models | forged/old/duplicate/retry events, 3 s ack with slow DB, replay, unknown team | S1 | S3 | 2.5 h |
| S3 identity and policy | Link flow, `slack_identities`, `slack_channel_policies`, org settings API, frontend settings (weaves-app) | `modules/slack/identity.py`, `policy.py`, HTTP routes, frontend `docs/handoff-specs` page | no (collections) | `SlackIdentity`, `SlackChannelPolicy` | linking, revoked member, role downgrade, guest | S1 | S2, S4 (stubs) | 2.5 h backend + 2 h frontend |
| S4 tag to answer | `slack_dispatch` and `slack_reply` ticks, task `origin`, reply formatter, per-user/channel/org buckets, F1 and F4, citations | `modules/slack/dispatcher.py`, `reply.py`, `apps/worker/ticks.py`, `contracts/v1/tasks.py` (origin) | no | `TaskOrigin` | full flow on fake Slack, failed turn, quarantine mid-run, suspended org, no provider; live run | S2, S3 | S5 | 3 h |
| S5 create task | F2(a): intent parse, watch mode, idempotency | `modules/slack/intents.py` | no | none | ambiguous intent falls back, duplicate event | S4 | S6 | 1.5 h |
| S5b shortcut/slash | F2(b),(c): interactions and commands endpoints, ack within 3 s, `response_url` host check | `modules/slack/http/interactions.py` | no | payload models | signature, expired trigger, thread limitation | S4 | S6, S7 | 2 h |
| S6 PR review (Slack-only) | `github.pr.review_context` capability (PR + files, size caps), review agent template, F3 variant A | `clients/integrations/github.py`, `modules/integrations/bootstrap.py`, `modules/agents/templates.py`, `modules/slack/intents.py` | no | capability spec | fake GitHub PR payloads incl. truncated patches, forbidden repo, 3,000-file PR; live run | S4 | S5 | 2.5 h |
| S7 lifecycle and purge | `app_uninstalled`/`tokens_revoked`, quarantine step, org purge step `slack`, tombstone, revocation on `invalid_auth` | `modules/auth/organization_purge.py`, `organization_lifecycle.py`, `modules/slack/lifecycle.py` | **yes (33, only if a new column/index is needed)** | none | purge then restore reconcile (T6-I01 style), uninstall during run | S2 | S5, S6 | 2.5 h |
| S8 writes and confirm (later) | Proposal store, Block Kit confirm, GitHub review post (variant B), Jira comment | new `modules/proposals/`, write capabilities, App permission change | **yes (34 reserved)** | `ActionProposal` | double-click, expiry, wrong confirmer, injection corpus | S5b, S6, owner D4 | none | 3 h x 2 |
| S9 hardening | Rate-limit tuning, optional token rotation, Grid, runbook, alerts | docs, metrics, `infra` | no | none | 429 chaos | S4 | S8 | 2 h |

Critical path: S0, S1, S2, S3/S4, S6. S7 must land **before any second org is invited**.
Parallel: after S2 merges, S3, S5, S6, S7 can run in separate worktrees; shared hot files are `later_migrations.py`, `row_security.py`, `ticks.py`
(append-only, small), so reserve migration numbers 32/33/34 up front. Total v1 (S0 to S4, S5, S6, S7): about 7 slices, 18 to 22 h of agent time
(ASSUMPTION), roughly the size of Phase 5.

Rollout: ship dark (`WEAVES_SLACK_ENABLED=false`), enable on the owner's dev workspace, owner live smoke, then per installation.

## 7. Decisions needed from the owner

1. **What "power itself from Slack" means.** Options: (a) core actions (ask, create task, review PR, status) work without opening the web
   app, while setup/admin stays in the web app; (b) full parity including connecting connectors, creating agents and managing org settings from
   Slack; (c) Slack as the identity/login for Weaves (Sign in with Slack) so Slack users never create a separate account; (d) Slack as the
   customer's only surface (a Slack-native app with Weaves as hidden backend, Marketplace listed, billing via Slack); (e) agents proactively
   operating in Slack (monitoring channels, posting digests). Recommendation: **(a)**, because it covers every requirement in the brief with
   the least security surface; (c) and (e) are separate products on top.
2. **Allowed-users model.** Options: any workspace member; linked Weaves members only; linked members plus per-channel allowlist. Recommend
   **linked members plus channel allowlist** (3.5): the only model where Slack membership alone grants nothing.
3. **PR review variant.** A Slack-only (read-only GitHub, nothing to re-approve), B post on GitHub (needs Pull requests: write and installer
   re-approval, plus confirm button), both. Recommend **A in v1**, B after the confirm/proposal backend exists, because B is a write and removes
   the "nothing happened on GitHub" safety.
4. **Do writes belong in v1?** Options: none; GitHub review comment only; also Jira comment. Recommend **no writes in v1**: the platform has no
   approval backend (1.3) and injection risk jumps with writes (4.1).
5. **Agent selection.** One default agent per workspace, or per-channel mapping. Recommend **one default now, per-channel field in the data
   model** so no migration is needed later.
6. **Public-channel answers from private connectors.** Options: block, ephemeral/DM only, allow with admin confirmation per connector.
   Recommend **per-connector "shareable in Slack" flag, default off; non-shareable answers go ephemeral** (4.1).
7. **Managed AI dependency.** Options: BYOK only (today), wait for managed AI, build a minimal managed key for the bot. Recommend **BYOK only
   in v1**; the bot refuses with a clear message when no provider is configured, and a managed-AI entitlement check is a reserved hook (3.8).
8. **Marketplace timing.** Options: never, after design partners hit Slack's limits (1 request/min, 15 messages), at public launch. Recommend
   **decide at the first external install**; ship v1 as a distributed non-listed app, accept the thread-context limit, and prepare the policy
   pages in parallel because they are cheap. Listing also needs 10 active workspaces (5.3).
9. **Smaller calls (recommend all as stated).** Enterprise Grid: refuse org-wide installs in v1. Token rotation: off in v1 (irreversible once on).
   Slack-origin message retention: keep as task data, no archive, subject to legal review of Slack's API terms (4.2).

## 8. Risks and what not to build now

Risks

1. **Leakage through shared connector tokens** (section 4.1). Mitigation: default-off Slack connectors, ephemeral replies, channel allowlist.
2. **Slack rate limits** on thread context (1 request/min, 15 messages) for non-Marketplace installs; the sources conflict on whether existing
   installs are included. Mitigation: mention-only context, per-workspace serialization, graceful degradation.
3. **Slack API terms** (storage, LLM use) are stricter than the product assumes; reading is from secondary sources. Mitigation: legal review first, no
   Slack archive, no bulk reads.
4. **Misuse of the 3-second ack under load**: an overloaded API or a slow DB would cause retries and eventually disabled subscriptions. Mitigation: the
   handler does one signature check and one insert; test with a slow-DB fake; metric `slack_ack_seconds`.
5. **Agent runtime is not an agent loop**: single query string, five documents, no structured tool input; PR review quality and "answers from
   connectors" will be shallow until a tool loop exists. Do not oversell in copy.
6. **Write path does not exist** and the approvals backend is removed; every write feature depends on building a proposal/confirm service first.
7. **Single VM capacity** (4 worker slots, per-org running cap 2, `docs/capacity-envelope.md`): busy channels queue; status must say "queued".
8. **Stale links and operational coupling**: membership is re-derived on every event; signing-secret rotation and bot-token loss need runbook steps (S9).

Not building now: Socket Mode; Grid org-wide install; DMs and `message.*` follow-ups; slash commands and App Home; any external write or confirmation
flow; automatic PR reviews from webhooks; Slack as login; a channel-history index; email-based user matching; token rotation; Marketplace submission;
managed-AI entitlements; a general agent tool loop (a separate product decision that would improve every flow here).

## Sources (read 2026-11-26 unless noted)

Slack (docs.slack.dev): `/authentication/verifying-requests-from-slack`, `/apis/events-api/`, `/reference/events/app_mention`, `/reference/events/tokens_revoked`,
`/authentication/installing-with-oauth`, `/authentication/using-token-rotation`, `/apis/events-api/using-socket-mode`, `/reference/methods/chat.postMessage`,
`/reference/methods/chat.postEphemeral`, `/interactivity/handling-user-interaction`, `/interactivity/implementing-slash-commands`,
`/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps`, `/slack-marketplace/slack-marketplace-app-guidelines-and-requirements`.
Search-result only (not read in full): app_uninstalled, team_access_revoked (api.slack.com/events), rate-limits-clarity changelog, Slack API Terms
(slack.com/terms-of-service/api) and Hunton/Cloud Wars write-ups. GitHub (docs.github.com): `/en/rest/authentication/permissions-required-for-github-apps`,
`/en/rest/pulls/reviews`, `/en/rest/pulls/pulls`, `/en/webhooks/webhook-events-and-payloads` (the `pull_request` action list was not in the fetched part; actions
named in F3 are from prior knowledge, UNVERIFIED). Not verified anywhere: message-shortcut scope needs, per-workspace Enterprise Grid event behaviour, which
event additions force a re-install.
