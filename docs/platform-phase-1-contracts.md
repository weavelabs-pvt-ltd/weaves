# Platform Expansion Phase 1: Product Contracts

## Status

P1A identity, P1B model/agent, P1C plugin/capability/workflow, and P1D
execution/governance contracts and generated schemas have been added under
`contracts/v1` and `schemas/product/v1`. The contract
and architecture test set passes. P1A–P1D do not change runtime, persistence,
API, or database behavior.

The repository already contains an incident-analysis implementation. The new
platform work uses `P1A` through `P1D` to describe the first product-contract
phase for the enterprise agent platform.

## Outcome

Phase P1 establishes the product language that later runtime and persistence
work will implement. It introduces strict, versioned contracts for:

- organization and workspace scope
- identities and authorization assignments
- model and agent configuration
- plugins and capabilities
- workflow definitions
- durable execution facts and artifacts

P1 establishes clean platform language without depending on incident contracts
or runtime modules. P1A through P1D are implemented as contract-only slices.

## Architecture Decisions For P1

These decisions are narrow enough to unblock contract design. They can be
revisited before persistence or public APIs make them expensive to change.

### Workspace Is Mandatory Internally

Every executable agent, workflow, plugin installation, run, and artifact has a
`workspace_id`. A small customer receives one default workspace that the UI can
hide until they need more than one.

This keeps authorization and data isolation explicit. It avoids nullable scope
fields whose meaning changes between products and customers.

Organization-level objects are limited to objects that are intentionally
shared, such as model profiles, users, and role definitions.

### Platform IDs Are First-Class Product Terms

New platform contracts use `org_id`, `workspace_id`, `agent_id`,
`plugin_installation_id`, and `run_id`.

The current incident implementation may contain older names such as
`tenant_id` or `installation_id`. Those names are legacy implementation details
and do not shape new platform contracts. P1 does not rename or migrate old
database columns because it does not touch persistence.

### Executable Definitions Have Immutable Versions

Agents and workflows use two objects:

- a stable definition for identity, display metadata, status, and the current
  version pointer
- an immutable version containing the executable configuration

Runs reference exact version IDs. Editing an agent or workflow creates a new
version instead of mutating history.

### Agents Are Workspace-Scoped

An executable agent belongs to one workspace. Reusable organization-wide
templates may be added later as a separate concept; they are not executable
agents and are outside P1.

### Model Profiles Are Organization-Scoped

A model profile can be reused across workspaces in one organization. Workspace
access is enforced by authorization policy rather than by copying credentials
or provider configuration.

An agent references a model profile, never raw provider credentials.

### Plugin Installations Are Workspace-Scoped

A plugin descriptor describes a connector type. A plugin installation is a
configured instance available to one workspace. Organization-wide connector
sharing can later be represented by explicit workspace bindings instead of a
nullable scope.

### Workflows Are Code-Backed With Typed Configuration

The initial workflow definition selects a registered handler by `handler_key`
and supplies validated configuration. The workflow graph is not arbitrary
user-authored JSON in P1.

This supports built-in workflows without committing to a visual workflow
language. A declarative graph can be added behind a new handler type later.

### Pydantic Is The Source; JSON Schema Is The Published Form

Product contracts follow the repository's existing convention:

- strict Pydantic v2 models are the source of truth
- unknown fields are rejected
- contracts carry `schema_version`
- published contract models have checked-in JSON Schema

Product schemas should live under `schemas/product/v1` so the enterprise
platform contract surface is clearly separated from older contract schemas.

## Package Boundary

The destination for P1 contracts is:

```text
contracts/v1/
  base.py
  identity.py
  agents.py
  plugins.py
  workflows.py
  executions.py
```

`contracts.v1` may reuse shared contract primitives. It must not import the API,
persistence, worker, integration, or configuration packages.

The package contains data contracts and validation only. Repository protocols,
database models, service methods, and provider clients belong to later phases.

The root `contracts` package should not re-export every model. Callers use the
versioned contract package explicitly, which prevents the root namespace from
becoming an accidental public API.

