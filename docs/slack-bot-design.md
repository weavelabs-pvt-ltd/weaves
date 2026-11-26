# Slack bot design: tag, create tasks, PR reviews, answers from connectors

Status: design for owner review, 2026-11-26. Docs only; no real Slack/GitHub calls were made. Code refs are to `origin/main` at `b50f4c3`.
External facts cite their source and read date; "UNVERIFIED" marks what I could not confirm from a primary page.

Requirement (owner, 2026-11-26): Weaves is a bot in a client's Slack workspace that answers when tagged (in thread), creates tasks, triggers GitHub PR
reviews, answers from org connectors (GitHub, Slack search, Jira, Notion, Datadog, custom MCP) and similar flows.

## 0. Summary

- The platform already has tasks/worker, limits, usage ledger, request ids, kill switch, purge, secrets and RLS; missing is the Slack edge (installation, signed endpoint, inbox, access policy, reply path).
- Access is two-tier (owner, 2026-11-26): **Ask** (pure Q&A, open to every Slack user, but only over data the admin marked shareable) and
  **Execute** (create task, PR review, run workflow, anything that persists work, spends meaningful AI or writes; off until an admin allowlists
  channels/users per action in a dashboard "Slack access" page). Connector tokens are org-wide, so Ask must never reach unticked connectors.
- No tool loop and no write path (approvals backend removed in migration 31): v1 is read-only answers, task creation, Slack-only PR review.
- Non-Marketplace apps get 1 request/min and 15 messages on `conversations.replies`. Recommended v1: slices S0 to S7 (section 6).

## 1. Current state (evidence)

### 1.1 Slack today is a read-only, user-token connector

| Fact | Evidence |
|---|---|
| OAuth start requests only **user** scopes `search:read,channels:read,groups:read,channels:history,groups:history`; no `scope=` (bot) parameter | `modules/integrations/http/oauth_start.py:78-84` |
| Callback reads only `payload["authed_user"]["access_token"]`; the bot token and `bot_user_id` Slack returns are ignored and not stored | `modules/integrations/http/oauth_slack.py:50-57`; `team_id`/`team_name` are only copied into a setup record, `:78-79` |
| Plugin supports one capability, `slack.messages.search`, via `search.messages` (+ optional `conversations.replies`, limit 10), restricted to configured channel names and quoting the query so operators cannot widen scope | `clients/integrations/slack.py:48-69, 85-112, 130-150`; seeded at `modules/integrations/bootstrap.py:212-240` |
| Channels discovered with the user token (`conversations.list`); setup docs list only user scopes and a Redirect URL; no signing-secret variable exists | `modules/integrations/http/oauth_resources.py:165-190`; `infra/gcp/singlevm/CONNECTORS.md` sections 1-2 |

The Slack app "Weaves" is registered (per owner): client id/secret on the server, signing secret offline, bot scopes `app_mentions:read, chat:write,
channels:history, groups:history`, the user scopes above, Event Subscriptions OFF. Nothing in the repo reads the signing secret yet.

### 1.2 No inbound chat or event triggers

`contracts/v1/triggers.py:1-33` defines one trigger type, `WorkflowTrigger.trigger_type: Literal["webhook"]`, authenticated by a stored secret digest. The
only public inbound route is `POST /api/v0/webhooks/{trigger_id}` (`modules/workflows/http/automation_routes.py:318-349`): secret header, `X-Event-ID`
idempotency, 202 with a `replayed` flag. A good pattern for "verify, store event id under a unique key, then act", but it has no HMAC and cannot verify
`X-Slack-Signature`.

### 1.3 The agent runtime

- `modules/agents/runtime_run_capabilities.py:42-98` calls each allowed capability **once** with `payload={"query": task}` (`:127-134`) and skips
  `ACTION_WRITE` ("proposed explicitly and run only after approval", `:67-69`). Output: at most 5 documents of 2,000 characters
  (`modules/integrations/context_search.py:24-44`). Budgets: `max_tool_calls` (`:70`), `admission.admit_tool_call` (`:74`). Which connectors run is fixed by
  the agent version's `allowed_capability_ids` (`:42`), which the Ask tiering relies on.
- That approval flow is gone: migration 31 removed `approval_policies`/`approval_requests` (`infrastructure/database/approvals_removal.py:1-27`).
  `jira.issues.comment` is still `ACTION_WRITE` (`modules/integrations/bootstrap.py:132-137`) and nothing can approve it: **writes have no approval path.**
- GitHub connector: `github.issues.search` and `github.code.search` only (`clients/integrations/github.py:99-100`); it cannot read a PR diff or files. Tokens
  are minted with `contents`, `issues`, `pull_requests` all `read` (`clients/integrations/github_app_auth.py:72-77`); the App has webhooks off (`CONNECTORS.md`).

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
| Tombstones (file-based JSONL) and RLS purposes | `docs/DATA_MODEL.md` "Tombstones"; `infrastructure/database/row_security.py:30-65` | Slack token deletion appends a tombstone; new purpose `slack` |
| Secret store (Fernet, `purpose` tag) and egress policy | `infrastructure/secrets/store.py:40,85,200`; `clients/model_providers/egress_policy.py:1-36`; `clients/integrations/oauth.py:140` | purpose `slack_bot_token`; outbound only to `slack.com`, `hooks.slack.com` (for `response_url`), `api.github.com` |
| Real-stack fakes pattern (fake Google OIDC) | `tenancy_harness/fake_google.py:1-30` | write `fake_slack.py` the same way |

### 1.5 Gaps the bot must close

