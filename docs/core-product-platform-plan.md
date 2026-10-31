# Weave Core Product Platform Plan

## Purpose

Weave is an enterprise agent platform where customers can configure agents,
choose models, connect organization tools and data sources, and run approved
workflows from multiple surfaces.

This plan focuses only on the core product and system architecture. Detailed
packaging, pricing, and go-to-market tiers are intentionally out of scope until
the product model is stable. The core architecture should be deployment-mode
agnostic, with a managed-first launch path and a future customer-hosted mode.

The reset must happen in small, verifiable phases. The current incident-analysis
implementation is reference material only; it is not the architecture anchor for
the new product. See [platform-phase-0-reset.md](platform-phase-0-reset.md) for
the Phase 0 product reset.

## Current Repository State

The repository currently implements an incident-analysis vertical:

- Alert/API intake creates an investigation.
- A worker collects incident evidence from observability and GitHub.
- `IncidentAgent` performs a bounded tool-using LLM analysis.
- The engine verifies citations and confidence.
- Reports are persisted and delivered through Slack/API.
- Follow-up is allowed only inside an existing incident scope.

Useful reference material:

- Tenant-scoped strict contracts.
- Provider-neutral LLM request/response interface.
- Declarative tool catalog with JSON Schema validation.
- Tool dispatcher with timeout and invocation tracking.
- Observability capability registry.
- GitHub and Slack integration clients.
- Durable PostgreSQL jobs and delivery outbox.
- Redis idempotency and short-lived sessions.
- Field encryption for sensitive persisted JSON payloads.
- Structured logs, Prometheus metrics, request correlation IDs.
- Fail-closed authorization pattern, currently single-token and local.

Why this cannot remain the product center:

- The product model is incident-native.
- Jobs, reports, evidence, timelines, feedback, and usage all reference
  incidents directly.
- Configuration is mostly YAML/environment driven, not product/UI driven.
- There is no first-class organization, workspace, user, role, agent, model
  profile, plugin installation, workflow definition, approval, or audit event.
- Slack is an incident follow-up surface, not a general agent surface.
- Observability has a capability registry, but GitHub, Slack, Jira, MCPs, and
  future systems do not yet share a generic plugin/capability registry.
- The current `IncidentAgent` contains reusable runtime patterns, but it is not
  a generic configurable agent runtime.
- The new product should start from organizations, users, agents, model
  profiles, plugins, runs, artifacts, approvals, and audit events.

## Product Thesis

Weave is the layer above AI models.

Models provide reasoning. Weave provides the enterprise product layer:

- agent configuration
- model selection and routing
- tool/plugin connectivity
- context construction
- workflow execution
- policy and approvals
- memory and artifacts
- auditability
- collaboration surfaces

The customer should be able to create agents such as:

- Engineering Incident Analyst
- Jira Ticket Triage Agent
- PR Risk Reviewer
- Release Readiness Agent
- Support Escalation Agent
- Security Alert Triage Agent

Each agent is a configured product object, not just a prompt.

## Deployment Strategy

Weaves should have one core platform with two possible operating modes:

- managed Weaves, operated by us for customers who want a low-maintenance
  subscription product
- customer-hosted Weaves, operated by the customer for regulated or highly
  security-sensitive enterprise environments

Managed-first is the recommended launch path because it reduces customer
operational burden and speeds up onboarding. Customer-hosted support should be
kept possible by design, not built in the first implementation slices.

The core product must not assume that Weaves owns the model provider, storage,
secrets backend, plugin credentials, or runtime infrastructure. These concerns
should sit behind interfaces and configuration boundaries.

## Product Primitives

### Organization

An organization owns users, workspaces, agents, plugins, workflows, policies,
and audit history.

Initial fields:

- `org_id`
- `name`
- `status`
- `created_at`
- `updated_at`

### Workspace

A workspace scopes agents, workflows, plugin access, memory, and collaboration.
For smaller customers this can default to one workspace.

Initial fields:

- `workspace_id`
- `org_id`
- `name`
- `environment`
- `created_at`
- `updated_at`

### User