## Shared Contract Rules

### IDs

- IDs are opaque non-empty strings with a maximum length of 128 characters.
- Contracts do not require UUIDs; ID generation is a persistence concern.
- External provider IDs are stored in explicitly named external reference
  fields, not reused as Weave resource IDs.

### Scope

Use separate bases rather than one object with an optional workspace:

```text
ProductContract
  OrgScopedContract
    WorkspaceScopedContract
```

Nested objects must match the organization and workspace of their parent.
Cross-scope references are rejected when the relevant scope is present in the
same payload. References requiring a lookup are validated by a service in a
later phase, not guessed inside a Pydantic model.

### Time

- Durable timestamps must include a timezone.
- Mutable resources carry `created_at` and `updated_at`.
- Immutable versions carry `created_at` and no `updated_at`.
- Run events record observed timestamps; they do not silently generate missing
  start or finish times during validation.

### Status

Status fields use `StrEnum`, not arbitrary strings. Status transitions are not
implemented in P1; later domain services will own transition rules.

### Configuration

- Credentials are represented only by opaque `auth_ref` or `secret_ref`
  values.
- Product models do not define fields for tokens, private keys, or provider
  credential blobs.
- Free-form configuration is limited to JSON-compatible values and bounded in
  size at API ingress when APIs are added.
- Capability IDs and other allowlists reject duplicates.
- Policy behavior is referenced by ID until the policy model is designed.

P1 contracts are not a secret-detection boundary. When plugin services are
implemented, they must validate configuration against the plugin's public
configuration schema, extract credential fields through its credential schema,
store only a secret reference, and redact rejected payloads from logs.

## P1A: Organization, Workspace, And Identity

### Purpose

Create the minimum scope and actor model required by every later product
contract.

### Contracts

- `Organization`
- `Workspace`
- `User`
- `Principal`
- `Role`
- `RoleBinding`
- `Permission`
- `OrganizationStatus`
- `WorkspaceStatus`
- `UserStatus`
- `PrincipalStatus`
- organization and workspace scoped base contracts

`Principal` represents the authenticated actor used in authorization and
audit. Initial principal types are `user`, `service_account`, `system`, and
`external_app`. A human `User` has a corresponding principal; service accounts
and external apps do not masquerade as users.

`RoleBinding` assigns one role to one principal at organization or workspace
scope. Its optional `workspace_id` has one explicit meaning: absent means an
organization-wide assignment; present means an assignment only in that
workspace.

### Initial Permissions

- `organizations.admin`
- `workspaces.admin`
- `users.manage`
- `roles.manage`
- `models.manage`
- `agents.manage`
- `agents.run`
- `workflows.manage`
- `workflows.run`
- `plugins.manage`
- `artifacts.read`
- `actions.approve`
- `audit.read`

Permissions are stable machine values. UI labels are not stored in the enum.

### Required Tests

- unknown fields are rejected
- empty and overlong IDs are rejected
- timestamps must be timezone-aware
- a workspace requires its organization scope
- user and principal status values are closed enums
- user principals require a user reference
- non-user principals reject a user reference
- workspace role bindings retain both organization and workspace scope
- organization role bindings cannot accidentally carry an empty workspace ID
- JSON schema export matches checked-in P1A schemas
- the new package has no imports from runtime or infrastructure packages

### Exit Criteria

- P1A contracts and tests pass
- all relevant existing tests pass unchanged
- no API, database, worker, or legacy incident contract changes
- no authentication implementation

## P1B: Models And Agent Definitions

### Purpose

Represent how an organization configures model access and publishes an
executable, versioned agent.

### Contracts

- `ModelProvider`
- `ModelProfile`
- `AgentDefinition`
- `AgentVersion`
- `AgentStatus`
- `ModelProviderStatus`
- `ModelProfileStatus`

`ModelProvider` contains provider type, display metadata, allowed model names,
endpoint metadata, and `auth_ref`. `ModelProfile` contains the selectable model
name and bounded inference defaults. Neither contains a decrypted secret.

