# Weaves Backend Structure Guide

Follow [Backend Code Quality Standards](backend-code-quality-standards.md) for Python file/function limits, linting, typing, docstrings, and CI quality gates.

## Goal

Organize Weaves as a layered modular monolith: one backend repository and deployable product, with clear capability boundaries, separate runnable apps, provider-specific clients, and managed shared infrastructure.

The frontend remains in its separate repository. This repository should not use a `backend/` wrapper or a `src/` directory. Preserve existing API behavior and `/api/v0` routes while reorganizing code. Do not introduce microservices, a new queue platform, or a database redesign as part of the structure work.

## Target repository layout

```text
weaves/
├── apps/                            # Runnable processes / composition roots
│   ├── api/
│   │   ├── app.py                   # FastAPI app and lifespan
│   │   ├── router.py                # Mount module HTTP routes under /api/v0
│   │   └── wiring.py                # Construct API dependencies
│   ├── worker/
│   │   ├── main.py                  # Worker process entry point
│   │   └── wiring.py
│   └── cli/
│       └── main.py                  # Optional local CLI
│
├── modules/                         # Product capabilities, each with its layers
│   ├── auth/                        # Weaves users, sessions, roles, credentials
│   │   ├── domain/
│   │   ├── application/
│   │   ├── ports.py
│   │   └── http/
│   ├── agents/
│   │   ├── domain/
│   │   ├── application/
│   │   ├── ports.py
│   │   └── http/
│   ├── executions/
│   ├── workflows/
│   ├── integrations/
│   ├── models/
│   ├── governance/
│   ├── knowledge/
│   ├── memory/
│   └── evaluations/
│
├── application/
│   └── ports/                       # Cross-capability application contracts
│
├── clients/                         # Outbound clients, one per provider/service
│   ├── identity_provider/           # Only for an external identity service
│   ├── model_providers/
│   │   ├── openai.py
│   │   ├── anthropic.py
│   │   └── provider_b.py
│   └── integrations/
│       ├── github.py
│       ├── jira.py
│       ├── slack.py
│       └── mcp.py
│
├── infrastructure/
│   ├── http/
│   │   ├── pool.py                  # Process-scoped HTTP transport/client factory
│   │   └── settings.py              # Limits, timeouts, keep-alive configuration
│   ├── database/
│   │   ├── pool.py                  # Process-scoped PostgreSQL connection pool
│   │   ├── unit_of_work.py          # Transaction boundary
│   │   └── migrations/
│   ├── persistence/
│   │   ├── postgres/                # Repository implementations
│   │   └── memory/                  # In-memory development/test implementations
│   ├── queue/
│   ├── secrets/
│   └── config/                      # Typed settings and environment parsing
│
├── contracts/v1/                    # Stable external product contracts
├── shared/                          # Cross-cutting types such as IDs and errors
├── utils/                           # Small, pure, generic helpers only
├── tests/
│   ├── architecture/
│   ├── contracts/
│   └── integration/
├── docs/
├── infra/
├── schemas/
├── scripts/
├── .github/
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── compose.yaml
└── README.md
```

Create detailed subpackages as needed. Avoid empty boilerplate layers: each module should have only the domain, application, port, and HTTP files it actually needs.

## What each boundary means

- **Apps** are runnable processes and composition roots: currently API, worker, and optionally CLI. They are not separate deployments by default.
- **Modules** own product capabilities and business rules. A module's application use cases coordinate its domain and ports.
- **Clients** make outbound calls to external services. Each client owns that provider's URLs, auth headers, request/response mapping, and safe error conversion.
- **Infrastructure** provides shared technical resources and implements ports: database pools, repository adapters, HTTP transports, queues, and secret storage.
- **Application ports** define adapter-neutral contracts used across capabilities. Capability-specific rules remain in the owning module.
- **Contracts** represent stable serialized contracts at the product boundary. Keep them distinct from internal domain entities and database records.
- **Shared** is for cross-cutting types used by multiple modules. **Utils** is for small pure helpers; neither should become a dumping ground for product behavior.

