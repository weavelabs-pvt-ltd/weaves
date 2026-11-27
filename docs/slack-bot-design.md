# Slack bot design: tag, create tasks, PR reviews, answers from connectors

Status: design for owner review, 2026-11-26. Docs only; no real Slack/GitHub calls were made. Code refs are to `origin/main` at `b50f4c3`.
External facts cite their source and read date; "UNVERIFIED" marks what I could not confirm from a primary page.

Requirement (owner, 2026-11-26): Weaves is a bot in a client's Slack workspace that answers when tagged (in thread), creates tasks, triggers GitHub PR
reviews, answers from org connectors (GitHub, Slack search, Jira, Notion, Datadog, custom MCP) and similar flows.

## 0. Summary

- The platform already has tasks/worker, limits, usage ledger, request ids, kill switch, purge, secrets and RLS; missing is the Slack edge (installation, signed endpoint, inbox, access policy, reply path).
- Access is two-tier (owner, 2026-11-26): **Ask** (pure Q&A, open to every Slack user, but only over data the admin marked shareable) and
  **Execute** (create task, PR review, run workflow, anything that persists work, spends meaningful AI or writes; off until an admin allowlists
  channels/users per action in a dashboard "Slack access" page). Every request runs one pipeline: **discover tools, plan, check access per tool, respond only if all needed checks are green** (3.5.1). Connector tokens are org-wide, so access is Weaves-level entitlement, not source-system identity (3.5.2).
- No tool loop and no write path (approvals backend removed in migration 31): v1 is read-only answers, task creation, Slack-only PR review.
- Non-Marketplace apps get 1 request/min and 15 messages on `conversations.replies`. Recommended v1: slices S0 to S7 incl. S4a/b/c (section 6, about 36 h, ASSUMPTION). All 14 decisions are ACCEPTED (section 7); the UX contract is the designer spec (section 2).

## 1. Current state (evidence)

### 1.1 Slack today is a read-only, user-token connector

| Fact | Evidence |
|---|---|
| OAuth start requests only **user** scopes `search:read,channels:read,groups:read,channels:history,groups:history`; no `scope=` (bot) parameter | `modules/integrations/http/oauth_start.py:78-84` |
| Callback reads only `payload["authed_user"]["access_token"]`; the bot token and `bot_user_id` Slack returns are ignored and not stored | `modules/integrations/http/oauth_slack.py:50-57`; `team_id`/`team_name` are only copied into a setup record, `:78-79` |
| Plugin supports one capability, `slack.messages.search`, via `search.messages` (+ optional `conversations.replies`, limit 10), restricted to configured channel names and quoting the query so operators cannot widen scope | `clients/integrations/slack.py:48-69, 85-112, 130-150`; seeded at `modules/integrations/bootstrap.py:212-240` |
| Channels discovered with the user token (`conversations.list`); setup docs list only user scopes and a Redirect URL; no signing-secret variable exists | `modules/integrations/http/oauth_resources.py:165-190`; `infra/gcp/singlevm/CONNECTORS.md` sections 1-2 |

The Slack app "Weaves" is registered (per owner) with bot scopes `app_mentions:read, chat:write, channels:history, groups:history`, the user scopes above, Event Subscriptions OFF; nothing in the repo reads its signing secret yet.

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

No bot-token storage or `team_id -> org` mapping; no signed endpoint with a global event inbox; no Slack-user mapping (tasks need a principal, `tasks/service.py:127`); no reply
path or Slack origin on tasks; no per-request tool selection or per-asker access check (the runtime runs every allowed capability once); no GitHub PR files/diff capability,
no write approval path; model access is BYOK per org (managed AI is a future dependency).

## 2. User flows and UX contract

The owner's designer spec ("Weaves in Slack", scenes A to I, mobile, system note) is the **UX contract**; copy below is quoted from it as summarized by the coordinator (the HTML file
renders by JavaScript, so only scene titles were machine-readable). Adjustments approved by the owner on 2026-11-26: (a) **standard Slack emoji only** in v1 (apps cannot install custom
emoji into a customer workspace; an optional custom pack is a later extra); (b) native status is UNVERIFIED for channel-thread mentions, see 2.3; (c) stop/retry/missing-detail replies
need a **re-mention** (no `message.*` subscription, so non-mention thread replies are never read); (d) no "View details" link on plain Ask answers (hidden Q&A tasks have no page; an
admin-only "Slack activity" view with counts and decisions, no text, is the later alternative); (e) no welcome-on-join in v1 (needs `member_joined_channel`).

Common rules: the request is acked within 3 s after the inbox insert, all work is async; the bot is **silent until tagged** and answers in the triggering thread; every request is
classified **Ask** (Tier 1) or **Execute** (Tier 2) by the dispatcher, not the model, and passes the pipeline of 3.5.1; nothing a Slack message says may widen permissions; blocked users
see the plain messages in 3.5 ("ask a Weaves admin"), with no invitation or access-request flow. **Bot profile:** name Weaves, avatar = woven mark on cobalt, status line "Tag me in a thread".

### 2.1 Scenes

| Scene | Trigger and gates | What the asker sees |
|---|---|---|
| **A Ask** (Tier 1) | `@Weaves <question>`, anyone | `:eyes:` reaction within about a second (the endpoint adds it after acking). Working state: Slack's native "is thinking" status with **real step labels** ("Checking sources", "Reading Jira", "Writing") from the pipeline stage, **not a message**; fallback: one status message edited in place. The answer is a **new message** (so the asker is notified): answer text, a "From Jira, Notion" evidence line, "3 sources · 41s", no buttons. Feedback is the asker's `:+1:`/`:-1:` reaction (2.4). Model-only answers say "From general knowledge" |
| **B Private refusal** | Ask needs a tool the asker may not use (3.5.2) | Ephemeral, `:lock:`: "Jira isn't open to everyone in Slack. Ask a Weaves admin." No data, no names |
| **C Task** (Execute `create_task`) | `@Weaves task: <text>`; rule passes | First message "Running" is **edited** to a "Done" receipt; the result is posted as a **new message**. A repeat `task:` in the same thread replies "Already tracked in this thread: <title>" with a **View task** button (lookup by `slack_threads` unique key) |
| **D PR review** (Execute `pr_review`) | `@Weaves review <PR url>`; pre-flight passes | Message 1 names the PR and file count ("Reviewing acme/api#42 · 12 files") and is edited into a receipt. Message 2 is the review: summary, risk as `:large_green_circle:`/`:large_yellow_circle:`/`:red_circle:` plus a word, findings, one question, footer "Reviewed N of M files · Nothing was posted on GitHub · Open in Weaves". Variants: partial review (names what was skipped), unreadable PR ("I can't read that PR") |
| **E Not allowed** | Execute rule fails | Ephemeral, `:lock:`: "I'm not set up to create tasks here. Ask a Weaves admin." |
| **F Missing detail** | e.g. `review` without a link | Asks in words with an example: "Reply with @Weaves and the PR link, for example @Weaves review https://github.com/acme/api/pull/42" (re-mention required) |
| **G Waiting, failing, stopping** | slow or failed work | Queued (after 10 s, ASSUMPTION) then "Still working (2 min)…", then at 5 min "This is taking longer than expected. I'll keep going and post here." with **Stop**. Failure: "`:warning:` I couldn't finish this (ref a1b2c3)" with **Try again**. Stop: message becomes "`:octagonal_sign:` Stopped." Details in 3.12 |
| **H Small exits** | empty mention, greeting, `status` | Empty mention: help with three examples. Greeting: a greeting. `status`: private (ephemeral) state and elapsed time. None creates a task or calls a model (fixed grammar) |
| **I System** | maintenance, limit, no model | "Weaves is unavailable right now. Try again later." / "You've reached the limit; try again at 14:20." / "No AI model is connected. Ask a Weaves admin." Optional welcome message: **out of v1** |

Mobile and dark variants use the same blocks (no layout width assumptions; text under 3,000 characters per section block). Ephemeral posts in a not-yet-active thread may not show
(Slack `chat.postEphemeral` docs); then the bot retries once at channel level, UNVERIFIED which is visible, and the dev-workspace test in 2.3 covers it.

### 2.2 Flows behind the scenes (v1)

- **F1 Ask**: endpoint verifies and stores `app_mention`; the dispatcher resolves team -> org, classifies, runs the cheap gates (3.5.1 steps 1-5) and creates a hidden Q&A task as the Slack
  service principal (idempotency key `slack:{team}:{event_id}`); at worker pickup the pipeline discovers, plans, checks, then the normal run fetches and answers; `slack_reply` posts. Answers
  use only connectors that are healthy, needed and admitted for the asker; model-only answers need none. Timeouts: `WEAVES_SLACK_REPLY_DEADLINE` 15 min (ASSUMPTION), then a failure message.
- **F2 Task**: `task:` grammar (shortcut and slash command later, S5b; developer slash commands cannot be used in threads). Created as the mapped member; ambiguous intent falls back to Ask,
  never to an unannounced write elsewhere.
- **F3 PR review**: strict PR-URL parse; pre-flight (3.5.1): rule passes, repo covered by the GitHub installation and on the admin's repo allowlist. **Variant A, Slack-only (v1):** new capability
  `github.pr.review_context` reads `GET /pulls/{n}` and `/pulls/{n}/files` (diff byte-capped; GitHub caps the files list at 3,000; which patches are omitted is UNVERIFIED, so tolerate missing
  patches); capabilities get only `{"query": task}` (`runtime_run_capabilities.py:133`), so it parses the URL from that text. **Variant B, post on GitHub (later):** needs Pull requests: write for
  `POST /pulls/{n}/reviews` (GitHub permissions page), installer re-approval (5.2), `COMMENT` only, an F5 confirmation, and optional `pull_request` webhooks (signature, `X-GitHub-Delivery` dedupe, per-repo opt-in).
- **F4 Status** (ephemeral, from `task_turns`/`tool_invocations` summaries, no content).
- **F5 Writes (later)**: no external write from free text. A write is a **proposal** message with exact parameters, Confirm/Cancel buttons, 30-minute expiry, confirmers limited to the requester plus
  admins, single-use, on the **same interactions endpoint** as Stop/Retry (3.12). Needs a proposal backend that does not exist (1.3).

### 2.3 Native status test (UNVERIFIED, must pass before relying on it)

Slack documents `assistant.threads.setStatus` for AI-assistant threads, auto-cleared when the app replies, with a two-minute timeout; it needs `chat:write` (`assistant:write` "for the time being"), lists
`method_not_supported_for_channel_type`, and points to a replacement `agents.sessions.setStatus`; Agents & Assistants features may need a paid plan (Slack docs, read 2026-11-26). Nothing there says it works
for a plain `@mention` in a channel thread. **Test (owner, dev workspace, after S4):** enable Agents & Assistants, mention the bot in a public and a private channel thread, call `setStatus` with
`channel_id`+`thread_ts`, record the response and whether the status appears and clears on reply. Pass: `ok:true` and visible in both. Result sets `WEAVES_SLACK_NATIVE_STATUS=on|off`. **Default path is the
edited-message fallback**: one status message ("Reading Jira…") edited on stage change (`chat.update`, rate tier 3), then deleted (`chat.delete`) when the answer posts, or edited into the failure text.

### 2.4 Cut line