No bot-token storage or `team_id -> org` mapping; no signed public endpoint with a global event-id inbox; no Slack-user to member mapping (tasks need a
principal, `tasks/service.py:127`); no reply path or Slack origin on tasks; no GitHub PR files/diff capability, no write approval path, and connector
reads take only a query string; model access is BYOK per org (managed AI is a future dependency).

## 2. User flows

Common rules: the request is acked within 3 s after the inbox insert and all work is async; the bot answers in the thread of the triggering message;
every request is classified **Ask** (Tier 1) or **Execute** (Tier 2) by the dispatcher, not by the model, and checked by the access policy (3.5);
org suspended/quarantined/over limit gives a short refusal and creates no task; nothing a Slack message says may widen permissions. Blocked users
see the plain messages in 3.5 ("ask a Weaves admin"); there is no invitation or access-request flow.

### F1. Ask: tag to answer (v1, Tier 1)

- Trigger: `@Weaves <question>` by **any user** in a channel the bot was invited to, or in its thread (thread_ts = parent). DMs later.
- Steps: endpoint verifies and stores `app_mention`; dispatcher resolves team -> org, classifies Ask, picks the Q&A agent version (open or trusted,
  3.5), creates a hidden Q&A task as the Slack service principal (idempotency key `slack:{team}:{event_id}`); the worker runs it (connector reads once);
  the `slack_reply` tick posts the answer in the thread.
- Who: everyone, linked or not, within Ask rate limits. Open answers use model knowledge plus **only connectors ticked "available to everyone in
  Slack"**; users allowed for `ask_private` also get unticked connectors, answered ephemerally.
- Bot posts: optional reaction, then one threaded message: answer (about 3,000 characters max, longer links to Weaves), "Sources" from the documents'
  `title`/`source_id`, and a task link (hidden Q&A tasks link to nothing).
- Failure: a question naming a non-shared source gets the "needs access" message (3.5); a failed turn posts "I could not finish this (code, request id)"; no
  model provider posts "ask an admin to connect a model"; admission refusal posts the limit and retry-after; past `WEAVES_SLACK_REPLY_DEADLINE` (15 min,
  ASSUMPTION) one timeout note; post failures retry with backoff, then `undeliverable`.

### F2. Create a task from Slack (v1, Tier 2)

- Triggers: (a) `@Weaves task: <text>` (v1, same path as F1); (b) message shortcut and (c) `/weaves task <text>` (S5b; developer slash commands cannot be
  used in threads, per Slack docs).
- Who: action `create_task` per the access rules (3.5), then `tasks.create`+`agents.run` of the mapped member. The task is created and the bot replies with title, link and "I'll post the result here" (watch mode, same thread).
- Failure: ambiguous intent falls back to F1 (answer), never to an unannounced write in another system.

### F3. PR review (two variants, Tier 2)

- Trigger: `@Weaves review https://github.com/org/repo/pull/123` (strict URL parse). Allowed only if the access rule for `pr_review` passes **and** the repo is in the GitHub installation's repositories **and** on the admin's PR-review repo allowlist.
- Variant A, Slack-only (v1, recommended): works with existing read permissions. New capability `github.pr.review_context` reads `GET /pulls/{n}` and
  `/pulls/{n}/files` (diff byte-capped; GitHub caps the files list at 3,000; which patches are omitted is UNVERIFIED, so tolerate missing patches). Capabilities
  get only `{"query": task}` (`runtime_run_capabilities.py:133`), so it parses the PR URL from that text. Reply: summary, risks, per-file notes, questions.
- Variant B, post on GitHub (later): needs **Pull requests: write** for `POST /pulls/{n}/reviews` (GitHub "Permissions required for GitHub Apps") and installer
  re-approval (5.2); posted as `COMMENT` only, from the App identity; it is a write, so a F5 confirmation is required. Automatic reviews on `pull_request`
  webhooks need webhooks on, `X-Hub-Signature-256` verification, `X-GitHub-Delivery` dedupe and a per-repo opt-in. Not v1.
- Failure: unreadable PR gets one generic "I cannot read that PR"; a large PR gets a partial-review notice listing what was skipped.

### F4. Status: "what is the agent doing" (v1, small, Tier 1 for the asker's own threads)

- Trigger: `@Weaves status` in a task thread (mapped via `slack_threads`), or `/weaves status`.
- Bot posts an ephemeral message (not persisted; may not show in an inactive thread) with state, step label, elapsed time and a Weaves link, from `task_turns`/`tool_invocations` summaries, no content.

### F5. Approvals and confirmation for anything that writes (later, gated)

- Rule: no external write from free text alone. A write is a **proposal**: Block Kit message with exact parameters, Confirm/Cancel, 30-minute expiry,
  confirmers limited to the requester plus admins, single-use, signature-verified (`response_url`: 5 uses/30 min; `trigger_id`: 3 s). Needs a proposal
  backend that does not exist (1.3). Applies to GitHub review/comment, Jira comment, PR generation.
- Failure: expired or double-clicked proposals show "already handled"; an unauthorized confirmer gets an ephemeral refusal and an audit event.

### Cut line

| In v1 | Later |
|---|---|
| Tier 1 Ask for every Slack user (model-only plus admin-ticked connectors), per-user limits, `ask_private` for allowlisted users | F5 writes and confirmations; Jira comment; PR generation |
| Tier 2 `create_task` and `pr_review` (Slack-only), off until configured; Slack access page, allowlists, audit | `run_workflow` (v1.1, S6b); message shortcut, slash command; PR review posted on GitHub |
| Status (ephemeral); install, uninstall, kill switch, purge, limits | DMs, `message.*` follow-ups, channel picker (needs `channels:read`), self-link flow, Grid, Marketplace, managed AI |

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
| `GET/PUT /api/v0/organization/slack/access` (+ allowlist routes) | the "Slack access" page (3.5); PUT bumps `version` | `slack.manage` (Owner/Admin) |