`AgentDefinition` contains stable identity and lifecycle metadata.
`AgentVersion` contains persona, instructions, model profile reference,
capability allowlist, allowed workflow IDs, policy references, budget settings,
and an optional output schema.

An agent version is immutable after validation. Version numbers are positive
integers, but the stable reference used by runs is `agent_version_id`.

### Required Tests

- provider contracts cannot contain raw credential fields
- model temperature, output token, and budget values are bounded
- fallback profiles cannot reference themselves
- agent versions are immutable
- agent version scope matches its agent definition in composed fixtures
- capability and workflow allowlists reject duplicates
- blank persona or instructions are rejected
- output schema is JSON-compatible
- schema exports are stable

### Exit Criteria

- agents can describe a useful assistant persona without executing it
- no model provider client or model registry is added
- current Anthropic configuration remains the runtime source of truth

## P1C: Plugins, Capabilities, And Workflow Definitions

### Purpose

Define the catalog and configuration language required for plug-and-play
context sources and code-backed workflows.

### Contracts

- `PluginDescriptor`
- `CapabilitySpec`
- `PluginInstallation`
- `PluginInstallationStatus`
- `CapabilityKind`
- `RiskLevel`
- `WorkflowDefinition`
- `WorkflowVersion`
- `WorkflowStatus`

Initial capability kinds are `context.read`, `action.write`, and
`delivery.send`. Keeping risk separate from kind allows a context operation to
be classified as sensitive even though it is read-only.

`CapabilitySpec` includes input and output JSON Schemas, risk level, timeout
ceiling, and whether approval can be required. It contains no executable code.

`PluginInstallation` references a plugin descriptor and an opaque auth
reference. It stores non-secret configuration and capability enablement for one
workspace.

`WorkflowVersion` references a registered `handler_key`, exact agent version
IDs, an input schema, an output schema, and typed JSON-compatible
configuration. Publishing a changed configuration creates a new version.

### Required Tests

- plugin installation scope is mandatory
- plugin installation rejects credential-shaped top-level fields
- capability input and output schemas are valid JSON Schema
- duplicate capability IDs are rejected
- write and delivery capabilities require an explicit risk level
- workflow versions are immutable
- workflow versions require a registered-looking handler key
- workflow agent-version references reject duplicates
- schema exports are stable

### Exit Criteria

- observability, GitHub, Slack, Jira, and MCP-style behavior can be described
  by these contracts on paper
- no connector is migrated or registered at runtime
- no arbitrary workflow graph or MCP client is introduced

## P1D: Runs, Invocations, Artifacts, Approvals, And Audit

### Purpose

Define durable execution facts before changing the job runner or database.

### Contracts

- `WorkflowRun`
- `AgentRun`
- `ToolInvocation`
- `Artifact`
- `ApprovalRequest`
- `AuditEvent`
- closed enums for run, invocation, approval, and artifact status

`WorkflowRun` references the exact workflow version and initiating principal.
`AgentRun` references the exact agent version and records the resolved provider
and model used. This preserves auditability if a model profile later changes.

`ToolInvocation` records the plugin installation, capability, input and output
artifact references, timing, outcome, and approval reference. It does not store
decrypted credentials or unrestricted raw provider payloads.

`Artifact` stores metadata, provenance, sensitivity, retention class, and a
content reference. Large content and encrypted payload storage are persistence
concerns, not embedded contract fields.

`AuditEvent` is append-only and records actor, action, target, workspace,
request correlation, and a bounded metadata object. It is not an application
log record.

### Required Tests

- runs require exact definition version IDs
- completed runs require finish timestamps and valid terminal status
- failed records contain a bounded sanitized error summary
- agent runs capture resolved provider and model identifiers
- invocation records distinguish denied, approved, executed, and failed states
- artifacts require provenance and a retention class
- approval decisions require decision actor and decision time
- audit events are immutable and workspace-scoped when their target is
  workspace-scoped