| In v1 | Later |
|---|---|
| Ask for every user via the pipeline; scenes A, B, F, G, H, I; Stop/Retry buttons; reaction feedback | F5 writes; Jira comment; PR generation; custom emoji pack; welcome on join |
| Execute `create_task` and `pr_review` (Slack-only), off until configured; scenes C, D, E; Slack access page, allowlists, audit | `run_workflow` (v1.1, S6b); shortcut and slash command; PR review posted on GitHub; admin "Slack activity" view |
| Status (ephemeral); install, uninstall, kill switch, purge, limits | DMs, `message.*` follow-ups, self-link, Grid, Marketplace, managed AI, per-person source identity |

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
| `POST /api/v0/slack/interactions` | Stop/Try again buttons (form-encoded `payload=` JSON); later confirmations and shortcuts | same signature and replay rules; 3.12 (S4b) |
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
| `slack_connector_health` (product_records, org) | health snapshot per installation | `installation_id, status, reason_code, checked_at` (written by a `slack_health` tick every 10 min, ASSUMPTION, using `healthcheck_plugin_installation`) |
| `slack_threads` (product_records, org) | Slack thread to task | `team_id, channel_id, thread_ts, kind, task_id, requester_slack_user_id, requester_principal_id, tier, run_state` (`queued|running|cancel_requested|done|failed|stopped`), `stage`, `attempt`, `status_ts`, `answer_ts`, `reply_state`; unique `(team_id, channel_id, thread_ts, kind)` |
| `slack_feedback` (product_records, org) | reaction feedback (3.13) | `answer_ref, turn_id, agent_run_id, request_id, asker_ref, value, source, at`; unique `(turn_id, asker_ref)` |
| `slack_access_settings` (product_records, org, one row) | the dashboard page (3.5) | `version`, `ask_enabled`, `connector_audience{installation_id: everyone|allowed_people|not_in_slack}` (default `not_in_slack`), `partial_answers` (default false), `planner_profile_id`, `ask_limits{user,channel,org_day}`, `rules{create_task, pr_review, run_workflow, ask_private}` each `off|user_only|channel_only|user_and_channel|user_or_channel` (default `off`), `workflow_ids[]`, `pr_review_repos[]`, `qa_principal_id`, `qa_agent_version_id`, `paused`, `updated_by`, `updated_at` |
| `slack_allowed_users` (product_records, org) | user allowlist | `slack_user_id` (unique per org), `principal_id` (null = listed by Slack id only), `note`, `added_by`, `added_at` |
| `slack_allowed_channels` (product_records, org) | channel allowlist | `channel_id` (unique per org), `name_cached`, `added_by`, `added_at` |

Global rows need no tenant context, so they follow the waitlist/oauth-state pattern: system purpose `slack` in `row_security.py` sees only
`slack_installations` and `slack_event_inbox`; org-scoped collections get `ownership.json` entries (a test fails otherwise) and a purge/export decision in
`modules/auth/organization_retention.py`. Migration 33 adds the partial unique indexes for the allowlists, `slack_threads` and `slack_feedback` and an idempotent backfill adding permission
`slack.manage` to stored Owner/Admin roles (as 31 touched roles, `approvals_removal.py:1-27`; confirm the seed path in S3). Audit reuses `audit_events`.

### 3.4 Installation and workspace to org binding

- Install starts in Weaves (Settings, "Add to Slack"), so the **org is chosen by a signed-in Owner/Admin**, never inferred from Slack. Authorize URL:
  `scope=` the v1 bot scopes of 5.1; `state` row binds org+principal (pattern `oauth_start.py:77-83`). The install
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
`tasks.create` on one Q&A agent. Tasks are `audience=PRIVATE` to that principal with `origin.kind="slack_qa"`, hidden from the task list, deleted by a
retention tick after 30 days (ASSUMPTION). The Q&A agent version lists all of the org's read capabilities, but **what a request may touch is narrowed per
request** by the pipeline below, not by agent version.
- Each connector has an **audience** setting: `everyone` (any Slack user), `allowed_people` (users passing the `ask_private` rule, answered ephemerally) or
  `not_in_slack` (default for every connector). The dashboard shows each connector's scope (e.g. Slack channel list, GitHub repositories) next to it, and
  the Slack-search connector, which acts as its installer, is labelled "not recommended".
- Ask limits (it spends the org's AI key): per Slack user 5 questions/10 min, per channel 20/10 min, per org 200/day, 1,000 characters per question
  (ASSUMPTION, editable); open Ask may hold at most 1 of the org's 2 running slots (`WEAVES_LIMIT_MAX_RUNNING_PER_ORG`) so Execute is not starved.

**Tier 2 Execute, off until configured.** For each action type the admin picks a rule: `off` (default), `user_only`, `channel_only`, `user_and_channel`,
`user_or_channel`. "User" = Slack user in `slack_allowed_users`; "channel" = channel in `slack_allowed_channels` (DMs never satisfy a channel rule).
Independent hard checks always apply on top: the user must map to an **active Weaves member** (`principal_id`) whose role has the Weaves permission (`tasks.create`
+ `agents.run`, or `workflows.run`); unlinked Slack ids may never execute (decision 2c); `pr_review` needs the repo in the GitHub installation
**and** in `pr_review_repos`; `run_workflow` needs the workflow published and in `workflow_ids`. `ask_private` (trusted Ask) uses the same rule engine and
may allow users listed by Slack id only, since it is read-only.

#### 3.5.1 Request-time pipeline: discover, plan, check, respond (both tiers)

| Step | Where | What |
|---|---|---|
| 1-5 cheap gates | dispatcher, again at pickup | platform/org state and `read_only`, installation, settings `paused`, rate limits (3.9), then classification and the Execute rule and pre-flight (below) |
| 6 DISCOVER | pickup | list the org's read capabilities with a status: installation `ACTIVE` (`PluginInstallationStatus`, `contracts/v1/plugins.py:54`), token present, `slack_connector_health` fresher than 15 min (ASSUMPTION), not quarantined/disabled. Stale or `ERROR` entries are re-probed live (`healthcheck_plugin_installation`, `runtime_installation_health.py:39`, 3 s timeout) only if the planner needs them; otherwise red with reason `unavailable` |
| 7 PLAN | pickup | Execute: **fixed grammar, no model**: `pr_review` needs `github.pr.review_context`; `run_workflow` needs that workflow's capabilities. Ask: one **planner call** (below) |
| 8 CHECK | pickup | per needed tool: health green AND access green for this asker (3.5.2); result recorded per tool |
| 9 RESPOND | worker run | strict (default): run only if every needed tool is green; else no data and a short message. `partial_answers=true`: answer from green tools and say which were skipped. Model-only answers stay allowed when the plan needs no tool |

**Minimal planner (Ask only).** The runtime has no tool loop (`runtime_run_capabilities.py:42-98` runs each capability once with the task text), so planning is one
extra, tool-less model call by the org's own provider key: input = the question (marked untrusted, 1,000 characters) plus a list of discovered tools (id, label, one-line
description; no data); output = JSON `{"needs": [≤3 presented ids], "general_ok": bool}`, validated in code (no provider structured-output feature found in
`clients/model_providers`, so JSON-in-prompt plus strict parsing, UNVERIFIED that none exists). It runs on `planner_profile_id` (default: the agent's profile; a cheaper
profile is recommended, decision 12). Cost: about 400-800 input and 60 output tokens per question, roughly +20-30% tokens and +1-3 s (ASSUMPTION), metered in the usage ledger
under key `{agent_run_id}:plan` (the ledger today assumes one call per run, `docs/DATA_MODEL.md` usage section; S4a must extend it and re-test idempotency). It is skipped
when discovery finds no usable tool. Regex hints (Jira key, PR URL, repo) are unioned into `needs`. **Failure:** timeout, invalid JSON or an unpresented id fails closed:
if any tool was discovered the reply is "I couldn't work out which sources to check. Try again." with no data and no general answer; with no tools, model-only continues.
**Why better than detection by explicit mention:** "what is the status of the checkout outage?" names no source yet needs Jira and Datadog; the planner finds it, so a red
tool is reported instead of a confident model-only guess, and the footer lists consulted sources. **It is not a security boundary:** it only picks among presented
ids; authorization is deterministic code at step 8; the answering run receives `origin.capability_ids` = needed AND green, and the capability loop fails closed
(runs none) for Slack turns without that field (edit at `runtime_run_capabilities.py:42`).

#### 3.5.2 What "access" means: (a) Weaves entitlement, recommended for v1; (b) identity, later

- **(a) Weaves-level entitlement** (v1): the asker may use connector X if its audience admits them (`everyone`, or `allowed_people` and the user passes the rule), and
  for Execute the allowlist rule, the mapped active member and that member's role permission pass. It **guarantees** that only people the admin entitled can
  cause reads through that connector, in the channels and shapes the admin allowed, with a record of every decision. It **cannot guarantee** that the asker could open
  that Jira project, private channel or Datadog dashboard themselves: connectors use an org-wide or installer token, so its reach is the ceiling and Weaves cannot see
  the asker's rights in the source system. Mitigation: admins scope each connector to what its audience may see, and the page shows that scope beside the setting.
- **(b) Identity-level** (later, stronger): each person connects their own account and the bot calls with the asker's token, so "green" means connected, valid and authorized in
  the source system. Cost (ASSUMPTION 6-8 slices): per-(principal, installation) token store (secret purpose `user_connector_token`), a per-user connect page (connector OAuth is
  already bound to the signed-in principal, #65), worker refresh, `plugin_gateway.invoke` taking a token override, per-user health, purge/tombstone coverage, GitHub user-to-server
  tokens, unlinked users limited to model-only. The Slack-user-to-member mapping (3.5) is its prerequisite.

**Red replies** (ephemeral where possible; no data, no list members, no request flow): "I can't check Jira right now (it's unavailable); nothing was looked up." / "Jira isn't
open to everyone in Slack. Ask a Weaves admin." Partial mode ends "Skipped: Jira (unavailable)". Quarantine shows only "temporarily unavailable".

**One decision record per request** (`audit_events`, action `slack.decision`, both tiers, no text or data): tier, action, tools considered, tools needed, per-tool health and
access result with reason codes, planner (used, profile, valid/invalid/skipped, tokens), mode strict/partial, outcome (`answered`, `answered_partial`, `model_only`,
`refused_access`, `refused_unavailable`, `planner_failed`, `blocked_rule`), `settings_version`, event and request ids. Repeats of the same blocked outcome per user merge with a counter.

**Execute pre-flight** (fixed grammar, before any model call, repeated at pickup): the action rule passes; `pr_review`: the PR URL parses, its repo is covered by the GitHub installation
(not stale) **and** in `pr_review_repos`; `run_workflow`: the workflow is published and in `workflow_ids`; the mapped member holds the Weaves permission. Any red path makes no connector call.

**Mapping** is admin-asserted in the dashboard (pick a member, paste their Slack member ID); audited. No self-link, invitation flow or email matching in v1.

**Validation at request time and at pickup.** The dispatcher runs steps 1-5 when it claims the inbox row. Settings, tokens and health can change before the worker
runs, so the authoritative pass is a pre-run guard in `modules/tasks/worker_service.py:_process_claimed_turn` (`:111`) for turns whose `origin.kind` is `slack` or `slack_qa`: it
re-runs steps 1-5, then 6-8, writes the decision record and sets `origin.capability_ids`. Blocked means the turn is cancelled (`slack_access_revoked`) or finishes with a fixed
short message without calling the answer model. Saving settings also cancels still-queued Slack turns that no longer pass (bounded batch). Running turns finish.

**What the user sees** (ephemeral to the asker; if Slack drops it in an inactive thread, one generic threaded line without internals):

| Cause | Message |
|---|---|
| Execute rule `off` | "Running things from Slack isn't turned on for this workspace. Ask a Weaves admin." |
| User not listed | "You're not on the list for this from Slack. Ask a Weaves admin." |
| Channel not listed | "This isn't enabled in this channel. Ask a Weaves admin." |
| Repo / workflow not enabled | "That repository (workflow) isn't enabled for Slack." |
| No Weaves member for that user | "Your Slack account isn't connected to a Weaves member. Ask a Weaves admin." |
| Ask rate limit | "You're asking a lot; try again in N minutes." |

Messages never name who is on a list. **Audit:** the per-request decision record above replaces separate allowed/blocked rows; Execute attempts also write
`slack.execute.allowed|blocked`. Ask additionally counts metrics (`slack_ask_total{outcome}`).

**Dashboard "Slack access" page** (new frontend page; edit permission `slack.manage`, Owner/Admin by default; members may view nothing):
1. Status: workspace, bot state, Pause bot (`paused`), Reconnect/Disconnect.
2. "Ask: open to everyone in Slack": toggle (default on after install), a connectors table (scope summary, health, Audience dropdown, default "Not in Slack"),
   the three limits, "Partial answers" toggle (default off: all sources green or no data) and planner model. Notice: "Anyone in a channel where @Weaves is added can ask questions. Answers use your AI key and only the sources you tick here."
3. "Run things from Slack": a rule dropdown per action (Create task, Review PR, Run workflow, Generate PR shown disabled "coming later", Private data in
   answers), default "Off".
4. Allowed people (member, Slack member ID, note; hint "Only these people can run the actions above") and allowed channels (pick from the channel list via `channels:read`/`groups:read`, or paste
   an ID).
5. Allowed repositories (repos the GitHub installation covers, stale entries flagged) and workflows ("Triggerable from Slack", published only).
6. Recent Slack activity: last 50 allowed/blocked Execute attempts, no content.


### 3.6 Tasks and attribution

Execute tasks are created through `TaskService.create_task` as the **mapped member's principal** (RBAC, `TASKS_CREATE`, `AGENTS_RUN` and audit use
existing paths); Ask tasks run as the Slack service principal (3.5). Both carry a new optional `origin` object (JSON, no migration):
`{"kind":"slack"|"slack_qa","team_id","channel_id","thread_ts","slack_user_id","event_id","settings_version"}`. Audit events carry actor and origin. Follow-ups
in a thread append a message (`add_message`, `tasks/service.py:359`) re-checked against the same tier rules.