Caddy already proxies `api.weave-studio.in` to `product-api`. New env `WEAVES_SLACK_SIGNING_SECRET` (API only; single quotes in `runtime.env`, never in Git). The endpoint never logs bodies, has its own global token bucket, and returns 200 for unknown teams after verification (no oracle).

### 3.2 Ack, retries, idempotency

Slack requires a 2xx within 3 seconds and retries up to 3 times (almost immediately, after 1 min, after 5 min) with `x-slack-retry-num`/`-reason`;
`x-slack-no-retry: 1` stops retries; subscriptions are temporarily disabled if more than 95% of deliveries fail in 60 minutes (Events API docs). `event_id`
is documented as globally unique, but **the docs give no explicit dedupe guidance, so we design it**: `slack_event_inbox(event_id PK, team_id, event_type,
received_at, state, attempts)` with `ON CONFLICT DO NOTHING`; a conflict acks 200 without work. A second guard `(team_id, channel, event_ts)` stops one
message producing two tasks under different event ids. Only ids and the mention text are kept, deleted 7 days after processing (ASSUMPTION), so the inbox
is not an archive. Dispatch is idempotent through the task idempotency key (`tasks/service.py:118,129-139`), so a crash before "mark done" replays safely.

### 3.3 Data model (migrations 32 and 33)

Migration head is 31 (`infrastructure/database/later_migrations.py:22-33`). Number 29 is unused (tombstones are file-based). Reserve **32** (S1/S2 global tables),
**33** (S3 indexes and role backfill), **34** (S7, only if needed), **35** (S8) up front so parallel slices do not collide. New module
`infrastructure/database/slack_schema.py`, appended to `LATER_MIGRATIONS`.

| Table / collection | Scope | Key columns |
|---|---|---|
| `slack_installations` (relational, global) | one row per workspace | `team_id` UNIQUE (active), `enterprise_id`, `org_id`, `workspace_id`, `app_id`, `bot_user_id`, `bot_secret_ref`, `scopes`, `installed_by_principal_id`, `installed_by_slack_user`, `status` (active/revoked/uninstalled/suspended), `installed_at`, `revoked_at` |
| `slack_event_inbox` (global) | dedupe + queue | see 3.2; RLS only for purpose `slack` |
| `slack_threads` (product_records, org) | Slack thread to task | `team_id, channel_id, thread_ts, task_id, requester_slack_user_id, requester_principal_id, tier, reply_state` |
| `slack_access_settings` (product_records, org, one row) | the dashboard page (3.5) | `version`, `ask_enabled`, `ask_open_installation_ids[]`, `ask_limits{user,channel,org_day}`, `rules{create_task, pr_review, run_workflow, ask_private}` each `off|user_only|channel_only|user_and_channel|user_or_channel` (default `off`), `workflow_ids[]`, `pr_review_repos[]`, `qa_principal_id`, `qa_agent_versions{open,trusted}`, `paused`, `updated_by`, `updated_at` |
| `slack_allowed_users` (product_records, org) | user allowlist | `slack_user_id` (unique per org), `principal_id` (null = listed by Slack id only), `note`, `added_by`, `added_at` |
| `slack_allowed_channels` (product_records, org) | channel allowlist | `channel_id` (unique per org), `name_cached`, `added_by`, `added_at` |

Global rows need no tenant context, so they follow the waitlist/oauth-state pattern: system purpose `slack` in `row_security.py` sees only
`slack_installations` and `slack_event_inbox`; org-scoped collections get `ownership.json` entries (a test fails otherwise) and a purge/export decision in
`modules/auth/organization_retention.py`. Migration 33 adds the partial unique indexes for the allowlists and an idempotent backfill adding permission
`slack.manage` to stored Owner/Admin roles (as 31 touched roles, `approvals_removal.py:1-27`; confirm the seed path in S3). Audit reuses `audit_events`.

### 3.4 Installation and workspace to org binding

- Install starts in Weaves (Settings, "Add to Slack"), so the **org is chosen by a signed-in Owner/Admin**, never inferred from Slack. Authorize URL:
  `scope=app_mentions:read,chat:write,channels:history,groups:history`; `state` row binds org+principal (pattern `oauth_start.py:77-83`). The install
  screen shows the Slack access defaults (3.5) before redirecting.
- Callback calls `oauth.v2.access` (helper as in `oauth_slack.py:34-38`), reads bot `access_token`, `bot_user_id`, `team.id`, `app_id`, `enterprise`,
  `is_enterprise_install` (Slack "Installing with OAuth"), stores the token (`purpose="slack_bot_token"`, `owner_ref=team_id`) and writes the installation
  row. A second org claiming an active `team_id` is refused (operator can free it); re-install by the same org updates in place.
- **Enterprise Grid:** org-wide installs (`is_enterprise_install: true`, `team` null) are refused in v1; per-workspace installs work (per-workspace events
  UNVERIFIED). Token rotation stays OFF in v1: irreversible, 12-hour tokens, single-use refresh tokens (Slack docs); if enabled later the worker refresh
  pattern exists (`CONNECTORS.md` 2.6) and bot tokens need `oauth.v2.exchange` first (S9).

### 3.5 Access model: Ask (Tier 1) and Execute (Tier 2)

**Classification** is code: the dispatcher parses intent with fixed grammar (`task:`, `review <PR url>`, `run <workflow>`, `status`, else Ask). Execute actions are
those that persist work for members, spend meaningful AI, or write: `create_task`, `pr_review`, `run_workflow`, later `pr_generate` and any write. An unclear
message is Ask, never Execute.