A user represents a human actor. Service accounts and system actors should be
separate principal types, not overloaded users.

Initial fields:

- `user_id`
- `org_id`
- `email`
- `display_name`
- `status`
- `created_at`
- `updated_at`

### Principal

A principal is the authenticated actor used for authorization and audit.

Types:

- `user`
- `service_account`
- `system`
- `external_app`

### Role And Permission

Permissions must become product-level capabilities instead of the current
single local admin token model.

Initial permission groups:

- manage agents
- run agents
- manage workflows
- run workflows
- manage plugins
- read artifacts
- approve actions
- view audit
- administer workspace

### Model Provider

A model provider describes an available model backend.

Examples:

- OpenAI
- Anthropic
- Gemini
- Azure OpenAI
- Bedrock
- local/provider-hosted endpoint

Initial fields:

- `provider_id`
- `org_id`
- `provider_type`
- `display_name`
- `auth_ref`
- `base_url`
- `allowed_models`
- `status`

### Model Profile

A model profile is what an agent actually chooses. It should hide provider
credential details from agent configuration.

Initial fields:

- `model_profile_id`
- `org_id`
- `provider_id`
- `model`
- `display_name`
- `default_temperature`
- `max_output_tokens`
- `cost_budget`
- `fallback_model_profile_id`
- `status`

### Agent Definition

An agent definition is a user-configurable persona and execution policy.

Initial fields:

- `agent_id`
- `org_id`
- `workspace_id`
- `name`
- `description`
- `persona`
- `instructions`
- `model_profile_id`
- `allowed_plugin_capabilities`
- `allowed_workflows`
- `memory_policy`
- `approval_policy_id`
- `budget_policy_id`
- `output_contract`
- `version`
- `status`

Important rule: agent definitions must be versioned. Existing runs must keep a
pointer to the exact agent version used.

### Plugin

A plugin is a type of external capability provider.

Examples:

- Slack
- GitHub
- Jira
- Datadog
- Grafana
- Prometheus
- Loki
- Tempo
- internal MCP server
- document store
- database

Initial fields:

- `plugin_id`
- `plugin_type`
- `display_name`
- `capability_manifest`
- `auth_schema`
- `status`

### Plugin Installation

A plugin installation is a configured instance of a plugin inside an
organization or workspace.

Initial fields:

- `plugin_installation_id`
- `org_id`
- `workspace_id`
- `plugin_id`
- `display_name`
- `auth_ref`
- `configuration`
- `capability_overrides`
- `status`
- `last_healthcheck_at`

### Capability

A capability is a single action or context operation exposed by a plugin.

Capabilities should describe both reads and writes.

Examples:

- `github.fetch_file`
- `github.search_code`
- `github.list_pull_requests`
- `github.comment_on_pr`
- `jira.get_issue`
- `jira.search_issues`
- `jira.create_issue`
- `slack.fetch_thread`
- `slack.post_message`
- `observability.search_logs`
- `observability.query_metrics`
- `mcp.call_tool`

Initial fields:

- `capability_id`
- `plugin_installation_id`
- `name`
- `description`
- `input_schema`
- `output_schema`
- `risk_level`
- `read_only`
- `requires_approval`
- `timeout_seconds`
- `rate_limit`

### Context Item

The current `EvidenceItem` is incident-specific. The platform needs a generic
context item that workflows can specialize.

Initial fields:

- `context_item_id`
- `org_id`
- `workspace_id`
- `source`
- `source_ref`
- `kind`
- `summary`
- `payload`
- `external_url`
- `collected_at`
- `retention_policy`

Incident evidence can later become a specialized context item or retain a
workflow-specific contract that references context items.

### Workflow Definition

A workflow definition describes a repeatable business process.

Initial examples:

- incident analysis
- Jira ticket triage
- PR risk review
- release readiness check

Initial fields:

- `workflow_id`
- `org_id`
- `workspace_id`
- `name`
- `workflow_type`
- `description`
- `trigger_schema`
- `input_schema`
- `output_schema`
- `required_capabilities`
- `default_agent_id`
- `steps`
- `version`
- `status`

### Workflow Run

