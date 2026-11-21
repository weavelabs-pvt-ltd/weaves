# ER diagrams

Logical relationships between collections of `product_records` (plus `product_secrets`
where a reference points at it). None of these is a database foreign key; they are
reference fields that the application resolves (see [../DATA_MODEL.md](../DATA_MODEL.md)
section 1.6). Entity names are collection names. The label on each line is the
referencing field. Cardinality notation: `||` exactly one, `o|` zero or one,
`o{` zero or many, `|{` one or many. The parent (left side) owns the lifetime of the
child, except where noted; deletion is by `PURGE_ORDER`, not by cascade.

Relationships come from the reference-like fields listed per collection in
[generated.md](generated.md).

## Overview

Every tenant collection also carries `org_id` to `organizations` (and workspace
collections `workspace_id` to `workspaces`); those edges are omitted from the domain
diagrams and drawn here once by domain.

```mermaid
erDiagram
    accounts ||--o{ memberships : "account_id"
    organizations ||--o{ memberships : "org_id"
    organizations ||--o{ workspaces : "org_id"
    organizations ||--|| organization_settings : "org_id"
    organizations ||--o| organization_lifecycle : "id"
    organizations ||--o{ users : "org_id"
    organizations ||--o{ audit_events : "org_id"
    workspaces ||--o{ agents : "workspace_id"
    workspaces ||--o{ workflows : "workspace_id"
    workspaces ||--o{ tasks : "workspace_id"
    workspaces ||--o{ execution_jobs : "workspace_id"
    workspaces ||--o{ plugin_installations : "workspace_id"
    workflows ||--o{ workflow_runs : "workflow_id"
    agents ||--o{ agent_runs : "agent_id"
    workflow_runs ||--o{ agent_runs : "run_id"
    tasks ||--o{ task_turns : "task_id"
    organizations ||--o{ model_providers : "org_id"
    model_providers ||--o{ model_profiles : "provider_id"
    organizations ||--o{ skill_definitions : "org_id"
    plugins ||--o{ plugin_installations : "plugin_id"
    organizations ||--o{ approval_policies : "org_id"
```

## Identity and accounts

`accounts` is global. `memberships` is the only collection that carries both an account
and a tenant. `account_organization_requests` is a ledger and survives organization deletion.

```mermaid
erDiagram
    accounts ||--o{ account_identities : "account_id"
    accounts ||--o{ account_sessions : "account_id"
    accounts ||--o{ account_organization_requests : "account_id"
    accounts ||--o{ memberships : "account_id"
    organizations ||--o{ memberships : "org_id"
    users ||--o| memberships : "user_id"
    principals ||--o| memberships : "principal_id"
    organizations ||--o{ account_organization_requests : "organization_id"
    account_oauth_states {
        string state_digest "unique, single use"
    }
    accounts {
        string id PK
        string status
    }
    account_identities {
        string account_id FK
        string kind
        string issuer
        string subject
    }
    memberships {
        string org_id
        string account_id FK
        string user_id FK
        string principal_id FK
    }
```

`account_oauth_states` has no reference: it holds a Google sign-in attempt before any
account is known.

## Tenancy, access control and lifecycle

```mermaid
erDiagram
    organizations ||--|| organization_settings : "id equals org_id"
    organizations ||--o| organization_lifecycle : "id equals org_id"
    organizations ||--|{ workspaces : "org_id"
    organizations ||--o{ users : "org_id"
    organizations ||--o{ principals : "org_id"
    organizations ||--o{ roles : "org_id"
    users ||--o| principals : "user_id"
    principals ||--o{ api_credentials : "principal_id"
    principals ||--o{ role_bindings : "principal_id"
    roles ||--o{ role_bindings : "role_id"
    workspaces |o--o{ role_bindings : "workspace_id optional"
    principals ||--o| organization_settings : "owner_principal_id"
    organizations ||--o{ audit_events : "org_id"
    principals ||--o{ audit_events : "actor_principal_id"
```

## Agents, workflows, tasks and runs

```mermaid
erDiagram
    agents ||--o{ agent_versions : "agent_id"
    agents |o--o| agent_versions : "current_version_id"
    workflows ||--o{ workflow_versions : "workflow_id"
    workflows |o--o| workflow_versions : "current_version_id"
    workflow_versions }o--o{ agent_versions : "agent_version_ids"
    agent_versions }o--o| approval_policies : "approval_policy_id"
    agent_versions }o--o| model_profiles : "model_profile_id"
    workflows ||--o{ workflow_triggers : "workflow_id"
    workflows ||--o{ workflow_schedules : "workflow_id"
    workflows ||--o{ workflow_runs : "workflow_id"
    workflow_versions ||--o{ workflow_runs : "workflow_version_id"
    workflow_runs ||--o{ agent_runs : "run_id"
    agent_versions ||--o{ agent_runs : "agent_version_id"
    agent_runs ||--o{ tool_invocations : "agent_run_id"
    workflow_runs ||--o{ artifacts : "run_id"
    agents ||--o{ conversation_threads : "agent_id"
    agents ||--o{ tasks : "agent_id"
    conversation_threads ||--o| tasks : "thread_id"
    tasks ||--o{ task_turns : "task_id"
    tasks ||--o{ task_messages : "task_id"
    task_turns ||--o| task_messages : "user_message_id"
    task_turns ||--o| task_messages : "assistant_message_id"
    task_turns }o--o| agent_runs : "agent_run_id"
    workflows ||--o{ canvas_runs : "workflow_id"
    workflows ||--|| canvas_settings : "workflow_id is the record id"
```