**Tier 1 Ask, open by default.** Any Slack user of the bound workspace in a channel where the bot was invited (linked or not; Slack's own invite is the channel
gate). Questions run as a per-org Slack service principal (`PrincipalType.SERVICE_ACCOUNT`, `contracts/v1/identity.py:40`) holding only `agents.run` and
`tasks.create` on the Q&A agent. Tasks are `audience=PRIVATE` to that principal with `origin.kind="slack_qa"`, hidden from the task list, and deleted by a
retention tick after 30 days (ASSUMPTION). Two pinned agent versions (`pinned_version_id`, `tasks/service.py:123`): **open** = model knowledge plus
connectors ticked "Available to everyone in Slack"; **trusted** = all enabled connectors, used only when `ask_private` passes (below) and answered
ephemerally. Because the runtime has no tool loop, the open version simply does not hold unticked connectors, so a prompt cannot reach them.
- Open-Q&A leak control: a connector is only offered in the dashboard with a risk label; the Slack-search connector (acts as its installer) is labelled
  "not recommended" and stays inside its configured channel list. Defaults: no connector ticked, so answers are model-only.
- Non-shareable need and user not allowed: answers are never silently degraded. If the text names an unshared source (Jira issue key regex, "Datadog",
  an unshared host/URL, a repo not allowlisted), the bot posts ephemerally: "That needs <Jira>, which isn't open to everyone in Slack. Ask a Weaves admin
  for access." Otherwise the open answer carries a footer line naming the sources used. (A tool-less runtime cannot know a question "needed" a connector, so
  detection is explicit-mention only: UNVERIFIED value, listed as a risk.)
- Rate limits (it spends the org's AI key): per Slack user 5 questions/10 min, per channel 20/10 min, per org 200/day, question capped at 1,000 characters
  (ASSUMPTION, editable on the page); open Q&A may hold at most 1 of the org's 2 running slots (`WEAVES_LIMIT_MAX_RUNNING_PER_ORG`) so Execute work is not starved.

**Tier 2 Execute, off until configured.** For each action type the admin picks a rule: `off` (default), `user_only`, `channel_only`, `user_and_channel`,
`user_or_channel`. "User" = Slack user in `slack_allowed_users`; "channel" = channel in `slack_allowed_channels` (DMs never satisfy a channel rule).
Independent hard checks always apply on top: the user must map to an **active Weaves member** (`principal_id`) whose role has the Weaves permission (`tasks.create`
+ `agents.run`, or `workflows.run`); unlinked Slack ids may never execute (recommended, D2b); `pr_review` needs the repo in the GitHub installation
**and** in `pr_review_repos`; `run_workflow` needs the workflow published and in `workflow_ids`. `ask_private` (trusted Ask) uses the same rule engine and
may allow users listed by Slack id only, since it is read-only.

**Mapping** is admin-asserted in the dashboard (pick a Weaves member and paste their Slack member ID, shown in Slack profile > Copy member ID); every
assertion is audited. No self-link or invitation flow in v1; the one-time self-link is a later convenience. No email matching (needs `users:read.email`).

**Validation at request time and at pickup.** The dispatcher evaluates the policy when it claims the inbox row and writes the decision. Settings can change
before the worker runs, so the policy is re-evaluated with the current `slack_access_settings.version` in a small pre-run guard in
`modules/tasks/worker_service.py:_process_claimed_turn` (`:111`) for turns whose `origin.kind` is `slack`: now blocked means the turn is cancelled with reason
`slack_access_revoked`, audited, and the thread gets the blocked message. Saving settings also cancels still-queued Slack turns that no longer pass (bounded
batch). Running turns finish; the kill switch still applies at claim.

**What the user sees** (ephemeral to the asker; if Slack drops it in an inactive thread, one generic threaded line without internals):

| Cause | Message |
|---|---|
| Execute rule `off` | "Running things from Slack isn't turned on for this workspace. Ask a Weaves admin." |
| User not listed | "You're not on the list for this from Slack. Ask a Weaves admin." |
| Channel not listed | "This isn't enabled in this channel. Ask a Weaves admin." |
| Repo / workflow not enabled | "That repository (workflow) isn't enabled for Slack." |
| No Weaves member for that user | "Your Slack account isn't connected to a Weaves member. Ask a Weaves admin." |
| Ask rate limit | "You're asking a lot; try again in N minutes." |

Messages never name who is on a list. **Audit:** every Execute attempt (allowed or blocked) writes an `audit_events` row `slack.execute.allowed|blocked`
with action, rule, reason code, `settings_version`, team, channel, Slack user, principal, event id and request id, no text. Repeated blocks by one user,
action and reason merge into one row with a counter per 10 minutes (ASSUMPTION) so a spammer cannot flood audit. Ask is counted as metrics
(`slack_ask_total{outcome}`), not audited per question, except rate-limit trips.

**Dashboard "Slack access" page** (new frontend page; edit permission `slack.manage`, Owner/Admin by default; members may view nothing):
1. Status: workspace, bot state, Pause bot (`paused`), Reconnect/Disconnect.
2. "Ask: open to everyone in Slack": toggle (default on after install), connectors table with checkbox "Available to everyone in Slack" (default none) and
   the three limits. Notice: "Anyone in a channel where @Weaves is added can ask questions. Answers use your AI key and only the sources you tick here."
3. "Run things from Slack": a rule dropdown per action (Create task, Review PR, Run workflow, Generate PR shown disabled "coming later", Private data in
   answers), default "Off".
4. Allowed people (member, Slack member ID, note; hint "Only these people can run the actions above") and allowed channels (paste the channel ID from
   "Copy link"; a picker needs `channels:read`/`groups:read`, v1.1).
5. Allowed repositories (repos the GitHub installation covers, stale entries flagged) and workflows ("Triggerable from Slack", published only).
6. Recent Slack activity: last 50 allowed/blocked Execute attempts, no content.

Bots read only channels they are in and private channels need an invite (Slack `app_mention` docs). Execute uses the org's chosen agent per action.

### 3.6 Tasks and attribution

Execute tasks are created through `TaskService.create_task` as the **mapped member's principal** (RBAC, `TASKS_CREATE`, `AGENTS_RUN` and audit use
existing paths); Ask tasks run as the Slack service principal (3.5). Both carry a new optional `origin` object (JSON, no migration):
`{"kind":"slack"|"slack_qa","team_id","channel_id","thread_ts","slack_user_id","event_id","settings_version"}`. Audit events carry actor and origin. Follow-ups
in a thread append a message (`add_message`, `tasks/service.py:359`) re-checked against the same tier rules.