A workflow run is the durable execution unit that should eventually replace
incident-specific top-level jobs.

Initial fields:

- `run_id`
- `org_id`
- `workspace_id`
- `workflow_id`
- `workflow_version`
- `trigger`
- `status`
- `requestor_principal_id`
- `idempotency_key`
- `started_at`
- `completed_at`
- `cancelled_at`
- `error`

### Agent Run

An agent run captures one invocation of one agent inside a workflow or direct
conversation.

Initial fields:

- `agent_run_id`
- `run_id`
- `agent_id`
- `agent_version`
- `model_profile_id`
- `status`
- `input`
- `output`
- `usage`
- `started_at`
- `completed_at`

### Tool Invocation

Tool invocation records should become durable and generic. The current in-memory
`ToolInvocation` is useful but not enough for enterprise audit.

Initial fields:

- `tool_invocation_id`
- `run_id`
- `agent_run_id`
- `capability_id`
- `input`
- `output_summary`
- `status`
- `risk_level`
- `approval_id`
- `duration_ms`
- `error`
- `created_at`

### Artifact

An artifact is any durable output produced by a workflow or agent.

Examples:

- incident report
- Jira triage summary
- PR review
- release risk report
- Slack answer
- structured JSON result

Initial fields:

- `artifact_id`
- `run_id`
- `artifact_type`
- `title`
- `payload`
- `summary`
- `created_by_agent_run_id`
- `version`
- `created_at`

### Conversation Thread

Threads allow Slack, dashboard, and API interactions to share continuity.

Initial fields:

- `thread_id`
- `org_id`
- `workspace_id`
- `surface`
- `external_ref`
- `scope_type`
- `scope_id`
- `created_at`
- `updated_at`

### Approval Request

Approvals gate risky actions.

Initial fields:

- `approval_id`
- `org_id`
- `workspace_id`
- `run_id`
- `requested_action`
- `risk_level`
- `requested_by_agent_run_id`
- `status`
- `approved_by_principal_id`
- `created_at`
- `resolved_at`

### Audit Event

Audit events are append-only product facts.

Initial fields:

- `audit_event_id`
- `org_id`
- `workspace_id`
- `principal_id`
- `event_type`
- `target_type`
- `target_id`
- `summary`
- `metadata`
- `created_at`

## Target Runtime Architecture

The future runtime should be composed of narrow services:

```text
Surface
  -> Request Router
  -> Workflow Runtime
  -> Agent Runtime
  -> Context Builder
  -> Plugin Gateway
  -> Policy Engine
  -> Artifact Store
  -> Delivery Router
  -> Audit Logger
```

### Surface Layer

Surfaces translate user interaction into product requests.

Initial surfaces:

- HTTP API
- Slack
- dashboard backend
- webhooks

Surface code should not contain workflow logic. It should authenticate,
normalize the request, call the router/runtime, and return or deliver results.

### Request Router

The router decides what the user is trying to do.

Inputs:

- principal
- workspace
- surface
- text or structured payload
- explicit agent/workflow selection if present
- conversation thread context

Outputs:

- direct agent run
- workflow run
- clarification request
- rejection

Initial rule: prefer explicit commands and configured defaults over broad
natural-language routing. Natural routing can be added after the product model
is stable.

### Workflow Runtime

The workflow runtime owns durable state transitions.

Responsibilities:

- validate workflow input
- create `WorkflowRun`
- enqueue executable jobs
- track status
- create artifacts
- call agent runtime where needed
- request approvals where needed
- dispatch delivery events
- handle retries/cancellation

### Agent Runtime

The agent runtime executes a configured agent version.

Responsibilities:

- load agent definition
- load model profile
- load allowed capabilities
- build system prompt from persona and instructions
- request context from context builders
- run bounded model/tool loop
- track usage and cost
- emit durable tool invocation records
- produce structured output
- run verifier when configured

### Context Builder

Context builders turn plugin capabilities into relevant model context.

Responsibilities:

- select context sources based on workflow, agent, and input
- fetch only needed context
- redact sensitive fields
- summarize large payloads
- store context snapshots when required
- return cited context item IDs