Weaves' authentication use cases belong in `modules/auth`. A client under `clients/identity_provider` is only for calls to an external identity provider. Internal modules should call application use cases directly, never call another module through its HTTP API.

## Dependency direction

```text
Apps (API / worker)
        │
        ▼
Module HTTP adapters ──> Module application use cases ──> Module domain
                                      │
                                      ▼
                                Module ports
                                      ▲
                                      │ implements
Infrastructure adapters ─────────────┘
   ├── database/repositories
   ├── HTTP/provider clients
   ├── queue
   └── secrets
```

- Domain code must not import FastAPI, database drivers, provider SDKs, or infrastructure implementations.
- Application code depends on domain types and ports, not concrete infrastructure.
- HTTP handlers parse/validate input, call use cases, and map results/errors to responses. They do not query repositories or implement business workflows.
- Workers call the same application use cases as the API; they do not duplicate business rules.
- Bootstrap/wiring constructs adapters and injects them. Avoid service locators and global mutable clients.
- Avoid circular imports between modules. Cross-module use should go through a narrow application-facing port or use case.

## HTTP clients and pooling

- Create process-scoped HTTP clients/transports with explicit maximum connections, keep-alive limits, connect/read/write timeouts, and retry policy where safe.
- Inject the managed transport into separate provider clients. Do not create an `httpx.Client`, `httpx.AsyncClient`, or connection pool per request or per tool invocation.
- Each API, worker, and CLI process creates and closes its own HTTP resources in its lifecycle. Python pool objects are not shared across processes.
- Importing `apps.api.app` must not open database pools or provider HTTP clients; open them during app startup.
- Use a synchronous or asynchronous client consistently with the calling code. Do not call blocking clients from async handlers without an explicit thread strategy.
- Give a provider a separate transport only when it needs distinct TLS, proxy, timeout, or isolation settings; document the reason.

## Database pooling and transactions

- Create one configured PostgreSQL pool per process. Repositories borrow connections from the pool; repository constructors do not create pools.
- API, worker, and CLI processes have separate pools. Size each pool for its process concurrency and keep their total connection budget within the database limit.
- Open pools at process startup and close them during shutdown. Keep connection configuration in typed settings.
- Use a unit-of-work/transaction boundary for operations that update multiple records. Keep transaction and locking behavior explicit.
- Preserve existing migrations and durable execution semantics, including idempotency, job claiming, leases/heartbeats, cancellation, stale-job recovery, retries, audit, and schedule dispatch.
- Run migrations from a controlled deployment/startup path rather than racing independently in every worker.

## Capability ownership

- **Auth:** users, principals, sessions, invitations, roles, and credentials.
- **Agents:** definitions, versions, templates, budgets, and capability allowlists.
- **Executions:** run/job lifecycle, retries, cancellation, progress, and artifacts.
- **Workflows:** definitions, versions, steps, triggers, schedules, and orchestration.
- **Integrations:** installation/configuration lifecycle and capability policy; provider API calls go through clients.
- **Models:** provider-neutral profiles, model selection, and usage policy; provider protocols go through clients.
- **Governance:** authorization policies, approvals, and audit events.
- **Knowledge and memory:** context retrieval and conversation memory policy/lifecycle.
- **Evaluations:** suites, evaluation runs, scoring, and reports.

Keep these modules in the same modular monolith. Extract a separately deployed service only when independent scaling, reliability, or ownership makes the operational cost worthwhile and the boundary has an explicit API/event contract.

## Migration and compatibility

1. Inventory current API/worker entry points, routes, contracts, clients, pools, persistence adapters, and tests.
2. Keep Python packages at the repository root, next to `tests/` and `pyproject.toml`. Do not add `backend/`, `src/`, or a second `weaves/` package directory.
3. Establish API and worker app lifecycles and explicit dependency wiring.
4. Introduce managed HTTP and database pool lifecycles and inject resources into clients/repositories.
5. Extract use cases from `LocalPlatformRuntime` into capability modules; preserve behavior while migrating one capability at a time.
6. Move provider-specific calls into their dedicated clients and put interfaces in the consuming module's ports.
7. Update Docker, Compose, CI, deployment scripts, and package discovery to use root-level paths.
8. Remove compatibility facades only after all call sites are migrated.