## Models and providers

```mermaid
erDiagram
    organizations ||--o{ model_providers : "org_id"
    organizations ||--o| model_configurations : "record id is org_id"
    model_providers ||--o{ model_profiles : "provider_id"
    model_profiles |o--o| model_profiles : "fallback_model_profile_id"
    model_configurations }o--o| model_providers : "default_provider_id"
    model_configurations }o--o| model_profiles : "default_model_profile_id"
    model_providers }o--o| product_secrets : "auth_ref"
    agent_versions }o--o| model_profiles : "model_profile_id"
    agent_runs }o--o| model_profiles : "model_profile_id"
    product_secrets {
        string secret_ref PK
        string org_id
        string purpose
        string owner_ref
    }
```

## Integrations and plugins

`plugins` rows are either global built-ins (`github`, `slack`, `jira`, no `org_id`) or
owned by one organization. After migration 20 their ownership is the row in
`product_plugin_ownership` (a real foreign key to `product_records`).

```mermaid
erDiagram
    plugins ||--o{ plugin_installations : "plugin_id"
    plugins ||--o| product_plugin_ownership : "plugin_id (database FK)"
    workspaces ||--o{ plugin_installations : "workspace_id"
    plugin_installations }o--o| product_secrets : "auth_ref"
    plugin_installations ||--o{ tool_invocations : "plugin_installation_id"
    workspaces ||--o{ oauth_authorization_states : "workspace_id"
    principals ||--o{ oauth_authorization_states : "principal_id"
    oauth_authorization_states }o--o| product_secrets : "secret_ref"
    product_plugin_ownership {
        string plugin_id PK
        string state "builtin owned quarantined"
        string org_id
        string reason
    }
```

## Skills

```mermaid
erDiagram
    skill_definitions ||--o{ skill_versions : "skill_id"
    skill_definitions |o--o| skill_versions : "current_version_id"
    skill_versions ||--o| skill_publications : "skill_version_id"
    skill_versions ||--o| skill_revocations : "skill_version_id"
    principals ||--o{ skill_definitions : "owner_principal_id"
    principals ||--o{ skill_versions : "author_principal_id"
    principals ||--o{ skill_publications : "publisher_principal_id"
    principals ||--o{ skill_revocations : "revoked_by_principal_id"
    tasks }o--o{ skill_versions : "private_skill_snapshots"
```

## Governance, approvals and audit

```mermaid
erDiagram
    approval_policies ||--o{ agent_versions : "approval_policy_id"
    tool_invocations |o--o| approval_requests : "approval_request_id"
    workflow_runs ||--o{ approval_requests : "run_id"
    agent_runs ||--o{ approval_requests : "requested_by_agent_run_id"
    principals ||--o{ approval_requests : "resolved_by_principal_id"
    principals ||--o{ audit_events : "actor_principal_id"
    organizations ||--o{ audit_events : "org_id"
    audit_events }o--o| workflow_runs : "target_id (free-form, any collection)"
```

## Execution queue and evaluations

```mermaid
erDiagram
    execution_jobs }o--o| workflows : "workflow_id"
    execution_jobs }o--o| agents : "agent_id"
    execution_jobs }o--o| conversation_threads : "thread_id"
    execution_jobs }o--o| workflow_triggers : "trigger_id"
    execution_jobs }o--o| workflow_schedules : "schedule_id"
    execution_jobs }o--o| execution_jobs : "retry_of_job_id"
    execution_jobs |o--o| workflow_runs : "run_id"
    execution_jobs |o--o| evaluation_executions : "evaluation_id"
    evaluation_suites ||--o{ evaluation_executions : "suite_id"
    evaluation_suites ||--o{ evaluation_reports : "suite_id"
    evaluation_executions |o--o| evaluation_reports : "evaluation_id"
    evaluation_executions }o--o| execution_jobs : "execution_job_id"
    task_turns {
        string status "queued running succeeded failed cancelled"
        string worker_id
        int attempts
    }
    execution_jobs {
        string status "queued running cancel_requested succeeded failed cancelled"
        string worker_id
        int attempts
    }
```

`task_turns` is the second durable queue (claimed the same way as `execution_jobs`) and
is related to `tasks` in the tasks diagram.