### 3.7 Replies

- `chat.postMessage` with `thread_ts` = the parent message `ts`; keep text under 4,000 characters (truncated beyond 40,000; Slack docs). Model mrkdwn is
  sanitized: `<!channel>`, `<!here>`, `<@U..>` and unlisted links neutralized, `parse=none`, `unfurl_links/media=false`.
- About 1 message/second per channel; at most 2 new posts and 6 edits per request. Citations list connector document titles/ids only, never invented links.
- `slack_threads.reply_state`: `pending -> posted | undeliverable`; the returned `ts` is stored and checked before re-posting (no Slack idempotency key; a rare duplicate is accepted).
- Thread context: `conversations.replies` is, for commercially distributed apps outside the Marketplace, rate tier 1 (1 request/min) and 15 messages per
  request, for new installs since 2025-05-29 and existing installs from 2026-03-03 (changelog 2025-05-29 says existing installs are unaffected while a search
  summary says 2026-03-03: **conflicting, UNVERIFIED**; plan for the stricter reading). Internal customer-built apps keep 50+/min. Design: mention text only
  by default; at most one thread fetch per request, cached 5 minutes, serialized per workspace, skipped with a note when limited.

### 3.8 Cost control

Every Slack-created task (Ask and Execute) passes `admit_task` and, in the worker, `admit_tool_call` and usage reservation, as web tasks do. Separate Ask and
Execute buckets per Slack user, channel and org (Ask defaults in 3.5; Execute 10/10 min per user, 30/10 min per channel, ASSUMPTION); refusals are ephemeral
with retry-after. `usage_daily` already rolls up by org/agent; `origin.kind` allows later reports. BYOK today: the customer's key pays. With managed AI, the
bot must not become a free proxy: an entitlement check before dispatch (3.14).

### 3.9 Kill switch, quarantine, suspension

The dispatcher re-reads `platform_controls.admissions_paused`, org lifecycle state, `org_quarantines`, installation `status` and settings `paused` at every
stage (order: platform/org and `read_only`, installation, `paused`, rate limits, classification and tier rule, then DISCOVER, PLAN, CHECK, RESPOND of 3.5.1). Quarantined or suspended org: inbox rows `dropped(reason)`, at most one neutral
"Weaves is temporarily unavailable" per channel per hour. `read_only` platform flag: Ask allowed, Execute refused. Quarantine gains a step "installation
suspended" (S7), reversed by `release-quarantine` through the recorded `changes` (6.4 pattern). `WEAVES_SLACK_ENABLED=false` acks and drops everything. Stop buttons keep working while paused or quarantined (3.12).

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

### 3.12 Interactions: Stop and Try again buttons

**Endpoint** `POST /api/v0/slack/interactions` (Interactivity ON, S4b): same signature and 5-minute replay rules as events, form body `payload=<json>`, 200 within 3 s, then work async. Only `block_actions`
are handled in v1 (URL buttons such as "View task" and "Open in Weaves" also post a payload and are acked and ignored). **Dedupe:** the press is inserted into `slack_event_inbox` as
`event_type=interaction`, `event_id=ia:{team}:{user}:{action_id}:{message_ts}:{action_ts}` (`action_ts` presence is UNVERIFIED; fall back to a 2-second bucket), so it needs no new table; effects are also
idempotent through compare-and-set on `slack_threads.run_state`. **Buttons:** `action_id=weaves.stop|weaves.retry`, `value=<run_ref>` (opaque `slack_threads` id; no text). The team, channel and message ts in
the payload must match the stored run, else "That's no longer available."