### Plugin Gateway

The plugin gateway is the only path from agents to external systems.

Responsibilities:

- validate capability input schema
- check principal/agent/workflow permission
- enforce timeouts and rate limits
- apply outbound allowlists
- handle secrets
- request approvals for risky actions
- record durable tool invocation/audit events
- normalize outputs

### Policy Engine

The policy engine should answer simple questions first:

- Can this principal run this agent?
- Can this agent use this capability?
- Can this workflow access this plugin installation?
- Does this action require approval?
- Is this model allowed for this workspace?
- Is this run within budget?

The first implementation can be deterministic and database-backed. Avoid adding
an external policy language until the product rules become too complex.

## Target Module Layout

Do not do this all at once. This is the destination structure.

```text
apps/
  api/
  worker/
clients/
  integrations/
  model_providers/
contracts/v1/
infrastructure/
modules/
  agents/
  auth/
  executions/
  governance/
  integrations/
  knowledge/
  memory/
  models/
  workflows/
shared/
```

The existing `api`, `investigation`, `evidence`, `followup`, and `delivery`
packages can be migrated into this layout gradually.

## Reset Strategy

The reset follows two rules:

1. Build platform primitives from the new enterprise-agent product model.
2. Keep each phase shippable and testable.

Avoid a large rename-only refactor. New platform code should not be constrained
by incident naming, but old code should only be deleted or moved when its
replacement exists and tests prove the product path.

## Phase 1: Product Domain Contracts

Goal: introduce the core product model without changing runtime behavior.

This phase is split into four independently reviewable platform-expansion
slices. The detailed contracts, invariants, tests, and legacy mapping are in
[platform-phase-1-contracts.md](platform-phase-1-contracts.md).

Small chunks:

1. `P1A`: organization, workspace, user, principal, role, role binding, and
   permission contracts.
2. `P1B`: model provider, model profile, agent definition, and immutable agent
   version contracts.
3. `P1C`: plugin descriptor, plugin installation, capability, workflow
   definition, and immutable workflow version contracts.
4. `P1D`: workflow run, agent run, tool invocation, artifact, approval, and
   audit contracts.
5. Add contract and architecture tests in each slice.
6. Do not depend on incident contracts or incident runtime modules.

Success criteria:

- No behavior changes.
- Existing tests still pass.
- New product contracts are strict and versioned.
- Product terminology is consistent across contracts and docs.

Do not in this phase:

- Build a dashboard.
- Change database schema.
- Move incident code.
- Add new workflows.

## Phase 2: Local Platform Runtime

Goal: prove the platform loop locally before integrating real external systems.

Small chunks:

1. Add in-memory or local repositories for organizations, workspaces, users,
   roles, model profiles, plugin installations, agents, runs, artifacts, and
   audit events.
2. Add deterministic service methods for creating a default organization and
   workspace.
3. Add a mock model provider that returns structured deterministic responses.
4. Add a mock plugin that exposes context-read capabilities.
5. Run one custom agent against mock context and store the resulting artifact.

Success criteria:

- A test can create an organization, workspace, model profile, plugin
  installation, agent version, run, artifact, and audit event.
- No real model provider or external API is required.
- No dashboard or database migration is required.

Do not in this phase:

- Add OAuth.
- Add real customer connectors.
- Add production persistence.
- Add autonomous writes.

## Phase 3: Model Gateway

Goal: make model execution a governed platform capability.

Small chunks:

1. Define a model gateway interface driven by `ModelProfile`.
2. Add provider-neutral request, response, usage, and error contracts.
3. Add one real provider adapter behind the gateway.
4. Keep mock provider support for fast tests.
5. Record resolved provider, model, usage, and cost estimates on agent runs.

Success criteria:

- Agents call models only through the model gateway.
- Model credentials are referenced through opaque secret references.
- Tests can run without paid or network-backed model calls.

Do not in this phase:

- Add every model provider.
- Add automatic model routing.
- Add billing.