- no contract defines a raw credential value field
- schema exports are stable

### Exit Criteria

- a future platform execution can be represented by these facts without adding
  runtime behavior
- no database migration or worker adapter is added
- P1 is complete only after P1A through P1D pass independently

## Legacy Incident Reference Map

This is a reference map for code we may reuse or replace later. It is not a
compatibility requirement for P1 and not a table-renaming plan.

| Current concept | Future platform concept | Migration note |
| --- | --- | --- |
| `tenant_id` | `org_id` | Legacy incident naming only; new contracts use `org_id`. |
| `installation_id` | deployment identity or legacy default selector | Never map it to `plugin_installation_id`. |
| bootstrap YAML | seeded product configuration | May inform local defaults until repositories and APIs exist. |
| Anthropic settings | `ModelProvider` plus `ModelProfile` | Credentials become an opaque auth reference. |
| `IncidentAgent` | agent runtime using an `AgentVersion` | Incident-specific prompts stay in the incident workflow. |
| tool catalog entry | `CapabilitySpec` | Existing dispatcher behavior remains unchanged through P1. |
| observability provider registration | plugin descriptor, installation, and capabilities | Adapter work belongs to the plugin registry phase. |
| GitHub client configuration | GitHub plugin installation | Repository allowlists remain enforced. |
| Slack client configuration | Slack plugin installation and delivery surface | Slack surface identity is distinct from product workspace identity. |
| incident | incident domain record linked to a `WorkflowRun` | An incident does not become a generic run. |
| investigation job | scheduler state for a `WorkflowRun` | The records may coexist; they are not necessarily one table. |
| investigation request | workflow input artifact | Reuse only if the future incident workflow needs it. |
| evidence item | typed context artifact with incident evidence semantics | Do not weaken evidence citation rules. |
| incident report | typed output artifact | Reuse only if the future incident workflow needs it. |
| tool invocation record | `ToolInvocation` | Gateway migration occurs after capability registration. |
| usage record | agent/model usage attached to `AgentRun` | Preserve provider and model resolution facts. |
| delivery outbox | delivery infrastructure for run artifacts | The outbox remains an infrastructure pattern. |
| Redis follow-up session | `ConversationThread` linked to incident artifacts | Conversation contracts are deferred until the surface phase. |

## Initial Implementation Proposal (Historical)

The following P1A-only proposal was written before implementation began. P1A
through P1D have since been added; its file layout and exclusions are historical.

Proposed commit title:

```text
contracts: add organization and workspace identity models
```

Expected files:

```text
contracts/v1/__init__.py
contracts/v1/base.py
contracts/v1/identity.py
tests/contract/test_product_identity_contracts.py
tests/architecture/test_product_contract_dependencies.py
scripts/export_product_schemas.py
schemas/product/v1/*.json
docs/platform-phase-1-contracts.md
```

Explicit exclusions:

- persistence protocols and database migrations
- API routes and authentication changes
- model and agent contracts from P1B
- plugin and workflow contracts from P1C
- run and artifact contracts from P1D
- runtime adapters
- legacy incident refactors

## Verification For Each Slice

Run verification in repository order:

```bash
pytest
ruff check .
ruff format --check .
mypy
```

PostgreSQL and Redis integration services are not required by P1 because P1
does not change persistence or worker behavior. Existing integration tests may
remain skipped when their test URLs are not configured, but all unit,
contract, architecture, golden, and benchmark tests must pass.

## Review Questions Before P1A Implementation

Only decisions that alter P1A should block its implementation:

1. Do we accept mandatory internal workspaces with a hidden default for small
   organizations?
2. Do we accept `org_id` as the new product term and treat `tenant_id` as
   legacy-only naming?
3. Do we accept separate `User` and `Principal` concepts so service accounts
   and external apps remain first-class actors?

Choices about the first new workflow, write approvals, retention defaults,
dashboard UX, MCP transport, deployment mode, and pricing do not block P1A.