| Rule | Behaviour |
|---|---|
| Who may press | The requester, or a Slack user mapped to a member holding `slack.manage`. Others get an ephemeral "Only the person who asked, or an admin, can stop this." (or "...retry this.") |
| Audit | Every press writes `slack.interaction` (action, allowed or blocked, reason code, Slack user, principal, run ref, request id); no text |
| Stop | Sets `run_state=cancel_requested` (compare-and-set), cancels the Weaves turn through the existing cancellation path (`_cancellation_requested`, `worker_service.py:205`; polled at stage boundaries by `cancel_if_requested`, `runtime_run_capabilities.py:43`); running read-only fetches are dropped and any late answer is discarded; tokens already spent still settle in the ledger; the task is marked cancelled; the message is edited to "`:octagonal_sign:` Stopped." with no buttons. Double press: ephemeral "Already stopped." |
| Where | Working message for tasks and PR reviews; the "taking longer than expected" message for Ask (native status cannot carry buttons). Text fallback `@Weaves stop`: the requester's newest active run in that thread |
| Try again | Only on failure messages. Re-runs the stored request (the hidden task's first message) as a new synthetic event with idempotency key `slack:{team}:retry:{run_ref}:{n}` through **all gates again** (steps 1-5, limits, health, current settings); at most 3 retries per request (ASSUMPTION); the failure message is edited to "Retrying…". Text fallback `@Weaves retry` |
| Expiry | Buttons disappear when the run reaches a terminal state; Try again expires after 24 h (ASSUMPTION); stale presses get "That's no longer available." |
| Paused, quarantined, uninstalled | Stop is always honoured while the token works (stopping is safe); Try again is refused by the gates with the generic unavailable message; after uninstall the team cannot be resolved, the press is acked and dropped, and the uninstall handler cancels running turns |

Responses to a press use `response_url` (exact host `hooks.slack.com`, 5 uses within 30 minutes) or `chat.update`. The same endpoint, dedupe and presser check carry F5 confirmations (`weaves.confirm:{proposal_id}`).

### 3.13 Reaction feedback

Subscribed event `reaction_added` (bot scope `reactions:read`; private-channel delivery is UNVERIFIED, the docs name no extra scope). A reaction counts only if the item is one of our answer or result messages
(`slack_threads.answer_ts`), the reactor is the asker, and the name normalizes to `+1` or `-1` (skin-tone suffixes stripped); the bot never pre-seeds reactions on its answers. The latest reaction by event time wins; removals
are not read (no `reaction_removed` subscription; adding it is a one-line option). Stored in org-scoped `slack_feedback` (product_records, unique `(turn_id, asker_ref)`, index in migration 33): `answer_ref`, `turn_id`,
`task_id`, `agent_run_id`, `request_id`, `asker_ref` (mapped `principal_id`, else HMAC-SHA256 of the Slack user id with a per-org pepper, never the raw id), `value` (+1/-1), `source="slack"`, `at`. **No message text and no
reason** (Slack reactions carry none; a reason can come from the web app later). It fills Analytics metric #8 "answer feedback" (`product/Analytics dashboard.md`: thumbs up/down linked to the run, source "none, not built") and
supports #10 (feedback-to-evidence via `request_id`); the web thumbs can write the same collection with `source="web"`. Reporting uses the cross-org operator read path that note's gap list already needs
(`usage_report.py` pattern, named system purpose); `slack_feedback` is added to that allow-list. Metric `slack_feedback_total{value}` (no org label). Retention: 400 days (`WEAVES_USAGE_RETENTION_DAYS`), deleted with the org purge; removed with the uninstall purge too.

### 3.14 Managed AI later (not built here)

The owner decided that low-tech-capacity customers get **Weaves-managed AI only**. For this bot that changes: model calls (the answer and the planner) use platform-held provider keys, so Weaves pays (BYOK today:
`docs/phase-5-design.md` 5.3 "keep BYOK distinct from platform-paid usage"); open Ask becomes a direct cost exposure, so managed orgs need stricter Ask limits and a pre-dispatch entitlement check (3.8); the usage ledger
needs a per-model price table so cost is real (`estimated_cost_nano_usd` is null without prices; also a gap in the Analytics note); the planner must use the cheapest profile; the model egress allow-list gains the platform
provider host; Phase 7 billing/entitlements gate it. None of this is built by S0-S7; the reserved hook is the entitlement check in S4.

## 4. Security and abuse

### 4.1 Authorization and data leakage (the main risk)

- **Who may trigger**: Ask is open; Execute needs the allowlist rule (3.5) plus the mapped member's Weaves role permission, never Slack roles.
- **Connector tokens are org-shared**: a connector installation is workspace-scoped (`PluginInstallation.auth_ref`,
  `contracts/v1/plugins.py:127-131`), and the Slack search connector runs with the **installing user's** user token
  (`oauth_slack.py:50-57`), so any answer that uses it can quote messages from channels the asker cannot see. Defaults:
  1. A connector's data reaches an answer only if the pipeline's per-tool check is green for that asker (3.5.1); default audience is `not_in_slack`, and the run receives
     only `needed AND green` capability ids (fail closed otherwise).
  2. `allowed_people` connectors answer ephemerally to the asker, never in channel. Access is Weaves-level entitlement; its limits are in 3.5.2.
  3. The Slack connector's own channel list (`clients/integrations/slack.py:48-69`) stays authoritative: the bot never searches beyond it.
  4. Admin UI warns: answers in a channel are visible to everyone in it, including guests and Slack Connect members; Slack does not tell us who is a guest without `users:read` (decision 2e).
- **Prompt injection** (Slack text, PR diffs): today the agent has read-only calls with bounded output and no write path, so the worst case is a
  misleading or data-revealing answer, which the tier split bounds (open Ask holds no private connector). System prompt marks Slack text and diffs as
  untrusted; connector results cannot pick tools; mentions and links in replies are neutralized (3.7). **With writes** injection becomes serious: typed
  parameters shown verbatim in the confirmation, per-capability allowlist, no hidden parameters derived from fetched content.
- **Rate limiting**: per Slack user, channel and org (3.8) plus a global ingest bucket; Slack's own cap is 30,000 events per workspace per hour.
- **Replay and forgery**: raw-body HMAC with a 5-minute window, constant-time compare, unique `event_id`; retries are deduped, not special. Interaction
  payloads: same signature check, then run state and presser checks (3.12). The verification token is deprecated and unused.
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

### 5.1 Exact v1 settings (Slack app "Weaves", api.slack.com/apps)

| Setting | Value |
|---|---|
| Display information | Name `Weaves`; avatar woven mark on cobalt; short description "Tag me in a thread"; app home messages tab off |
| Bot scopes | `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `channels:read`, `groups:read`, `reactions:read`, `reactions:write`; plus `assistant:write` **only if** the 2.3 test passes |
| User scopes (read connector, unchanged) | `search:read`, `channels:read`, `groups:read`, `channels:history`, `groups:history` |
| Event Subscriptions | Request URL `https://api.weave-studio.in/api/v0/slack/events`; bot events `app_mention`, `reaction_added`, plus `app_uninstalled` and `tokens_revoked` (S7 needs them: this adds two to the owner's list). **Switch ON only after S2 is deployed**: saving the URL needs the endpoint to answer `url_verification`. No `message.*`, no `member_joined_channel` |
| Interactivity | ON; Request URL `https://api.weave-studio.in/api/v0/slack/interactions` (after S4b is deployed) |
| OAuth redirect URLs | existing `.../oauth/slack/callback` plus the bot install callback `.../oauth/slack/install/callback` |
| Agents & Assistants | enable on the dev workspace to run the 2.3 test; keep only if it passes (UNVERIFIED paid-plan requirement) |
| Slash commands, shortcuts | none in v1 |
| Token rotation | off (decision 9); distribution: non-listed, "Manage distribution" on only when the first external install is planned |

Why each scope: `app_mentions:read` mention events; `chat:write` post, edit, delete, ephemeral; `channels:history`/`groups:history` thread context (`conversations.replies`, 1 request/min for non-Marketplace apps);
`channels:read`/`groups:read` the channel picker on the access page; `reactions:write` the `:eyes:` ack; `reactions:read` feedback. `chat:write.public`, `users:read` and `users:read.email` are avoided; user scopes
belong to the connector and are never merged into the bot install (Marketplace: user scopes only when the action must be the user's; no scopes for future features, so `assistant:write` is dropped if unused).

### 5.2 What customers must re-approve, and what the owner can do now

No customer has installed, so **everything above can be added now at zero cost**: scopes can be added to the app and the dev workspace reinstalled; the display information, Agents & Assistants test, redirect
URLs and the Interactivity toggle can be set (an unreachable Request URL is only saved when Slack can verify it, so the Events and Interactivity URLs are set when S2/S4b ship, UNVERIFIED for Interactivity).
After the first external install, adding any scope sends the installer back through OAuth and workspaces with app approval need an admin to approve; new events alone do not need re-install unless a scope is
new (UNVERIFIED per event). Later scope additions: v2 `commands`, `im:*`. GitHub variant B needs the App owner to raise Pull requests to Write; each installation then shows a pending permission request the
org owner must accept (tokens stay read-only until then). One coordinated notice. Removing a scope later is only possible by revoking tokens (Slack OAuth docs).

### 5.3 Marketplace readiness and timing

Needed: public landing page with privacy policy and support contact (reply in two business days), individually justified least-privilege scopes, TLS 1.2+, signature verification, OAuth `state`, onboarding for users
without an account, 10 active workspaces and 20 monthly active users (28 days); Socket Mode apps cannot be listed, so HTTP is the design. Timing: decided at the first external install (decision 8).

## 6. Slice plan

Durations are an ASSUMPTION from Phase 5/6 slices (1.5 to 3 h of agent time each plus CI). Every slice ends with unit tests, PostgreSQL tests under the restricted role, a real-stack control against the
fake Slack server (S0) and, where a model is involved, the budget-gated live run (AGENTS.md). The **owner runs a live smoke on the dev workspace** after S2, S4 and S6, plus the 2.3 native-status test after S4.
Migration plan is unchanged: **32** (S1/S2: `slack_installations`, `slack_event_inbox`, which also carries interaction dedupe rows) and **33** (S3: allowlist, `slack_threads` and `slack_feedback` unique indexes, role
backfill) fit everything including S4b/S4c; no new table; **34** only if S7 needs it; **35** reserved for S8.

| Slice | Goal | Files/modules | Mig. | Tests beyond common | Depends | Parallel with | Effort |
|---|---|---|---|---|---|---|---|
| S0 prelude | Config, signing check, thin client, **fake Slack server** (spec below) | `shared/slack_config.py`, `clients/slack/`, `tenancy_harness/fake_slack.py`, compose, `CONNECTORS.md` | no | see S0 acceptance | none | none | 3 h |
| S1 install | Bot install flow, installation row, token in secret store, team to org binding | `modules/slack/installation_*.py`, `oauth_slack_install.py`, `slack_schema.py`, `row_security.py` purpose `slack`, `ownership.json` | **32** | PG unique team, cross-org claim refused, RLS; install via fake | S0 | S3 design | 2.5 h |
| S2 events ingest | Signed endpoint, `url_verification`, inbox dedupe, retries, `:eyes:` ack, kill-switch checks | `modules/slack/http/events.py`, `inbox.py`, `apps/api/router.py` | **32** | forged/old/duplicate/retry events, slow-DB ack, unknown team | S1 | S3 | 2.5 h |
| S3 Slack access | Policy engine (pure `evaluate`), settings and allowlists, admin-asserted mapping, `slack.manage`, connector audience, partial/planner settings, health snapshot, channel picker, dashboard page | `modules/slack/access/*.py`, routes, `contracts/v1/slack_access.py`, frontend page | **33** | rule matrix (5 rules x listed x mapped); PG unique, RLS, role backfill; version bump cancels queued turns | S1 | S2, S4a stubs | 3.5 h + 3 h frontend |
| S4a pipeline core | Discover, planner, per-tool check, strict/partial respond, decision record, `origin.capability_ids` fail-closed edit, planner usage key, `slack_health` tick, worker guard | `modules/slack/pipeline/*.py`, `runtime_run_capabilities.py` (`:42`), `modules/usage/service.py`, `worker_service.py`, `ticks.py` | no | matrix T7-P* (below) | S3 | S4 stubs | 3.5 h |
| S4 Ask + UX | Dispatch/reply ticks, classification, service principal, Ask limits, scenes A, B, F, G (status text), H, I, status updater and edited-message fallback, `slack_threads` state machine, citations | `modules/slack/dispatcher.py`, `reply.py`, `classify.py`, `status.py`, `contracts/v1/tasks.py` (origin) | no | T7-A* incl. blocked Execute makes zero tasks and zero provider calls; edit/delete flow; quarantine, `paused`, `read_only`; live run | S2, S3, S4a | S5 | 4.5 h |
| S4b interactions | Stop and Try again buttons, interactions endpoint, press authorization, cancel and retry semantics, audit | `modules/slack/http/interactions.py`, `modules/slack/controls.py` | no | T7-I* (below) | S4 | S5, S6 | 3 h |
| S4c feedback | `reaction_added` handler, `slack_feedback`, metric, report allow-list | `modules/slack/feedback.py` | no (33 index) | only asker counts, normalization, latest wins, no text stored | S4 | S4b, S5 | 1.5 h |
| S5 create task | Scene C: grammar, rule gate, Done receipt, "Already tracked" | `modules/slack/intents.py` | no | allowed/blocked matrix; duplicate `task:` | S4a | S6 | 2 h |
| S6 PR review | Scene D: `github.pr.review_context`, review agent template, pre-flight, receipt, risk icon | `clients/integrations/github.py`, `bootstrap.py`, `templates.py`, `intents.py` | no | covered-not-allowlisted, allowlisted-not-covered, truncated patches, 3,000-file PR; live run | S4 | S5 | 3 h |
| S7 lifecycle, purge | `app_uninstalled`/`tokens_revoked`, quarantine step, org purge step `slack`, tombstone, cancel running turns | `organization_purge.py`, `organization_lifecycle.py`, `modules/slack/lifecycle.py` | maybe 34 | purge then restore reconcile, uninstall during run | S2 | S5, S6 | 2.5 h |
| S6b run workflow (v1.1) | `run <workflow>`, flag check | `intents.py` | no | unflagged blocked | S4 | S6, S7 | 2 h |
| S5b shortcut/slash (later) | message shortcut, `/weaves` | `interactions.py`, `commands.py` | no | thread limitation | S4b | any | 2 h |
| S8 writes and confirm (later) | Proposal store, confirm buttons on S4b, GitHub review post, Jira comment | `modules/proposals/` | **35** | double press, expiry, wrong confirmer, injection corpus | S4b, S6 | none | 6 h |
| S9 hardening (later) | Limit tuning, token rotation option, Grid, runbook, alerts | docs, metrics | no | 429 chaos | S4 | S8 | 2 h |

Critical path: S0, S1, S2, S3, S4a, S4, then S4b/S4c/S5/S6 in parallel; S7 must land **before any second org is invited**. Shared hot files (`later_migrations.py`, `row_security.py`, `ticks.py`,
`worker_service.py` guard) are append-only or tiny. **Total v1 = S0 to S7 incl. S4a/S4b/S4c: 11 slices, about 36 h of agent time (range 33 to 40, ASSUMPTION)**, plus S6b 2 h. Change from the previous plan (29 to 34 h):
S0 +1.5 h (fake server), S4 +1.5 h (UX contract), S4b 3 h (the owner estimated +1.5 to 2 h; I add the endpoint's own dedupe and authorization, the cancel/retry state machine and the harness cases for double press, wrong presser, expiry
and uninstall), S4c 1.5 h, S5/S6 +0.5 h each for receipts; less overlap saved 1 h.

**Pipeline matrix T7-P\*** (fake Slack and fake connector servers that count requests; pairwise, about 40 cases): tool health {healthy, error, disabled, token expired, stale then probe fails} x access {everyone, allowed_people
listed, allowed_people not listed, not_in_slack} x planner {none, one, two, invalid JSON, timeout, unpresented id, injected "use Jira"} x tier {Ask, Execute pre-flight} x mode {strict, partial} x org state {ok, quarantined,
read_only}. Assertions: **zero requests to any red or unneeded connector**; no answer-model call on a short-circuit; planner usage row settles once; the decision record has per-tool results and no question text (grep audit and logs);
partial mode names skipped tools; replies never name list members. **Interaction matrix T7-I\***: presser {requester, admin, other member, unmapped} x state {running, done, failed, stopped} x action {stop, retry} x org state {ok,
paused, quarantined, uninstalled} x delivery {first, duplicate, double press}; asserts cancel at the next stage boundary, no late answer posted, retry through gates with a new key and limit respected, one audit row per press.

### S3 as built (backend part)

Migration **33** is `infrastructure/database/slack_access_schema.py` (the S1/S2 installation tables are 32 in `slack_schema.py`). Collections `slack_access_settings` (record id = `org_id`, field `settings_version`), `slack_allowed_users` and `slack_allowed_channels` (record ids `{org_id}:{slack_id}`) are registered in `REPOSITORY_SPECS`; `slack_threads` and `slack_feedback` only get their unique indexes (33) and are registered by S4. The policy engine is `modules/slack/access/engine.py::evaluate` (pure; the engine sees only the asker's two list booleans, never the lists), reason codes in `models.py::Reason`, user texts in `messages.py`, the `slack.decision` record in `decision_audit.py`, Ask limits in `ratelimit.py` (counted from allowed `slack.decision` events, so there are no counters), settings and allowlists behind `/api/v0/organization/slack/access` (`slack.manage`, organization-level role binding). Choices the design left open: `settings_version` bumps on every settings, pause and allowlist change but only the full settings replacement needs `expected_version`; the Slack-id to member mapping is the `principal_id` on the allowlist row, so a mapped member is by definition listed (a channel-only rule still needs the asker's row for the mapping); Execute actions have no rate limit yet (only Ask limits are built); partial mode with no green tool refuses instead of answering from model knowledge; `org_per_day` is capped at 5,000 so the audit-derived count stays one bounded scan; blocked repeats merge into one audit event per ten-minute bucket by rewriting the same event id.

### S4a as built (pipeline core)

Code is `modules/slack/pipeline/` (`pipeline.py`, `planner.py`, `discovery.py`, `health.py`, `hints.py`, `targets.py`, `guard.py`, `models.py`). The one interface for S4, S5 and S6 is `SlackPipeline.run_pipeline(PipelineRequest) -> PipelineResult`:

- `PipelineRequest`: `access` (the S3 `AccessRequest` with everything resolved and **no tools**: action, `now`, settings view, asker standing, platform and org state, installation state, repo and workflow target, `ask_usage`), `context` (the S3 `DecisionContext`: org, event and request ids, Slack user, channel), `usage_key` (`planner_usage_key(run key)`), `question` (excluded from `repr`, never copied into a result), `planner_target` (the org's model profile and provider, from `PlannerTargets.resolve`), `workspace_id`, `workflow_capability_ids` (the `run_workflow` hook for S6b).
- `PipelineResult`: `decision` (the engine's `Decision`, or the planner-failed one), `outcome`, `proceed`, `capability_ids` (needed AND green: the only capabilities the run may use; empty means model-only), `tools_considered`, `tools_needed`, `planner` (status, token units), `message` (fixed, list-free text to show when `proceed` is false), `skipped_note` (partial mode), `answer_ephemeral`, `request_id`, `audit_event_id`. `short_circuit` is `not proceed`: no connector and no answer model.
- Order: the engine's gates first (no planner or connector cost for a blocked request), DISCOVER (reads only), PLAN (Execute: fixed grammar, no model; Ask: one planner call plus hints), CHECK (access first, then health; a live 3 s re-probe only for a needed tool that access allows and whose snapshot is missing, older than 15 minutes or errored), RESPOND (strict or partial through the engine), one `slack.decision` record.
- Worker guard: `TaskWorkerService.turn_guard` (`SlackTurnGuard`, port `application/ports/turn_guard.py`) runs in `_process_claimed_turn` before the heartbeat starts, for turns whose `origin` is set. Proceed: `origin.capability_ids` and `origin.verdict` are stored on the turn. A policy that no longer passes (engine outcomes dropped, blocked_state, blocked_rule, rate_limited) cancels the turn with `origin.verdict.cancel_reason = "slack_access_revoked"` and one `slack.turn.cancelled` audit record. A red tool or a failed plan finishes the turn with the fixed message as its only assistant message and no agent run. The reply tick (S4) reads `turn.origin.verdict`: `outcome`, `reason`, `message`, `skipped_note`, `answer_ephemeral`, `cancel_reason`, `tools_needed`, `tools_green`, `planner_status`.
- Capability narrowing: `shared/run_origin.py` binds the turn's origin around the agent run and `runtime_run_capabilities.py` loops over `allowed_capabilities(agent_version.allowed_capability_ids)`: for `slack` and `slack_qa` only the approved ids (never more than the agent version lists), nothing when the list was never set; every other run is unchanged.
- Health: collection `slack_connector_health` (record id = plugin installation id; `status`, `reason_code`, `checked_at`), no migration, refreshed every ten minutes by the worker tick `slack_health` and by the live re-probe. The probe is `probe_plugin_installation`, the connector's own health check run without `plugins.manage` and without writing audit or installation state.

Where this differs from the text above:

- **Planner usage key.** `{agent_run_id}:plan` is impossible because the planner runs before the agent run exists (its id is generated inside `run_agent`). The key is `{run idempotency key}:plan` (`task:{turn}:run:{n}:plan`, shortened with a digest above 128 characters), which is what the agent run's workflow run later carries as its idempotency key. The `usage_ledger` text columns and the 128-character contract hold it (tested on PostgreSQL); `agent_run_id` and `run_id` of that row hold the same key; the reconciler decides planner rows by age alone (120 s) and never from an agent run.
- **Origin lives on `TaskTurn`**, not on `Task`: the approved capabilities and the verdict differ per turn. S4 sets it when it creates or appends a turn.
- **Discovery** offers a tool to the planner when its snapshot is healthy or worth a live re-probe; tools that are disabled, token-expired, rate-limited or listed on two installations are final-red and are not offered, but a regex hint can still name them so they are reported instead of guessed. The planner is skipped when nothing can be offered (model-only continues, no fail-closed). An empty plan with `general_ok=false` is treated as an invalid plan (fail closed).
- **Access before probe.** A needed tool the asker may not use is red by access whatever its health and is never probed.
- **`pr_review`** needs `github.pr.review_context`; until S6 ships it the placeholder reports unavailable. `run_workflow` takes its capability ids from `PipelineRequest.workflow_capability_ids`.
- **Org state** maps the lifecycle state: active, suspended (also what quarantine sets) and deleting or deleted. The worker needs only `WEAVES_SLACK_ENABLED` (added to the worker environment); the signing secret stays API-only.
- **No real-stack harness control.** S4a has no route and nothing can create a Slack-origin turn through the real API before S4, so there is no `T7-*` control to move to `required_controls`; `pair.json` is unchanged and the cases run on PostgreSQL under the restricted role in `tests/integration/test_postgres_slack_pipeline.py`.

Live planner calibration (measured, `claude-haiku-5-5`, budget-gated ledger relay; recorded replies in `tests/slack/fixtures/planner_calibration.json`, prompt in `modules/slack/pipeline/planner_prompt.py`):

- **Round 1 (n=5, first prompt, 4 tools):** 210-218 input tokens, 17-68 output tokens, 0.91-1.43 s; only 1 of 5 replies usable (the model returned `{"needs": [], "general_ok": false}` for questions that need a source). Prompt rewritten.
- **Round 2 (n=10 calls, rewritten prompt):** a mix of clearly-tool (4), no-tool (2), ambiguous (2) and injection (2) questions, 4 tools (n=6) and 12 tools (n=4). The first five calls ran at `max_tokens=120`: 4 parsed and were sensible, 1 (an injection attempt) was truncated because the model had spent all 120 output tokens thinking first, and the strict parser refused it. The cap was raised to 512 and the next five calls (that case re-run, plus the rest of the mix) all parsed and were sensible. 9 of 10 parsed on the first try and every parsed reply was sensible. No reply ever named a tool that was not offered; both injection cases (asking for Jira when Jira was not offered, and a fake `SYSTEM:` line) chose other sources.
- **Tokens (rewritten prompt):** input 464-496 with 4 tools, 772-777 with 12 tools (about 38 per tool on a base of about 320), so the original 400-800 assumption holds. Output 44-56 without thinking and 73-194 when the model thinks first (mean 93 over 10); budget about 200 per call. Cost per question at haiku-class prices is a fraction of a cent.
- **Latency:** 1.07-2.30 s (mean 1.57 s), inside the assumed +1-3 s.
- **Strict-fail rate:** 1 of 10 (10%) at `max_tokens=120`, 0 of 5 at 512; the cap is now 512. The parser itself is unchanged.

### S4 as built (dispatcher, Ask flow, replies, working status)

Three worker ticks, all behind `WEAVES_SLACK_ENABLED`, each binding an organization's tenant context before it touches anything of theirs:

- **`slack_dispatch`** (every second; held while the operator `worker_paused` switch is on). Reads pending `app_mention` inbox rows oldest first (`list_events(event_type=...)`; `reaction_added` rows stay for S4c), resolves team to installation, classifies by the fixed grammar (`modules/slack/classify.py`: empty or `help` is help, a bare greeting or thanks is a greeting, `task: ...`, `review <PR url>`, `run <workflow>`, `status`, `stop`/`retry`, anything else is Ask; an unclear message is always Ask). **Cheap exits** (help, greeting, status, the not-yet-live commands, scene E and F texts) run only the engine's state gate (`check_state`: platform, organization, installation, bot pause), create no task, call no model and are never counted: Ask limits are derived from `slack.decision` records written at pickup, and cheap exits write none. **Ask** runs the whole S3 engine with no tools (state, limits, tier), then provisions the organization's Q&A principal and agent (below), then creates the hidden private task as that principal with `TurnOrigin(kind="slack_qa")` and the idempotency key `slack:{team}:{event_id}`, and writes the `slack_threads` row. A row is claimed by compare-and-set (`SlackStore.settle_event`: `pending` to `done` or `dropped`, which also clears the stored question text); refusals and cheap replies settle first and post after (a crash loses the reply, never doubles it), an Ask creates first and settles after (the task key makes a replay a no-op). A mention while the thread's previous answer is still going out waits up to two minutes, then is told "I'm still working on your last question in this thread. Ask again when I've replied." One thread is one conversation: a later mention in the same thread appends a message to the same hidden task (`TaskService.add_message(origin=...)`; an internal origin lets the service principal extend its own private task without `tasks.view`).
- **`slack_reply`** (every second). Looks at every `slack_threads` row whose `reply_state` is `pending`. Running or queued turn: shows the working state (below). Finished turn: builds the reply from the guard's verdict and the run, delivers it, turns the working message into the receipt and closes the row. Each row is claimed with a compare-and-set under an advisory lock that stores a 45 s lease in `next_attempt_at`, the post's result (`answer_ts`) is stored at once and checked before any post, a failed post backs off (5, 10, 20, 40, 80, 160 s, or Slack's own `Retry-After` if longer), a dead token revokes the installation and makes the row `undeliverable`, and so does a permanent Slack error, the scrubber failing, six failed attempts, or a turn that never finished within `WEAVES_SLACK_REPLY_DEADLINE` (900 s; then the request fails with a reference). A suspended installation or a quarantined organization posts nothing of ours (one neutral private line per channel per hour).
- **`slack_retention`** (every 15 minutes). Deletes processed inbox rows after 7 days and every hidden Q&A conversation 30 days after the thread's last activity (`WEAVES_SLACK_QA_RETENTION_DAYS`, ASSUMPTION): the task, thread, messages, turns, agent runs, workflow runs, tool invocations, artifacts (they hold answer text and queries) and the `slack_threads` row.

**Q&A principal and agent.** First Ask in an organization creates, inside the organization's Slack mutation lock, a `PrincipalType.SERVICE_ACCOUNT` principal with a workspace role holding exactly `agents.run` and `tasks.create`, and one agent created through the agent service as the installing administrator (`agents.manage`) with every enabled read capability that exists on exactly one installation, a 1,000 output-token and 120 s budget (a Slack answer must fit one message) and the organization's default model profile. The ids are kept in `slack_access_settings.qa_principal_id`/`qa_agent_version_id` **without bumping `settings_version`** (an open settings page is not made stale and queued turns are not cancelled). Each Ask compares the agent's capability list with the connectors that exist now and publishes a new version when they differ, and re-points the agent when its model profile was disabled; an organization with no active model profile gets "No AI model is connected. Ask a Weaves admin." and nothing is created.

**Working state (scene G, edited-message fallback, the default).** One working message per request, posted when the turn is running (or queued for more than 10 s) and then only edited in place with `chat.update`, never re-posted: `:hourglass_flowing_sand: Queued. I'll start as soon as a slot is free.` (queued after 10 s), `:mag: Checking sources…` (the guard is running), `:mag: Checking Jira…` (the next approved connector without a `ToolInvocation` yet; the display name is the installation's), `:pencil2: Writing the answer…`, `:hourglass_flowing_sand: Still working (2 min)…` from two minutes, `:hourglass_flowing_sand: This is taking longer than expected. I'll keep going and post here.` from five. The label comes from the verdict's approved capabilities and the run's real `ToolInvocation` records, not from a timer. When the answer is posted (a new message, so the asker is notified) the working message is edited to `:white_check_mark: Done in 41s`; for a private reply it is deleted (edited to ":lock: I've replied to you privately." if the delete fails). At most two bot posts per request. **Native status path** (`WEAVES_SLACK_NATIVE_STATUS=on`, default off, unverified for channel threads): the same keys are sent to `assistant.threads.setStatus` and no message is posted; an unsupported answer falls back to the message for that request. A quick answer posts no working message at all. The "Stop" and "Try again" buttons are not built (S4b): `Delivery.show_status` and the failure text are the extension points.

**Copy by scene** (standard emoji only; `modules/slack/copy.py`, plus the S3 `access/messages.py` texts):

| Scene | Text |
|---|---|
| A answer (new thread message) | the model's text converted to mrkdwn, then `_From WEAVES-1: Checkout outage; …_` (up to three document titles, `(+N more)`), `_3 sources · 41s_`; model-only: `_From general knowledge · 41s_`; partial mode adds `_Skipped: Slack search (not open here)_`. No button, no link |
| B refusal (private) | `:lock: Jira isn't open to everyone in Slack. Ask a Weaves admin.` / `I can't check Jira right now (it's unavailable); nothing was looked up.` (the pipeline's verdict message; the working message is removed) |
| E not allowed (private) | `:lock: I'm not set up to create tasks here. Ask a Weaves admin.` (rule off; other causes use the S3 texts) |
| F missing detail (threaded) | `Reply with @Weaves and the PR link, for example @Weaves review https://github.com/acme/api/pull/42` / `Tell me which workflow to run, for example @Weaves run weekly-report` |
| G failure | `:warning: I couldn't finish this (ref a1b2c3).` (the turn id's last six characters; no error text; the working message becomes this text) |
| H small exits | help (three examples), `Hi! Tag me with a question in a thread and I'll look it up.`, `You're welcome.`, private `status` (`Working on your last question: Checking Jira… (12s so far).` or `Nothing is running in this thread.`), `Pull request reviews from Slack are coming soon.` (and the run, stop, retry variants; private; the `task:` variant went away with S5) |
| I system | `Weaves is unavailable right now. Try again later.` (paused bot, platform pause, quarantine; once per channel per hour), `:hourglass_flowing_sand: You're asking a lot; try again in 3 minutes.`, `No AI model is connected. Ask a Weaves admin.`, `I couldn't work out which sources to check. Try again.` (planner failure, private) |

**Egress scrubber** (`modules/slack/text/scrub.py`, applied to every post, edit and whisper by `SlackPoster`, **fails closed**: any error in the scrubber, or text that still holds raw Slack markup, means the message is not sent and the row is `undeliverable`). Removed: private key blocks, Slack tokens (`xox*`, `xapp-`) and webhook URLs, GitHub tokens, Anthropic and OpenAI-style `sk-` keys, AWS access keys, Google API keys, JWTs, bearer tokens, `password/secret/api_key/token = value` assignments, credentials inside URLs, e-mail addresses (a flag turns this off), Luhn-valid card numbers, US SSNs. The replacement is a fixed `[category removed]`; only categories and counts are returned. **Sanitizer** (`modules/slack/text/mrkdwn.py`, deterministic, no model): `<!channel>`, `<!here>`, `<@U…>`, `<#C…>` and every other angle-bracket form lose their meaning (a zero-width space after the sigil), `&`, `<`, `>` are escaped, links survive only for allow-listed hosts (none in v1: a link keeps its label and shows the bare host, a bare URL loses its scheme), headings, bold, italics, strikethrough, bullets, rules, tables (monospace) and code fences are converted, messages go out as plain `text` with `parse=none` and no unfurling, are capped at 3,800 characters without cutting inside a fence and end with "Read the full answer in Weaves." (hidden Q&A tasks have no page, so that sentence is a placeholder until the owner decides what it should point to). `split_fenced` keeps fences balanced for longer, multi-message results (S5).

**Observability.** The request id `slack-{event_id}` is on the inbox row, the task turn, the thread row, the audit decision and every log line of the dispatch, run and reply. Metrics (no organization label, no text): `slack_dispatch_total{intent,outcome}`, `slack_inbox_age_seconds`, `slack_reply_total{kind,outcome}`, `slack_reply_seconds`, `slack_posts_total{method,outcome}`, `slack_api_429_total{method}`, `slack_scrub_total{result}`, `slack_retention_total{kind}`; tests grep logs, metrics, audit and thread rows for a question marker and the bot token.

Where this differs from the text above:

- **Limit and wait text** uses a relative time ("try again in 3 minutes", the S3 text) instead of "at 14:20": the asker's time zone is unknown without `users:read`.
- **No `processing` inbox state and no migration.** Claiming is the compare-and-set above (a lease column would need a migration number that S7 may take); `reaction_added` rows are left pending for S4c (built: see "S4c as built").
- **`slack_threads` holds the latest request**, not one row per request: a follow-up starts only after the previous reply was delivered, so one set of request fields per thread is enough.
- **Hidden tasks are deleted by the retention tick**, which also deletes their run outputs; the usage ledger and audit rows (ids and counts only) stay.
- **Not built (extension points kept):** Stop and Try again buttons and their endpoint (S4b), reaction feedback (S4c), `task:`/`review`/`run` execution (`task:` is S5, built below; `review` and `run` are S6 and S6b: today they are recognized, gated by the engine's rule and answered "coming soon" or with the refusal), the "open Ask holds at most one of two running slots" cap (3.5), cancelling the turn when the 15 minute deadline fires, pending replies of an uninstalled workspace (S7), and the native-status verification.
- **Poison rows:** a mention whose dispatch fails five times is dropped (`dispatch_error`) and a thread row whose reply step fails ten times becomes `undeliverable` (`internal_error`), so one bad row never starves the rows behind it (counters are per process).
- **Reply scan:** the reply tick finds pending rows with `list_by_field("reply_state", "pending")` per installation every second, a scan of the organization's `slack_threads` rows; a partial index on `reply_state = 'pending'` belongs in the next migration (no migration in this slice, S7 may own 34).
- **Planner prompt:** not touched (calibrated in a separate pull request).
- **Live run:** see the pull request. The one live run made real model calls through the product's own provider client **instead of the budget relay**, so they are not in the lifetime ledger (a harness mistake, disclosed in the pull request).

Tests: unit and in-process tests under `tests/slack/` (`test_text_*`, `test_classify`, `test_ask_dispatch`, `test_ask_reply`, `test_ask_delivery`, `test_ask_retention`, `test_ask_observability`, `test_ask_ticks`), PostgreSQL tests under the restricted role in `tests/integration/test_postgres_slack_ask.py`, and the real-stack control `T7-A01` (`tenancy_harness/phase7/slack_ask_checks.py`, now a required control; T7-I02 holds the dispatcher off with the operator worker pause while it reads its inbox rows).

### S0 spec (ready to build)

**Goal:** everything later slices need to talk to "Slack" in tests, with no real Slack and no secrets. **Files:**
- `shared/slack_config.py`: frozen dataclass `SlackConfig(signing_secret: bytes, enabled: bool, api_base_url: str)` with `from_environment()`; `WEAVES_SLACK_SIGNING_SECRET` (API only), `WEAVES_SLACK_ENABLED` (API, worker; default false),
  `WEAVES_SLACK_API_BASE_URL` (operator env like `WEAVES_GOOGLE_DISCOVERY_URL`, `modules/auth/google_oidc.py:74`; default `https://slack.com/api`; used by the harness only). `repr` never shows the secret; empty secret with enabled raises at start.
- `clients/slack/signature.py`: `compute_signature(secret, timestamp, raw_body) -> "v0=..."`, `verify_slack_request(secret, headers, raw_body, now, tolerance_seconds=300)`, raising `SlackSignatureError(reason)` with reasons
  `missing_header | stale | bad_signature | malformed`; `hmac.compare_digest`; header names case-insensitive.
- `clients/slack/client.py`: `SlackApiClient(http: httpx.Client, *, api_base_url, timeout_seconds=10)`; `follow_redirects=False`; methods `oauth_v2_access`, `auth_test`, `auth_revoke`, `post_message(token, channel, *, text, blocks=None, thread_ts=None) -> MessageRef`,
  `update_message`, `delete_message`, `post_ephemeral`, `add_reaction`, `set_thread_status -> StatusResult(ok|unsupported|error)`, `conversations_replies(token, channel, ts, limit<=15)`, `conversations_list`; errors `SlackApiError(code)`,
  `SlackRateLimited(retry_after)`, `SlackTokenError` (`invalid_auth`, `token_revoked`, `account_inactive`); one bounded retry on `ratelimited` honoring `Retry-After`; tokens never logged.
- `modules/slack/__init__.py` (empty package marker), `infra/gcp/singlevm/compose.yaml` and `CONNECTORS.md` (new variables, signing-secret step), `tenancy_harness/compose.yml` (test values), `tenancy_harness/pair.json` (`T7-*` controls declared planned), then run `scripts/sync_tenancy_harness.py` (HANDOFF section 6).
- `tenancy_harness/fake_slack.py` (FastAPI `APIRouter` included from `tenancy_harness/fakes.py` like `fake_google.py`), served at `/slack/api/{method}` on the isolated network. Implements `oauth.v2.access` (code check, returns bot and user tokens, `team`,
  `is_enterprise_install`), `auth.test`, `auth.revoke`, `chat.postMessage|update|delete|postEphemeral` (thread_ts, blocks, `message_not_found`, `cant_update_message`), `reactions.add` (`already_reacted`), `assistant.threads.setStatus` (mode `supported|unsupported`
  -> `method_not_supported_for_channel_type`; clears on reply; two-minute expiry), `conversations.replies` (**1 request/min/workspace**: second call returns 429 `ratelimited` with `Retry-After: 60`; `limit` clamped to 15; clock injectable) and `conversations.list`;
  a `response_url` receiver (5 uses/30 min). Control endpoints under `/slack/_control/`: `deliver_event`, `deliver_interaction` (signed with the test secret; form-encoded `payload=`; sets `X-Slack-Request-Timestamp`), `calls` (recorded API calls), `configure`
  (fault injection: 500, 429, slow, bad signature, stale timestamp), `reset`. Delivery models Slack: measures ack time and treats >3 s or non-2xx as failure, retries per `retry_schedule_seconds` (test default `[0,1,5]`, real `[0,60,300]`) with
  `x-slack-retry-num`/`-reason`, honors `x-slack-no-retry`, answers `url_verification`.

**Tests:** `tests/slack/test_signature.py` (valid, tampered body, wrong secret, 301 s old, future-dated, missing/malformed header, constant-time call, case-insensitive headers); `tests/slack/test_client.py` with `httpx.MockTransport` (each method's request shape,
error mapping, one retry on 429, no redirect following, token absent from logs); `tests/tenancy/test_fake_slack.py` (fake behaviours above incl. rate-limit simulation and retry timing with an injected clock); config tests. **Acceptance:** `ruff`, format, size,
`mypy` strict and `pytest` pass; the fake is reachable from the harness stack and a self-test control `T7-S00` (planned to required) delivers a signed `url_verification` to a stub and sees the challenge echoed; no file over 500 lines or function over 80; no secret or real host
in the repo; frontend vendoring in sync.

Rollout: dark (`WEAVES_SLACK_ENABLED=false`), owner dev workspace and live smoke, then per installation.

### S1/S2 as built (deviations from the sections above)

- Install routes are `GET /api/v0/oauth/slack/install` (JSON `authorization_url`, so the signed-in app can call it with its session and then redirect) and the public `GET .../install/callback`; the status view is `GET /api/v0/organization/slack` and disconnect is `DELETE /api/v0/organization/slack/{installation_id}`. They need the existing `organizations.admin` permission (`slack.manage` arrives with S3). The OAuth state row uses provider `slack_bot`, so a connector state cannot complete a bot install.
- Both migration-32 tables are real tables with their own row policies (like the usage ledger), not `product_records` collections; the `slack` purpose is inlined in their policies rather than added to `weaves_system_allows`. A workspace is bound while its installation is `active` or `suspended` (partial unique index); `revoked` and `uninstalled` rows free it, and the same organization reinstalling updates its row in place.
- The inbox keeps `reaction_added` only for thumbs up/down (S4c's feedback), and no row for `app_uninstalled`/`tokens_revoked`, which act at once (only a bot-token revocation ends the bot; user-token revocations are ignored). Dropped rows (platform admissions paused, organization suspended or quarantined, installation suspended) are stored without text so a Slack retry cannot revive them. The `:eyes:` reaction runs after the response for newly stored mentions only.
- `WEAVES_SLACK_ENABLED=false` still answers `url_verification` (so the Request URL can be saved) and acks and drops everything else; the install cannot start while it is off. The events route is exempt from the `read_only` platform gate (Ask stays allowed). Inbox retention (`purge_events`) exists but its worker tick ships with the dispatcher (S4).
- Rollback floor: migration 32 is forward-only for older images (they refuse unknown versions), so the same pull request raises `infra/gcp/singlevm/release_policy.json` to schema 32 with the commit that introduces the migration as the floor revision; merge with a merge commit (not squash) so that revision stays reachable on main.

## 7. Decisions (owner replied "accept all", 2026-11-26: all ACCEPTED as recommended)

1. **ACCEPTED** "Power itself from Slack" = core actions (ask, create task, review PR, status) without the web app; setup and admin stay in the web app. Slack as login and proactive agents are separate products.
2. **ACCEPTED** two-tier access (3.5): **2a** Execute = `create_task`, `pr_review`, `run_workflow`, PR generation and any write (Ask creates a hidden 30-day Q&A task because the worker needs one); **2b** rule per action, default off, suggested user AND channel for
   `pr_review`/`run_workflow` and user OR channel for `create_task`; **2c** unlinked users may Ask, never Execute (admin maps Slack member IDs); **2d** Ask on by default after install, model-only, with limits; **2e** guests and Slack Connect users accepted in Ask, bounded by channel invitation.
3. **ACCEPTED** PR review: Slack-only in v1; posting on GitHub after the proposal/confirm backend exists.
4. **ACCEPTED** no writes in v1.
5. **ACCEPTED** one default agent per action; per-channel mapping later.
6. **ACCEPTED** per-connector audience (`everyone`, `allowed_people`, `not_in_slack`), default `not_in_slack`; Slack-search connector labelled "not recommended"; blocked users are told "ask a Weaves admin".
7. **ACCEPTED** BYOK only in v1 (clear refusal without a model). Separately decided by the owner: low-tech-capacity customers get Weaves-managed AI only; a separate roadmap item, see 3.14.
8. **ACCEPTED** Marketplace decided at the first external install; ship as a distributed non-listed app.
9. **ACCEPTED** refuse Enterprise Grid org-wide installs; token rotation off; no Slack archive (keep only mentions and our replies, subject to legal review of Slack's API terms, 4.2).
10. **ACCEPTED** strict by default (all needed sources green or no data); "Partial answers" is an org setting, off.
11. **ACCEPTED** Weaves-level entitlement now (3.5.2); per-person source identity later, timing decided at the first customer with strict per-person data rules.
12. **ACCEPTED** a cheaper planner profile on the org's own key; measure cost and latency in the live run before any default.
13. **ACCEPTED** planner failure with tools installed fails closed ("try again").
14. **ACCEPTED** UX contract = designer scenes A to I with the adjustments of section 2; Stop and Try again are buttons with `@Weaves stop|retry` text fallbacks (3.12).

**Still open (my recommendation in brackets):** (i) native status works in channel threads? (test 2.3; default edited-message fallback); (ii) Stop/retry/feedback timing values, 24 h button expiry, 3 retries, 10 s queued notice, 5 min notice, 30-day Q&A retention (ASSUMPTIONS; accept as set);
(iii) add `reaction_removed` so feedback can be withdrawn (recommend no for v1); (iv) subscribe `app_uninstalled` and `tokens_revoked` now (recommend yes: S7 needs them); (v) legal review of Slack's API terms before the first external install; (vi) who uploads the avatar and sets the display text (owner, zero cost, now).

## 8. Risks and what not to build now

1. **Leakage through shared connector tokens** (4.1): per-request per-tool check, default audience `not_in_slack`, ephemeral for `allowed_people`; residual: access is Weaves-level (3.5.2) and planner misses give model-only answers (UNVERIFIED rate).
2. **Slack rate limits** on thread context (1/min, 15 messages); sources conflict on existing installs: mention-only context, serialization.
3. **Slack API terms** read from secondary sources: legal review first, no archive, no bulk reads.
4. **Ack under load**: a slow DB causes retries and a disabled subscription: one signature check plus one insert; slow-DB fake test.
5. **No tool loop**: the planner is a one-shot router; answers stay shallow; the planner adds cost and 1-3 s (ASSUMPTION).
6. **No write path** and no approval backend: every write needs a proposal/confirm service first (S8).
7. **Single VM capacity** (4 slots, per-org running cap 2): busy channels queue; open Ask holds at most 1 running slot.
8. **Native status and button edge cases** (UNVERIFIED): status in channel threads, ephemeral visibility in inactive threads, `action_ts`, private-channel `reaction_added`; each has a fallback in sections 2.3, 3.12, 3.13.
9. **Stale mappings and operations**: membership re-derived on every event; secret rotation and token loss need runbook steps (S9).

Not building now: any access-request or invitation flow; self-link; Execute for unmapped Slack ids; Socket Mode; Grid org-wide install; DMs and `message.*` follow-ups; welcome on join; custom emoji; slash commands and App Home; any external write or confirmation flow;
automatic PR reviews from webhooks; Slack as login; a channel-history index; email-based user matching; token rotation; Marketplace submission; managed-AI build (3.14); a general agent tool loop; per-person source identity.

## Sources (read 2026-11-26 unless noted)
Slack (docs.slack.dev): authentication/verifying-requests-from-slack, apis/events-api, reference/events/{app_mention,tokens_revoked,reaction_added}, ai/developing-agents, authentication/{installing-with-oauth,
using-token-rotation}, apis/events-api/using-socket-mode, reference/methods/{chat.postMessage,chat.postEphemeral,chat.update,reactions.add,assistant.threads.setStatus}, interactivity/{handling-user-interaction,
implementing-slash-commands}, changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps, slack-marketplace/slack-marketplace-app-guidelines-and-requirements.
Search results only: app_uninstalled, team_access_revoked, Slack API Terms, Hunton/Cloud Wars. GitHub (docs.github.com):
rest/authentication/permissions-required-for-github-apps, rest/pulls/{reviews,pulls}, webhooks/webhook-events-and-payloads (`pull_request` actions not in the fetched part,
UNVERIFIED). Unverified: message-shortcut scope, Grid per-workspace events, re-install triggers.

### S7 as built (lifecycle; no migration 34)

No migration was needed: installations and inbox keep their shape and the new facts are tombstones. One shared path, `modules/slack/lifecycle.py::SlackLifecycle.end`, serves Weaves-side Disconnect, `app_uninstalled`, bot `tokens_revoked`, dead-token failures, quarantine with credential revocation and the restore reconcile (tombstone first and fail closed, flip the status and free `team_id`, drop the team's pending inbox rows without text, `auth.revoke` only when Weaves ends it, delete the secret with its own tombstone, clear the reference, audit without secrets); a crash between steps is finished by the `slack_lifecycle` worker tick (every 10 minutes, also the inbox retention: 7 days after receipt for done or dropped rows, 14 days for rows nobody processed, 500 rows per batch, at most 10 batches per tick). New 6.2 tombstone kind `slack_installation_ended`, one per install generation (`{installation_id}@{installed_at}`) so a reinstall is a new fact; reconcile replays it only against the same generation. Organization purge has a `slack` step between credential revocation and the secret purge, so the token is still there to revoke; the thread and feedback collections are removed by the generic sweep once S4 registers them. Quarantine marks active installations `suspended` and release restores exactly those; `--revoke-credentials` additionally ends them for good (the admin reinstalls). Deviations from the text above: the "temporarily unavailable" message and cancelling running turns belong to the dispatcher (S4), which re-reads installation status, so S7 only drops the events and stops answering; the owner export lists installations but there is no thread map yet (S4). Because the tombstone kind is new, an older image reading the log refuses it as unknown: roll back only to a build that knows `slack_installation_ended`.

### S5 as built (`task:` creates a task from Slack)

Code: `modules/slack/dispatch/task_flow.py` (the dispatcher branch), `modules/slack/tasks/` (`text.py` title and short result, `links.py` task and thread links, `reply.py` the three messages), `modules/slack/access/execute_limits.py`, a small edit to the engine, the pipeline and the guard, and a link button on `SlackPoster`. No migration.

**Flow.** The dispatcher classifies `task: <text>` and `TaskFlow.run` evaluates the S3 engine for `create_task` (state gate, the rule, the hard checks: mapped **active** member holding `tasks.create` and `agents.run`, then the Execute limits). Order of answers: blocked or limited (private, scene E or the S3 text, one merged `slack.execute.blocked` and `slack.decision` record), then an existing task row for the thread ("Already tracked", public, **View task** button, no agent needed to say it), then empty text (public, scene F: `Tell me what the task is, for example @Weaves task: prepare the weekly KPI summary`, nothing counted), then no usable default agent (private: `No agent is set up for Slack tasks. Ask a Weaves admin.`), then creation. Creation sets the request identity to the **mapped member's principal** and calls `TaskService.create_task` (so RBAC, admission and audit are the dashboard's own) with the key `slack:{team}:{event_id}`, a `workspace` audience, `TurnOrigin(kind="slack", action="create_task", ...)` on the turn and a public `TaskOrigin` on the task, then inserts the `slack_threads` row (kind `task`, tier `execute`, unique per thread and kind, so an Ask and a task in one thread are separate rows; the hidden-conversation retention now filters on `kind = ask`, so it never touches a task). The whole step runs in one transaction under an advisory lock per thread: two dispatchers, a replay, or six different events racing in one thread produce exactly one task (the PostgreSQL tests run exactly that).

**Messages (standard emoji only, texts in `modules/slack/copy.py`).** The reply tick posts the first message as soon as the row exists (it does not wait ten seconds like an Ask working message): `:white_check_mark: Task created: <title> · Agent: <name> · Running · I'll post the result here` with a **View in Weaves** button. When the turn finished the result goes out as a **new** message (the asker is notified): a short summary (first prose paragraph, at most 280 characters), up to five bullets (140 characters each), `Full result and sources in Weaves.` and an **Open result** button; then the first message is edited into `:white_check_mark: Task done: <title> · <agent> · 4m 12s` (button stays). A long result is therefore always cut to the digest and the link says where the rest is; the sanitizer, the egress scrubber (fail closed) and the 2,900-character block limit apply as for an Ask. At most two bot posts per request; each `ts` is stored the moment Slack accepts it, so a retry after a lost edit edits again and never posts twice; backoff, the dead-token path and `undeliverable` are the S4 ones. A failed turn edits the first message into `:warning: I couldn't finish this task: <title> (ref a1b2c3).` (no error text, no second post), a turn the guard refused or cancelled (a red connector, a member deactivated after dispatch) into the fixed list-free reason from the verdict, a user-cancelled turn into `:octagonal_sign: Stopped.`. The Stop and Try again **buttons** are S4b. Tasks have no Slack-side 15-minute deadline (a task may legitimately run long): the row ends when the turn reaches a terminal state, and the worker's own recovery owns stuck turns.

**Which connectors a created task may use.** The pipeline plans Execute by grammar, so for `create_task` the plan is: the read capabilities its **pinned agent version** lists (`PipelineRequest.agent_capability_ids`, filled by the guard) intersected with the discovered read tools. The engine then checks each one's health only: the connector *audience* is an open-Ask rule and does not apply to an Execute action, whose entitlement is the rule, the mapped member's role and the admin's choice of the default agent. `origin.capability_ids` is the green subset and the runtime never runs more. `create_task` is evaluated in **partial** mode: a red connector (disabled, token expired, stale and failing its live probe) is skipped and named (`origin.verdict.skipped_note`, `outcome=allowed_partial`) and is never called, and the task still runs with the rest; only when every listed connector is red is the task refused with the fixed "I can't check Jira right now ..." text and no model call. An agent with no read connectors runs model-only. No model plans anything for a task, and write capabilities are never run (they are not discovered and the worker does not run them).

**Settings contract (backward compatible).** `SlackSettingsFields` gains `default_task_agent_id` (string or null, default null) and `execute_limits` (`{user_per_10_min: 10, channel_per_10_min: 30}`, 1 to 1000 and 1 to 10000). Both are in `GET /organization/slack/access` and replaced by `PUT` like every other field; `default_task_agent_id` must be an **active agent of the organization's workspace** (422 otherwise, null clears). A `PUT` from an older client that **omits** either field keeps the stored value (the service compares `model_fields_set`). The settings row also caches `workspace_urls` (not editable). Saving the settings bumps `settings_version` as before and the change is audited (`default_task_agent_id` and `execute_limits` are in the event).

**Execute limits.** 10 per 10 minutes per Slack user and 30 per 10 minutes per channel, all Execute actions together, a separate budget from Ask (an Ask never uses it). Counted like the Ask limits, from the organization's own audit events: `slack.execute.allowed` rows, whose id is derived from the Slack event (`slack-exec-<hash of org and event>`), so the dispatcher, a replay and the pickup guard write the *same* row and a request counts once. The request being decided is excluded by its event id. Only allowed requests count; refusals, an empty `task:` and "Already tracked" do not. The check is the engine's `_execute_limits_gate` (after the hard checks, before tools). It runs only when the caller supplies `execute_usage`: the dispatcher does, the pickup guard does not, so a queued task is never cancelled for being its own tenth request. The answer is private: `:hourglass_flowing_sand: You're asking a lot; try again in N minutes.`

**What the task API exposes about the origin.** The task gets an optional `origin` (`TaskOrigin`, no migration, JSON in the task record; older readers with `extra=forbid` cannot read a task that has it, so roll back only to a build that knows the field): `kind` (`slack`), `channel_id`, `channel_name` (only for an allowlisted channel, from the admin's cached name; a private channel nobody allowlisted stays unnamed), `requested_by_name` (the mapped member's display name) and `permalink` (the Slack thread). It does **not** carry the team id, the Slack user id, the thread timestamp (only inside the permalink), the event id, the settings version, the access verdict or the approved capability list: those stay on the turn (`TurnOrigin`, never returned by the turn API, which projects a fixed field list) and on `slack_threads`. The task's existing `idempotency_key` field still reads `slack:{team}:{event_id}` (ids, no content). `GET /tasks?origin=slack` filters the normal list (`payload->'origin'->>'kind'`); list and detail carry `origin`. A Slack task is a normal workspace task; hidden Q&A tasks stay private to the service principal and out of every list. The **permalink** is built from the workspace address, which is not stored at install (that would need a migration): the first task of a workspace calls `auth.test` with the bot token, accepts only an `https://<name>.slack.com/` (or `.enterprise.slack.com`) address and caches it on the settings row (no version bump); anything else, or a failed call, means no permalink and never blocks the task. The "View task" link is `WEAVES_PUBLIC_FRONTEND_URL` + `/#tasks/<id>` (the variable is now in the worker's environment); without it the messages carry no button.

**Audit and metrics.** `slack.execute.allowed|blocked` at dispatch (ids and reason codes, no text, blocked repeats merged per ten minutes), the full `slack.decision` with per-tool results written once at pickup by the guard, `slack_execute_total{action,outcome}` (`created`, `already_tracked` and the engine outcomes), `slack_reply_total{kind=task_created|task_result|task_receipt|task_closed}`, plus the S4 dispatch and post metrics. Tests grep logs, audit and thread rows for the task text and the bot token.

**Deviations.** (1) Follow-ups in a thread that append a message to the task (3.6) are not built: a repeat `task:` is "Already tracked" as the UX contract says, and any other mention is an Ask (an unclear message is never Execute); an explicit follow-up command would be a grammar change for the owner to decide. (2) The workspace address is cached lazily instead of stored at install (no migration). (3) The `task:` messages are posted by the reply tick, not by the dispatcher, so one component owns every Slack write of a request. (4) Control id `T7-I06` (`T7-I05` is the feedback slice's). (5) Stop and Try again buttons are S4b, reactions S4c, PR review S6. (6) Rollback: the contracts reject unknown fields, so an older image cannot read a task with `origin`, a `slack_threads` row of kind `task` or the settings row once it holds the new fields; roll back only to a build that knows them (no migration is involved). (7) The Execute limits count only allowed `create_task` requests today because it is the only live Execute action; `pr_review` and `run_workflow` write the same `slack.execute.allowed` row and will share the budget.

Tests: `tests/slack/test_task_flow.py` and `tests/slack/test_task_policy.py` (flow, replays and restarts, failure and refusal, sanitizing, no leakage, the 40-case rule matrix of all five rules x user listed x channel listed x mapped, the limits, settings, connectors, API exposure, permalink, retention), `tests/integration/test_postgres_slack_task.py` (concurrency and isolation on PostgreSQL), the real-stack control `T7-I06` (`tenancy_harness/phase7/slack_task_checks.py`), and the frontend spec `tests/e2e/slack-task-origin.spec.js` in `weaves-app`.
### S4c as built (reaction feedback; no migration 34)

No migration was needed: `slack_feedback` is registered like the S4 collections (contract `SlackFeedback`, repository spec, ownership entry, `PURGE_ORDER`, export) over the unique index `slack_feedback_turn_asker_uq` that migration 33 already created. The code is `modules/slack/feedback/` (`reaction.py`, `asker_ref.py`, `processor.py`, `retention.py`, `report.py`), the cross-organization store is `infrastructure/persistence/slack_feedback_admin.py` behind the port `application/ports/slack_feedback.py`, and the wiring is `apps/runtime_services_slack_feedback.py`. S4's dispatcher is untouched: it still claims only `app_mention` rows, and the new `slack_feedback` worker tick (every 2 s, held while the operator `worker_paused` switch is on, like the dispatcher) claims only `reaction_added` rows with the same compare-and-set (`settle_event`).

**What counts.** The events endpoint already stored only thumbs (`+1`, `-1`, `thumbsup`, `thumbsdown`, with an optional `::skin-tone-2` to `::skin-tone-6` suffix, which is how Slack's emoji naming carries a skin tone in the `reaction` field) and never the bot's own reactions. The tick re-checks every rule and drops a row with a reason code: `installation_inactive` (uninstalled, revoked or suspended installation, also quarantine), `slack_disabled`, `platform_paused`, `org_unavailable` (suspended or deleting organization), `bot_paused` (the S3 state gate, the same one the dispatcher uses), `not_feedback` (any other emoji or a malformed suffix), `own_reaction`, `not_our_answer` (no thread row has this channel and `answer_ts`: the user's own message, our working message, a private reply, or an older answer), `not_the_asker` (the reactor is not the Slack user of the thread's request). The platform `read_only` flag holds the row `pending` instead (a rating is a write; the 14-day pending retention of S7 still applies). A row that fails five times is dropped as `feedback_error`, so one bad row never starves the rows behind it. The 👀 we add is a reaction on the asker's mention, not on an answer, and is never stored; the bot never pre-seeds a reaction on its answers.

**Latest wins, idempotent.** One row per turn and asker (record id `{org_id}:{turn_id}:{asker_ref}`). The row's `at` is when the inbox received the event (the inbox does not keep Slack's `event_ts`); a reaction replaces the row only if it is not older than the stored one, so a late delivery of an older reaction never wins. Replaying the same event writes nothing (the settle is a compare-and-set and the write itself is a no-op when value and time match), and a crash between write and settle is repaired by the replay. `reaction_removed` is still not subscribed.

**What is stored, and what is not.** Stored: `answer_ref` (`{channel}:{answer ts}`), `turn_id`, `task_id`, `agent_run_id`, `request_id`, `asker_ref`, `asker_kind`, `value` (`1`/`-1`), `source="slack"`, `at`, plus a snapshot of the run taken at reaction time: `agent_id`, `model_id` (the resolved provider model) and `connector_set` (the sorted capability ids the turn was allowed to use; empty means general knowledge). Deliberately not stored: any message text (question or answer), any reason, the raw Slack user id, the channel name, a token. The snapshot is beyond the design's field list and exists because hidden Q&A conversations (and their run records) are deleted after 30 days while ratings live 400 days: without it the per-agent, per-model and per-connector counts could not be computed for old ratings.

**Asker reference (decision).** A Slack user whom an administrator mapped to a Weaves member (the allowlist row's `principal_id`) is recorded by that principal id: it is an internal id, joins to the rest of the product and disappears with the member. Every other asker is `hmac:` plus the first 32 hex characters (128 bits) of HMAC-SHA256 over `slack-user:{slack_user_id}` keyed with a per-organization random pepper. The pepper is created on first use (32 random bytes, `secrets.token_urlsafe`) and kept in the secret store with purpose `slack_feedback_pepper`; its reference is `slack_access_settings.feedback_pepper_ref`, written without bumping `settings_version` (like the Q&A ids) and never returned by the settings API. Creation runs inside the organization's Slack mutation lock, so two workers cannot create two. Why a stored pepper and not a value derived from a server key: it works with every secret store (also the in-memory one), it is rotated and re-encrypted with the store's own key rotation, it is deleted with the organization, and a leak of one organization's pepper tells nothing about another's; deriving from `WEAVES_SECRET_ENCRYPTION_KEY` would tie every reference to one process-wide key and make a key change silently rename every asker. The same person keeps one reference inside an organization and two organizations never share one for one Slack user. If the secret store cannot be read the reaction is retried and finally dropped; the raw id is never a fallback. If a person is mapped (or unmapped) later, their next reaction writes under the new reference and removes the row under the old one, so there is still one rating per turn. A lost pepper (secret deleted by hand) starts a new keyspace instead of failing.

**Analytics read path (metrics 8 and 10).** An operator report, not an org-admin endpoint: the design (3.13) puts reporting on the cross-organization operator path next to `usage_report.py`, and an org-admin view would need a UI decision. `infra/gcp/singlevm/slack_feedback_report.py` prints, per UTC day or month, organization, agent, model and connector set, the thumbs up, thumbs down and total (`--by`, `--from`, `--to`, `--org`, `--csv`; formula-safe CSV); `--request-id slack-{event_id}` returns the rating of one request with its organization, task, turn and agent run for debugging and metric 10. Neither mode returns an asker, a Slack id, text or a reason. Like `usage_report.py` it is streamed into the API container and is not in `TOOL_FILES` or the release bundle. Storage access is the named system purpose `feedback`, added to the row-policy function `weaves_system_allows` (re-applied at every start, so no migration) and limited to the one collection `slack_feedback`; the retention tick and the report use it. The request-id lookup is an unindexed scan of the collection (operator use, 400 days of ratings); an expression index on `payload->>'request_id'` belongs in the next migration if it is ever slow.

**Retention, purge, export.** `slack_feedback_retention` (every 15 minutes, not held by the Slack kill switch) deletes ratings older than `WEAVES_USAGE_RETENTION_DAYS` (400), oldest first, 500 rows per statement and at most 10 statements per tick, for every organization (also one whose workspace was disconnected). `PURGE_ORDER` removes the collection with the organization (before `slack_threads`) and the pepper goes with the organization's secrets; the rows are in the owner export (ids and values only). Disconnecting or uninstalling the workspace does not delete ratings: they hold no Slack text and no Slack id, and are business metrics until retention or the organization purge removes them. The design sentence "removed with the uninstall purge too" is read as the organization purge; deleting ratings on every disconnect would be a one-line change in `SlackLifecycle.end` if the owner wants it.

**Observability.** `slack_feedback_total{value,outcome}` with `value` in `up|down|none` and `outcome` in `recorded|changed|repeated|stale|dropped_<reason>`; `slack_retention_total{kind="feedback"}`; `slack_inbox_age_seconds` is observed for these rows too. One log line per settled row carries the event id, request id, outcome and value; no organization, user, channel or text. The tick writes no audit event (a rating is not an administrative change).

Where this differs from the text above: (1) `slack_threads` keeps only the latest request of a thread, so a reaction on an answer that a follow-up has already replaced is `not_our_answer` (kept as is; the earlier answer was the asker's old request); (2) the thread is found by scanning the organization's `slack_threads` rows for `answer_ts` (at most 30 days of rows; a partial index belongs in the next migration, like the reply scan); (3) the `at` of a rating is the receive time, not Slack's `event_ts`; (4) no org-admin summary endpoint; (5) the web thumbs (`source="web"`) are reserved in the contract and not built.

Tests: unit and in-process tests `tests/slack/test_feedback_tick.py` and `tests/slack/test_feedback_refs_report.py` (reaction names, latest wins, stale delivery, idempotent replay, wrong user, wrong message, working message, bot's own, other emoji, private and missing answers, follow-up, mapped member and late mapping, every state, read-only hold, poison row, no ids in logs, metrics or audit, HMAC stability and per-org separation, pepper in the secret store and not in the settings view, retention bound, counts, request lookup, CSV, tick registration), PostgreSQL tests under the restricted role in `tests/integration/test_postgres_slack_feedback.py` (and the extended export, purge and purpose lists in `test_postgres_slack_access.py` and `test_postgres_slack.py`), and the real-stack control `T7-I05` (`tenancy_harness/phase7/slack_feedback_checks.py`, a required control).