Backend v0 records provider-reported token usage and calculates an estimated
USD cost when both per-million input and output rates are configured on the
model profile. Rates are operator supplied because provider pricing changes;
the estimate is not an invoice. Per-agent and per-model-profile cost caps are
enforced against provider-reported usage after completion. If usage exceeds a
cap, the run fails and its measured usage is retained. This check cannot prevent
the completed provider request from incurring charges.

## Phase 4: Plugin Gateway

Goal: make organization data access plug-and-play and governed.

Small chunks:

1. Implement generic capability registration from plugin descriptors.
2. Add `PluginGateway.dispatch(...)` for context reads.
3. Validate capability input and output schemas.
4. Enforce principal, agent, workflow, and workspace allowlists.
5. Record tool invocation and audit facts.
6. Add one real read-only connector after the mock connector is stable.

Success criteria:

- Agent runtime does not know connector-specific client code.
- Capabilities declare schemas, risk level, timeout, and approval behavior.
- New connectors can be added without changing agent execution logic.

Do not in this phase:

- Add write actions.
- Add marketplace behavior.
- Add arbitrary MCP calls.

## Phase 5: Product APIs

Goal: expose the platform model through API endpoints before building the UI.

Small chunks:

1. Add read APIs for agents, model profiles, plugin installations,
   capabilities, workflow definitions, workflow runs, artifacts, and audit
   events.
2. Add create/update APIs only for low-risk objects first:
   `AgentDefinition`, `ModelProfile`, and `WorkflowDefinition`.
3. Keep plugin credentials and write actions out of API until secret handling
   and approvals are ready.
4. Add API authorization checks using the new permission model.

Success criteria:

- A dashboard could be built against these APIs.
- Product objects can be configured without changing YAML for every detail.
- Existing operator config remains supported as bootstrap/default config.

The local v0 API supports operator-configured bearer tokens bound to active
principals and admin-managed service-account credentials with persisted token
digests, expiry, and revocation. Human users can enroll a password, sign in to
a 12-hour bearer session, and revoke sessions. Workspace admins can issue
seven-day, one-time invitations; acceptance activates the account, sets its
initial password, and creates a session. Passwords and issued bearer tokens
are stored as one-way digests/verifiers. Role and workspace access is resolved
from the authenticated principal on each request. PostgreSQL mode rejects
unauthenticated requests to protected routes even when no environment token is
configured; health, onboarding, login, and invitation acceptance remain
public. Login throttling uses shared PostgreSQL fixed-window counters for
account and source-address buckets; memory mode is process-local. Email delivery
and identity proof, MFA, and SSO/OIDC remain open.

Run creation accepts an optional `Idempotency-Key`, replays successful
requests, and rejects conflicting reuse. PostgreSQL enforces the key with a
partial unique index. Startup marks synchronous runs stranded beyond the
four-hour heartbeat threshold failed and records a recovery audit event.

The API also exposes durable PostgreSQL-backed execution jobs for asynchronous
agent runs. A separate worker claims each job atomically, invokes the same
runtime, and records a linked run and terminal job state. Jobs are visible to
their requester or workspace user administrators. Queued jobs can be cancelled
immediately; running jobs accept a cooperative cancellation request that takes
effect between context calls and before model execution for standalone agent
jobs, or between workflow steps. A request during the final call may arrive too
late to cancel completed work; successful run and job states remain successful.
Retryable failed jobs can be manually retried with explicit lineage and
idempotency. Workers renew a
fenced lease every 20 seconds; stale jobs are
reconciled after two minutes without a heartbeat. The worker cannot interrupt
an external call already in progress. Automatic retries and production
multi-tenant worker isolation remain open; see
[product-execution-queue.md](product-execution-queue.md).

Do not in this phase:

- Build frontend UI.
- Add OAuth flows.
- Add user-created write tools.

## Phase 6: Dashboard Skeleton

Goal: build the first real product surface.

Small chunks:

1. Add dashboard app shell.
2. Add agent list/detail.
3. Add model profile list/detail.
4. Add plugin/capability health view.
5. Add workflow run history.
6. Add artifact/report viewer.

Success criteria:

- Users can understand what agents exist, what they can access, and what they
  ran.
- The dashboard does not need to support every admin action yet.

Do not in this phase:

- Build a full visual workflow designer.
- Add complex onboarding.

## Phase 7: First Real Workflow

Goal: prove the architecture works for a real enterprise workflow using
multiple organization context sources.

Recommended first workflow: engineering assistant over GitHub, Slack, and Jira.

Why:

- It exercises multiple common enterprise data sources.
- It is useful without production writes.
- It can start read-only and approval-friendly.
- It is useful for many teams.

Small chunks:

1. Add GitHub read capabilities.
2. Add Slack thread/channel context reads.
3. Add Jira issue reads.
4. Add a built-in engineering assistant template.
5. Produce artifacts with cited context and suggested next steps.

Success criteria:

- The same agent runtime and plugin gateway power the workflow.
- The product can answer useful organization-specific questions using multiple
  plugins.
- All external actions remain read-only or draft-only.

Do not in this phase:

- Auto-update Jira tickets.
- Post Slack messages autonomously.
- Add broad unrestricted organization chat.

## Phase 8: Approvals And Safe Writes

Goal: introduce controlled write actions.

Small chunks:

1. Add approval policy contracts.
2. Add approval request lifecycle.
3. Add Slack/dashboard approval surface.
4. Enable one low-risk write action, such as posting a Slack message or adding
   a Jira comment.
5. Add audit records for request, approval, execution, and result.

Success criteria:

- Agents can propose actions.
- Humans can approve actions.
- The plugin gateway blocks unapproved risky actions.
- Audit trail is complete.

Do not in this phase:

- Enable production-remediation actions.
- Enable source-code writes.

Backend v0 currently includes a seeded approval policy and request lifecycle,
plus one approval-gated Jira comment capability. The approval surface is API
only and authorizes the authenticated request principal (falling back to the
seeded local developer only in local memory mode). The seeded local policy
permits self-approval; organization/workspace defaults forbid it. A database
compare-and-set on the pending approval allows only one API process to dispatch
the approved action. Database state transitions are transactional, but the
external Jira write cannot share the database transaction. Production reviewer
identity verification, connector idempotency, and uncertain-result
reconciliation remain open work. Startup and worker recovery move interrupted
`EXECUTING` invocations to non-retryable `TIMED_OUT` with an explicit
unknown-outcome audit record; they do not infer whether Jira applied the write.

## Phase 9: MCP Support

Backend v0 now supports stateless MCP Streamable HTTP (`2026-07-28`). Admins
discover tools, select an explicit read-only allowlist, and import compatible
query schemas as installation-scoped capabilities. Calls route through the
plugin gateway and share its schema validation and invocation tracking. The
current adapter is read-only, bearer-token only, requires exact HTTPS host
allowlisting, and does not support stdio or legacy handshake protocol versions.
See [product-mcp-v0.md](product-mcp-v0.md). OAuth, async/task results, and broader
input schemas remain open.

Goal: add internal tool extensibility through MCP without weakening governance.

Small chunks:

1. Add MCP plugin type.
2. Import MCP tool schemas as capability specs.
3. Enforce allowlists per MCP server and capability.
4. Route MCP tool calls through plugin gateway.
5. Record MCP invocation audit events.
6. Add health/status visibility.

Success criteria:

- Internal tools can be plugged in without changing agent runtime code.
- MCP calls have the same policy, timeout, audit, and redaction behavior as
  native plugins.

Do not in this phase:

- Let agents call arbitrary MCP tools by default.
- Bypass capability registration.

## Phase 10: Memory And Knowledge

Backend v0 has explicit `MemoryItem` and `MemoryPolicy` contracts. The local
runtime supports organization/workspace scoped records, expiry filtering,
audited create/delete, and bounded retrieval through `MemoryContextBuilder`.
Admins can select a retention class and optional expiry; expired items are
hidden immediately, then physically deleted and audited at API startup and by
the worker's 30-second maintenance loop.
Open conversation threads also pass up to eight recent successful task/response
turns (at most 24,000 characters) into each follow-up model call.
All five memory policy scopes are retrievable: conversation-thread items require
the matching open thread, workflow items require the matching workflow, and
workspace/organization items follow their respective tenant scopes. Thread
lifecycle, run binding, expiry checks, auditable deletion, and source citations
are implemented. Semantic/vector indexing and ranking beyond lexical overlap
are still open.