### 3.7 Replies

- `chat.postMessage` with `thread_ts` = the parent message `ts`; keep text under 4,000 characters (truncated beyond 40,000; Slack docs). Model mrkdwn is
  sanitized: `<!channel>`, `<!here>`, `<@U..>` and unlisted links neutralized, `parse=none`, `unfurl_links/media=false`.
- About 1 message/second per channel; at most 2 posts per request. Citations list connector document titles/ids only, never invented links.
- `slack_threads.reply_state`: `pending -> posted | undeliverable`; the returned `ts` is stored and checked before re-posting (no Slack idempotency key; a rare duplicate is accepted).
- Thread context: `conversations.replies` is, for commercially distributed apps outside the Marketplace, rate tier 1 (1 request/min) and 15 messages per
  request, for new installs since 2025-05-29 and existing installs from 2026-03-03 (changelog 2025-05-29 says existing installs are unaffected while a search
  summary says 2026-03-03: **conflicting, UNVERIFIED**; plan for the stricter reading). Internal customer-built apps keep 50+/min. Design: mention text only
  by default; at most one thread fetch per request, cached 5 minutes, serialized per workspace, skipped with a note when limited.

### 3.8 Cost control

Every Slack-created task (Ask and Execute) passes `admit_task` and, in the worker, `admit_tool_call` and usage reservation, as web tasks do. Separate Ask and
Execute buckets per Slack user, channel and org (Ask defaults in 3.5; Execute 10/10 min per user, 30/10 min per channel, ASSUMPTION); refusals are ephemeral
with retry-after. `usage_daily` already rolls up by org/agent; `origin.kind` allows later reports. BYOK today: the customer's key pays. With managed AI, the
bot must not become a free proxy: an entitlement check before dispatch (D7).

### 3.9 Kill switch, quarantine, suspension

The dispatcher re-reads `platform_controls.admissions_paused`, org lifecycle state, `org_quarantines`, installation `status` and settings `paused` at every
stage (order: platform/org, installation, `paused`, rate limits, tier rules). Quarantined or suspended org: inbox rows `dropped(reason)`, at most one neutral
"Weaves is temporarily unavailable" per channel per hour. `read_only` platform flag: Ask allowed, Execute refused. Quarantine gains a step "installation
suspended" (S7), reversed by `release-quarantine` through the recorded `changes` (6.4 pattern). `WEAVES_SLACK_ENABLED=false` acks and drops everything.

### 3.10 Uninstall, revocation and deletion

- Subscribe to `app_uninstalled` and `tokens_revoked` (user ids per token kind, not token strings; no ordering guarantee between them; Slack docs). On
  either: installation `uninstalled`/`revoked`, bot token deleted from the secret store (tombstone), dispatch stops, pending replies `undeliverable`. A
  failed post with `account_inactive`/`invalid_auth`/`token_revoked` does the same, since events can be missed while the endpoint is down.
- Org purge: new step `slack` in `modules/auth/organization_purge.py` (`PURGE_ERASED`, `:52`) removes the installation, settings, allowlists, threads and
  inbox rows and the secret, appends a tombstone and calls `auth.revoke` best effort. Export includes settings, allowlists and thread maps, not message text.

### 3.11 Observability

Request id `slack-{event_id}` (valid per `shared/correlation.py:36`) is bound at ingest and stored on inbox row, task, turn and `slack_threads`. Logs carry ids,
event type, outcome and latency, never message text, tokens or `response_url`. Metrics (org id never a label): `slack_events_total{type,outcome}`,
`slack_ask_total{outcome}`, `slack_execute_total{action,outcome}`, `slack_inbox_age_seconds`, `slack_ack_seconds`, `slack_api_429_total{method}`. Alerts: inbox
age above 120 s, ack p95 above 1.5 s, signature-failure spikes, Slack's disabled-subscription email.

## 4. Security and abuse

### 4.1 Authorization and data leakage (the main risk)

- **Who may trigger**: Ask is open; Execute needs the allowlist rule (3.5) plus the mapped member's Weaves role permission, never Slack roles.
- **Connector tokens are org-shared**: a connector installation is workspace-scoped (`PluginInstallation.auth_ref`,
  `contracts/v1/plugins.py:127-131`), and the Slack search connector runs with the **installing user's** user token
  (`oauth_slack.py:50-57`), so any answer that uses it can quote messages from channels the asker cannot see. Defaults:
  1. Open Ask uses an agent version that holds only connectors ticked "Available to everyone in Slack" (default none), so a prompt cannot reach others.
  2. Unticked connectors are reachable only through `ask_private` (allowlisted users), always answered ephemerally to the asker, never in channel.
  3. The Slack connector's own channel list (`clients/integrations/slack.py:48-69`) stays authoritative: the bot never searches beyond it.
  4. Admin UI warns: answers in a channel are visible to everyone in it, including guests and Slack Connect members; Slack does not tell us who is a guest without `users:read` (D2c).