Do not change `/api/v0` routes, request/response shapes, authorization scope, or persistence semantics as part of a file move. Document any unavoidable compatibility changes separately.

## Current migration state

The repository has root-level `apps`, `modules`, `clients`, `infrastructure`,
`contracts`, `shared`, and `tests` packages. Capability services consume
repository, secret-store, and login-throttling ports; API and worker composition
roots construct the pooled clients, database store, repositories, and adapters.
The API surface has been split into bounded modules, and the HTTP method/path
inventory matches the pre-restructure API inventory.

Auth, model, agent, evaluation, workflow, integration, knowledge, execution,
and governance HTTP routes receive capability services directly. OAuth state
management is an integration service, and OAuth handlers receive the shared
HTTP client, secret store, GitHub App token provider, and connection service as
explicit dependencies. Workflow definitions, workflow automation, workflow
execution, integrations, identity administration, and execution job operations
have dedicated services. The worker uses an injected execution worker service.
API, worker, and CLI composition roots open process-specific resources through
`apps/runtime_wiring.py` and use
capability-focused builders in `apps/runtime_services_*.py` to construct and
inject module services. `LocalPlatformRuntime` binds shared process resources,
repositories, and legacy runtime delegates; it no longer constructs capability
services. Model and agent default selection live in their owning modules. Built-in
identity, model, knowledge, integration, and agent records are seeded or repaired
by their owning modules' bootstrap services and invoked through the application
bootstrap lifecycle. API startup constructs an `ApiRouteServices` bundle, and the
router consumes that explicit bundle without importing the compatibility runtime.
The API app receives `ApiProcessServices` for routes, auth, bootstrap, and
shutdown. Worker startup similarly consumes a `WorkerServices` bundle, and the
CLI receives only its agent run command and cleanup callback. The runtime
implementation is in `apps/platform_runtime.py`; `application/platform.py` is a
temporary import shim for older callers. API handlers do not receive or read the
process runtime; app lifespan owns startup and shutdown directly.
Authentication middleware receives identity use cases through an explicit
request-scoped service bundle. Worker recovery uses injected
`ExecutionRecoveryService` and `ActionRecoveryService` instances.
`LocalPlatformRuntime` remains a compatibility façade with focused identity,
auth/execution, workflow, bootstrap, foundation, and recovery delegates. API and
worker entrypoints no longer use its service registry directly. Capability
builders consume the typed `RuntimeCompositionInputs` protocol, which describes
the repositories, adapters, configuration, and lifecycle callbacks needed during
composition. `apps/runtime_wiring.py` attaches the resulting `CapabilityServiceSet`
to the façade for legacy callers. The façade and `application/platform.py` import
shim can be removed after those callers migrate to explicit service bundles.

`scripts/check_python_size.py` enforces a 500-line file limit and an 80-line
function limit across all Python files, including tests. Ruff, Ruff formatting,
and strict mypy are configured in `pyproject.toml`. Ruff still has a centrally
tracked set of docstring, complexity, and other legacy-debt exceptions; those
exceptions should be reduced as modules are cleaned up.

## Completion criteria

- API, worker, and CLI have small, explicit composition roots and clean resource shutdown.
- There is no platform-wide service object that owns all capability behavior or constructs every adapter.
- Routes call use cases; application modules do not access concrete repositories or clients directly.
- Model and integration clients are separate, provider-specific, and use managed pooled HTTP transports.
- Each process uses a managed database pool, and transaction boundaries are explicit.
- Domain modules are independent of HTTP, database drivers, and provider-specific code.
- Existing auth, workspace scoping, OAuth, secrets, approvals, audit, idempotency, recovery, scheduling, and public API behavior is preserved.
- Architecture checks enforce dependency direction and fail if modules bypass their ports.
- Root-level Docker, Compose, CI, and deployment commands point to the new package paths.
- No unrelated product features or infrastructure migrations are included.