Goal: add controlled memory after the execution and audit model is stable.

Small chunks:

1. Define memory scopes: none, thread, workflow, workspace, org.
2. Add retention and deletion policies.
3. Add context item indexing hooks.
4. Add retrieval through context builder, not direct agent access.
5. Add citations to retrieved memory.

Success criteria:

- Memory improves context without becoming unbounded data leakage.
- Users can inspect and delete stored memory.
- Agent outputs cite memory/context items.

Do not in this phase:

- Add hidden memory.
- Store all messages forever by default.

## Engineering Guardrails

Every phase should follow these rules:

- Keep changes small and independently reviewable.
- Treat the incident-analysis implementation as reference material until a
  platform replacement exists.
- Prefer additive platform contracts before destructive code removal.
- Use strict Pydantic models with `extra="forbid"`.
- Version user-configurable definitions.
- Store durable run facts, not only logs.
- Route all external calls through a gateway.
- Separate read capabilities from write capabilities.
- Require explicit approval for risky writes.
- Keep model provider details behind model profiles.
- Keep deployment-owned dependencies behind interfaces.
- Use interfaces for storage/runtime boundaries.
- Add tests at the contract and service level before UI work.
- Avoid introducing a second framework or service unless the current modular
  monolith is demonstrably blocking progress.

## Testing Strategy

Each small phase should include:

- contract validation tests
- persistence tests when a phase adds storage
- migration tests when schema changes are introduced
- authorization tests for new permissions
- gateway tests for capability allowlists and schema validation
- workflow runtime tests with in-memory model/plugin implementations
- regression tests for any legacy code touched by the phase

Before changing runtime behavior, run:

```bash
pytest
ruff check .
ruff format --check .
mypy
```

When schema or worker behavior changes, also run Postgres/Redis-backed
integration tests.

## First Implementation Slice

The first build slice should be intentionally small:

1. Implement `P1A` only from
   [platform-phase-1-contracts.md](platform-phase-1-contracts.md).
2. Add organization, workspace, user, principal, role, role binding, and
   permission contracts.
3. Add contract, schema export, and dependency-boundary tests.
4. Do not add model, agent, plugin, workflow, run, API, or persistence code.
5. Do not change runtime behavior.

This creates the language and type boundaries needed for the rest of the
platform without pulling incident concepts into the new foundation.

Recommended first PR:

```text
contracts: add organization and workspace identity models
```

Files likely touched:

```text
contracts/v1/
tests/contract/test_product_identity_contracts.py
tests/architecture/test_product_contract_dependencies.py
scripts/export_contract_schemas.py
schemas/product/v1/
```

## Open Product Decisions

The P1 architecture decisions are recorded in
[platform-phase-1-contracts.md](platform-phase-1-contracts.md). The remaining
decisions should be made before implementation reaches UI or workflow
expansion:

- Which real connector follows the mock plugin?
- Should the first usable surface be API-first or dashboard-first?
- What are the first three built-in workflow types?
- What plugin capabilities are read-only in the initial platform release?
- What is the first approved write action?
- How much conversation history should be retained by default?

## Non-Goals For The Core Planning Stage

- Pricing and packaging.
- Detailed deployment topology.
- Plugin marketplace.
- Multi-agent autonomous swarms.
- Autonomous remediation.
- Source-code writes.
- Generic unrestricted organization chat.
- Full visual workflow builder.
- Long-term memory before audit and retention controls.

## Guiding Principle

Weave should become extensible by building reusable platform primitives around
organizations, configurable agents, model profiles, plugins, runs, artifacts,
approvals, and audit.

The sequence is:

```text
product contracts
  -> model profiles
  -> agent definitions
  -> capability registry
  -> plugin gateway
  -> workflow runs
  -> product APIs
  -> dashboard
  -> second workflow
  -> approvals
  -> MCP
  -> memory
```

At every step, legacy incident code remains reference material unless that step
explicitly replaces it.