- **Prompt injection** (Slack text, PR diffs): today the agent has read-only calls with bounded output and no write path, so the worst case is a
  misleading or data-revealing answer, which the tier split bounds (open Ask holds no private connector). System prompt marks Slack text and diffs as
  untrusted; connector results cannot pick tools; mentions and links in replies are neutralized (3.7). **With writes** injection becomes serious: typed
  parameters shown verbatim in the confirmation, per-capability allowlist, no hidden parameters derived from fetched content.
- **Rate limiting**: per Slack user, channel and org (3.8) plus a global ingest bucket; Slack's own cap is 30,000 events per workspace per hour.
- **Replay and forgery**: raw-body HMAC with a 5-minute window, constant-time compare, unique `event_id`; retries are deduped, not special. Interaction
  payloads: signature, then proposal id and state checks. The verification token is deprecated and unused.
- **Secrets**: signing secret in `runtime.env`, bot token only in the Fernet store; none in logs, args or CI (AGENTS.md); `response_url`/`trigger_id` never logged.
- **SSRF/egress**: outbound only to `slack.com/api` (fixed base, as `clients/integrations/slack.py:30`), `hooks.slack.com` for `response_url` (exact host,
  https, no redirects) and `api.github.com`. No `files.*` fetches; links in Slack text are never followed server-side.
- **Cross-tenant**: events for an unbound `team_id` are acked and dropped; tenant context comes from the installation row before any tenant table is read.

### 4.2 Privacy, terms and sub-processors

Slack becomes a new inbound data source and the bot a processing path. Before external customers: privacy notice (Slack text and user ids processed to
answer, retention, deletion on uninstall), sub-processor list (Slack for delivery; the org's model provider for content, already BYOK), terms saying the
customer authorizes the bot for the channels and people it configures. Slack's API Terms (updated 2025-05-29/06-30) restrict storing and reusing Slack
data; secondary sources (Cloud Wars, Hunton; UNVERIFIED against the primary text) report bans on bulk export, persistent copies/indexes and LLM training,
and the Marketplace guidelines say AI agent apps should not store Slack API data. Design consequence: no Slack archive or index; only mentions addressed to us and our replies are kept. Legal review first.

## 5. Slack app configuration

### 5.1 Per version (exact)

| Version | Bot scopes | User scopes | Events | Interactivity / commands | Other |
|---|---|---|---|---|---|
| v0 (registered today) | `app_mentions:read, chat:write, channels:history, groups:history` | `search:read, channels:read, groups:read, channels:history, groups:history` | none (OFF) | none | Redirect URL `https://api.weave-studio.in/api/v0/oauth/slack/callback` |
| v1 | same four (no change; re-install not needed for existing installs of the bot) | same (connector only) | Request URL `https://api.weave-studio.in/api/v0/slack/events`; bot events `app_mention`, `app_uninstalled`, `tokens_revoked` | none | add second redirect URL for bot install; bot token via `scope=`; "Always show my bot as online" off; public distribution on only when D8 says so |
| v1.1 | add `reactions:write` (ack emoji, optional); `channels:read` + `groups:read` (channel picker on the access page) | none | add `message.channels`, `message.groups` only if no-mention follow-ups are chosen | Interactivity Request URL `.../slack/interactions` (needed for F5 and shortcut) | message shortcut "Create Weaves task" (no scope; UNVERIFIED) |
| v2 | add `commands` (slash command) and `im:history` + `im:write`/`message.im` if DMs | none | `message.im` | `/weaves` URL `.../slack/commands` | optional `chat:write.public` is **not** requested |

Scope notes: `chat:write.public` and `users:read.email` are avoided; the user scopes belong to the connector and are never merged into the bot install
(Marketplace: user scopes only when the action must be the user's).

### 5.2 What customers must re-approve

Scopes are additive and removable only by revoking (Slack OAuth docs); adding one (v1.1 `reactions:write`, `channels:read`, `groups:read`; v2 `commands`,
`im:*`) sends the installer through OAuth again, and workspaces with app approval need an admin to approve. New events need no re-install unless a scope is
new (UNVERIFIED per event). GitHub **variant B** needs the App owner to raise Pull requests to Write; each installation then shows a pending permission
request the org owner must accept (tokens stay read-only until then). One coordinated notice.

### 5.3 Marketplace readiness and timing

Needed: public landing page with privacy policy and support contact (reply in two business days), individually justified least-privilege scopes, TLS 1.2+,
signature verification, OAuth `state`, onboarding for users without an account, 10 active workspaces and 20 monthly active users (28 days); Socket Mode apps
cannot be listed, so HTTP is the design. Timing: not before partners hit the 1/min thread limit or want discovery.

## 6. Slice plan

Durations are an ASSUMPTION from Phase 5/6 slices (1.5 to 3 h of agent time each plus CI). Every slice ends with unit tests, PostgreSQL tests under the
restricted role, a real-stack control against `tenancy_harness/fake_slack.py` (fake `oauth.v2.access`, `chat.postMessage`, `conversations.replies`, signed,
replayable and forgeable events) and, where a model is involved, the budget-gated live run (AGENTS.md). The **owner runs a live smoke on the dev workspace**
after S2, S4 and S6.

| Slice | Goal | Files/modules | Migration | Contracts | Tests beyond common | Depends | Parallel with | Effort |
|---|---|---|---|---|---|---|---|---|
| S0 prelude | Config: `WEAVES_SLACK_SIGNING_SECRET`, `WEAVES_SLACK_ENABLED`, compose forwarding, `pair.json` T7-S* planned, fake Slack server skeleton, `modules/slack/` package, Slack client with fixed base URL | `shared/slack_config.py`, `clients/slack/`, `tenancy_harness/fake_slack.py`, `infra/gcp/singlevm/compose.yaml`, `CONNECTORS.md` | no | none | unit config; fake serves/verifies | none | none | 1.5 h |
| S1 install | Bot install flow, installation row, token in secret store, team to org binding, admin status view | `modules/slack/installation_*.py`, `modules/integrations/http/oauth_slack_install.py`, `slack_schema.py`, `row_security.py` purpose `slack`, `ownership.json` | **yes (32)** | `SlackInstallation` | PG: unique team_id, cross-org claim refused, RLS; harness: install via fake | S0 | S3 (design only) | 2.5 h |
| S2 events ingest | Signed endpoint, url_verification, inbox, dedupe, retries, kill switch checks | `modules/slack/http/events.py`, `modules/slack/inbox.py`, `apps/api/router.py` | **yes (32, same file as S1)** | event envelope models | forged/old/duplicate/retry events, 3 s ack with slow DB, replay, unknown team | S1 | S3 | 2.5 h |
| S3 Slack access | Policy engine (`evaluate(action, user, channel, repo/workflow)` pure function), settings/allowlist collections, admin-asserted user mapping, `slack.manage` permission, settings API with version + queued-turn cancel, dashboard "Slack access" page (weaves-app) | `modules/slack/access/*.py`, HTTP routes, `contracts/v1/slack_access.py`, frontend page + `docs/handoff-specs` | **yes (33)** | `SlackAccessSettings`, `SlackAllowedUser/Channel` | pure-function matrix (5 rules x user/channel listed x linked); PG: unique allowlists, RLS, role backfill, non-admin refused; version bump cancels queued turns | S1 | S2, S4 stubs | 3 h backend + 3 h frontend |
| S4 Ask + dispatch | `slack_dispatch`/`slack_reply` ticks, classification, Slack service principal and two pinned Q&A agent versions, `origin`, Ask limits and 1-slot cap, worker pre-run guard, audit rows, blocked messages, F1, F4, citations, Q&A retention tick | `modules/slack/dispatcher.py`, `reply.py`, `classify.py`, `apps/worker/ticks.py`, `modules/tasks/worker_service.py` (guard), `contracts/v1/tasks.py` (origin) | no | `TaskOrigin` | harness matrix T7-A* (3.5): Ask by unlinked user works; open Ask makes zero calls to unticked connectors (`tool_invocations` count); `ask_private` ephemeral only; blocked Execute makes zero tasks and zero provider calls (relay count); settings change between enqueue and pickup cancels; quarantine, `paused`, `read_only`; per-user limit; live run | S2, S3 | S5 | 3.5 h |
| S5 create task | F2(a): intent parse, rule gate `create_task`, watch mode, idempotency | `modules/slack/intents.py` | no | none | blocked/allowed matrix for `create_task`; ambiguous intent stays Ask; duplicate event | S4 | S6 | 1.5 h |
| S5b shortcut/slash | F2(b),(c): interactions and commands endpoints, ack within 3 s, `response_url` host check | `modules/slack/http/interactions.py` | no | payload models | signature, expired trigger, thread limitation | S4 | S6, S7 | 2 h |
| S6 PR review (Slack-only) | `github.pr.review_context` capability, review agent template, F3 variant A, rule gate `pr_review` + repo allowlist check at dispatch and at pickup | `clients/integrations/github.py`, `modules/integrations/bootstrap.py`, `modules/agents/templates.py`, `modules/slack/intents.py` | no | capability spec | repo covered but not allowlisted, allowlisted but not covered, truncated patches, 3,000-file PR; live run | S4 | S5 | 2.5 h |
| S7 lifecycle and purge | `app_uninstalled`/`tokens_revoked`, quarantine step, org purge step `slack`, tombstone, revocation on `invalid_auth` | `modules/auth/organization_purge.py`, `organization_lifecycle.py`, `modules/slack/lifecycle.py` | **maybe (34)** | none | purge then restore reconcile (T6-I01 style), uninstall during run | S2 | S5, S6 | 2.5 h |
| S6b run workflow (v1.1) | `run <workflow>` intent, `workflow_ids` flag check, member `workflows.run` | `modules/slack/intents.py`, workflows service | no | none | unflagged/unpublished workflow blocked | S4 | S6, S7 | 2 h |
| S8 writes and confirm (later) | Proposal store, Block Kit confirm, GitHub review post (variant B), Jira comment | new `modules/proposals/`, write capabilities, App permission change | **yes (35 reserved)** | `ActionProposal` | double-click, expiry, wrong confirmer, injection corpus | S5b, S6, owner D4 | none | 3 h x 2 |
| S9 hardening | Rate-limit tuning, optional token rotation, Grid, runbook, alerts | docs, metrics, `infra` | no | none | 429 chaos | S4 | S8 | 2 h |

Critical path: S0, S1, S2, S3 (policy engine) then S4, S5/S6. S7 must land **before any second org is invited**.
Parallel: after S2 merges, S3 (policy engine first), S6 and S7 can run in separate worktrees; shared hot files (`later_migrations.py`, `row_security.py`,
`ticks.py`, `worker_service.py` guard) are append-only or tiny, and migration numbers 32 to 35 are reserved up front. Total v1 (S0 to S7): 8 slices, about 24 to
28 h of agent time (ASSUMPTION; S3 and S4 grew about 4 h for the access model), plus 2 h for S6b.

Rollout: dark (`WEAVES_SLACK_ENABLED=false`), then the owner's dev workspace and live smoke, then per installation.

## 7. Decisions needed from the owner

1. **What "power itself from Slack" means.** Options: (a) core actions (ask, create task, review PR, status) without opening the web app, setup
   staying in the web app; (b) full parity incl. connectors/agents/settings from Slack; (c) Slack as login; (d) Slack-native product; (e) proactive agents.
   Recommend **(a)**: covers the brief with the least surface; (c) and (e) are separate products.
2. **Access model (owner-revised): two tiers** (3.5). Ask is open to every Slack user over shareable data; Execute is off until an admin allowlists.
   Recommended and needing your yes/no on each sub-decision:
   - 2a. **Which actions are Execute.** Recommend: `create_task`, `pr_review`, `run_workflow`, PR generation and any write; Ask stays side-effect-free
     (it still creates a hidden 30-day Q&A task because the worker needs one; say if that counts as "persisting work" for you).
   - 2b. **Rule per action.** Recommend selectable per action with default `off`, and suggested setting **user AND channel** for `pr_review`/`run_workflow`
     (blast radius: spends AI and touches repos) and **user OR channel** for `create_task`.
   - 2c. **Unlinked users.** Recommend they may Ask but **never Execute**; an admin maps a Slack member ID to a member (no self-link, no invitations).
     Alternative: allow Execute for Slack-id-only entries under the service principal (weaker attribution and RBAC).
   - 2d. **Ask on by default after install**, model-only, with the per-user limits (5/10 min) and a 1-slot cap, or off until an admin turns it on?
     Recommend on, because "basic accessibility for every user" was the requirement and the notice makes the cost visible.
   - 2e. **Guests and Slack Connect users in Ask.** Undetectable without `users:read` (re-install). Recommend accept in v1, bounded by channel invitation.
3. **PR review variant.** A Slack-only, B post on GitHub (Pull requests: write, installer re-approval, confirm button), both. Recommend **A in v1**; B
   after a proposal/confirm backend exists, because B is a write.
4. **Writes in v1?** None, GitHub review comment only, or also Jira comment. Recommend **none** (no approval backend, 1.3; injection risk, 4.1).
5. **Agent selection.** One default agent per workspace or per-channel mapping. Recommend **one default per action now**; per-channel is a later settings field.
6. **Which connectors are open to everyone in Slack.** Recommend a per-connector checkbox "Available to everyone in Slack", default none (model-only),
   the Slack-search connector labelled "not recommended"; unticked connectors only via `ask_private`, ephemeral. A blocked user is told "ask a Weaves
   admin" (no request flow). Alternative: no private connector use in Slack at all.
7. **Managed AI dependency.** BYOK only (today), wait, or a minimal managed key. Recommend **BYOK only in v1**; clear refusal without a provider; reserved
   entitlement hook (3.8).
8. **Marketplace timing.** Never, after partners hit Slack's limits, or at public launch. Recommend **decide at the first external install**; ship as a
   distributed non-listed app and prepare policy pages meanwhile; listing needs 10 active workspaces (5.3).
9. **Smaller calls (recommend all as stated).** Enterprise Grid: refuse org-wide installs in v1. Token rotation: off in v1 (irreversible once on).
   Slack-origin message retention: keep as task data, no archive, subject to legal review of Slack's API terms (4.2).

## 8. Risks and what not to build now

Risks

1. **Leakage through shared connector tokens** (4.1). Mitigation: open Ask holds only ticked connectors (default none), `ask_private` is ephemeral.
   Residual: detecting "needs a non-shared source" works only on explicit mentions, so some questions get a model-only answer (UNVERIFIED how often).
2. **Slack rate limits** on thread context (1/min, 15 messages); sources conflict on existing installs. Mitigation: mention-only context, serialization.
3. **Slack API terms** (storage, LLM use) read from secondary sources. Mitigation: legal review first, no archive, no bulk reads.
4. **Ack under load**: a slow DB causes retries and a disabled subscription. Mitigation: one signature check plus one insert; slow-DB fake test.
5. **Agent runtime is not an agent loop**: one query string, five documents, no structured input; PR review and connector answers stay shallow. Do not oversell.
6. **No write path** and no approval backend: every write feature needs a proposal/confirm service first.
7. **Single VM capacity** (4 slots, per-org running cap 2): busy channels queue; status must say "queued"; open Ask is capped at 1 running slot.
8. **Stale mappings and operations**: membership is re-derived on every event; secret rotation and token loss need runbook steps (S9).

Not building now: any access-request or invitation flow; self-link; Execute for unmapped Slack ids; Socket Mode; Grid org-wide install; DMs and `message.*` follow-ups; slash commands and App Home; any external write or confirmation
flow; automatic PR reviews from webhooks; Slack as login; a channel-history index; email-based user matching; token rotation; Marketplace submission;
managed-AI entitlements; a general agent tool loop (a separate product decision that would improve every flow here).

## Sources (read 2026-11-26 unless noted)

Slack (docs.slack.dev): authentication/verifying-requests-from-slack, apis/events-api, reference/events/{app_mention,tokens_revoked}, authentication/{installing-with-oauth,
using-token-rotation}, apis/events-api/using-socket-mode, reference/methods/{chat.postMessage,chat.postEphemeral}, interactivity/{handling-user-interaction,
implementing-slash-commands}, changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps, slack-marketplace/slack-marketplace-app-guidelines-and-requirements.
Search results only: app_uninstalled, team_access_revoked, Slack API Terms (slack.com/terms-of-service/api), Hunton and Cloud Wars write-ups. GitHub (docs.github.com):
rest/authentication/permissions-required-for-github-apps, rest/pulls/{reviews,pulls}, webhooks/webhook-events-and-payloads (`pull_request` actions not in the fetched part,
UNVERIFIED). Not verified anywhere: message-shortcut scope, Grid per-workspace events, which event additions force a re-install.
