# Weaves Backend Code Quality Standards

## Purpose

These standards apply to every Python file in the Weaves backend, including application code, adapters, scripts, tests, and generated Python files. The goal is readable, reviewable code with clear module boundaries and failures caught in CI.

The file and function limits below are Weaves team rules, not universal industry limits. They should be enforced consistently and not bypassed by splitting code into meaningless wrappers.

## Size limits

- **Python file:** maximum 500 physical lines, including blank lines, comments, and docstrings. Applies to source, scripts, tests, and generated files; there are no file-size exemptions.
- **Production function or method:** aim for 60 lines or fewer; hard maximum 80 physical lines from the `def` line through the final body line. Blank lines, comments, and docstrings count. If it exceeds 80, split it around named responsibilities or use cases.
- **Test function:** maximum 80 physical lines, with a target of 60 lines. Split longer scenarios into named setup, action, and assertion helpers while keeping the original test coverage intact. No test function has a size exemption.
- **Class:** one cohesive responsibility. If a class grows past roughly 300 lines or accumulates unrelated methods, review whether it should be separated. This is a review threshold, not an automatic rejection.
- Do not evade these limits with nested functions, one-line delegators, compressed formatting, or moving a large function unchanged into another file.

Implement file and function limits with `scripts/check_python_size.py` and run it in CI. The checker has no grandfathered size baseline or per-file/per-function exemptions: every Python file is checked against 500 lines, and every production or test function is checked against 80 lines. Avoid broad test-directory exclusions. Ruff is the main linter and formatter, but it does not provide a general custom-rule mechanism for these physical line-count limits.

## Formatting and linting

- Use **Ruff** for linting, import ordering, and formatting. Pin its version in the development dependency set.
- Use a formatter width of **88 characters** and four-space indentation. Run `ruff format` locally and `ruff format --check` in CI.
- Enable Ruff's core correctness and hygiene rules: `E`, `F`, and `I`; add `UP`, `B`, `SIM`, and `RUF` for modernization, common bug patterns, simplifications, and Ruff-specific checks.
- Enable Ruff's `D` docstring checks using one documented convention (Google style for this project). Exclude only the rules that conflict with the selected formatter/convention; every exception should be configured centrally, not hidden with inline suppressions.
- Consider Ruff's `S` security rules for unsafe patterns, configuring deliberate exceptions for tests and security-reviewed code.
- Set McCabe cyclomatic complexity to **10 or lower** for a function. Refactor high-branching code or document an exceptional case in review.
- Avoid broad `noqa`. Any suppression must name the rule and explain why it is safe. Remove stale suppressions.

Recommended Ruff configuration shape (keep lint exceptions centralized; size limits have no exemptions):

```toml
[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF", "D", "C90"]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.format]
docstring-code-format = true
```

When using Ruff's formatter, do not enable conflicting style rules such as `D203`, `D206`, `D300`, or `COM812`; follow Ruff's formatter guidance and keep any required ignores in `pyproject.toml`. Tests are not a public API, so docstring rules are not applied under `tests/`. The checked-in Ruff version is pinned in `requirements-dev.txt`.

`pyproject.toml` lists the files that already violate docstring or complexity rules. The 88-character line limit applies to every file. That list is existing debt for the other rules: a new file is checked against the full rule set, and an entry should be removed once its file is clean. Do not add a file to the list to hide a violation introduced by new work.

## Types and API boundaries

- Add complete parameter and return annotations to all public functions, methods, ports, and use cases.
- Run **mypy in strict mode** over application, module, client, infrastructure, and app code. Keep the configured target Python version aligned with runtime/deployment.
- Prefer specific types, enums, dataclasses, protocols, and validated contracts over unstructured `dict[str, Any]` values.
- Keep `Any`, `cast`, `type: ignore`, and `# noqa` at integration boundaries where needed. Each non-obvious escape hatch must explain its reason and be as narrow as possible.
- Validate untrusted HTTP, webhook, connector, and model-provider data at the boundary. Do not pass provider-specific response dictionaries into domain logic.
- Keep external contracts, internal domain objects, and persistence records separate when their responsibilities differ.

## Docstrings and comments

- Use PEP 257-compatible docstrings with the project's configured Google-style sections.
- Add docstrings to public modules, classes, functions, methods, ports, and non-obvious use cases. Private helpers need docstrings when their name and implementation do not make the contract obvious.
- Start with a concise summary. Add `Args`, `Returns`, `Raises`, or examples only when they clarify non-obvious behavior, constraints, or failure cases.
- Explain intent, invariants, security assumptions, and surprising tradeoffs. Do not repeat the function signature or narrate each line of code.
- Comments should explain why a decision is needed, not restate what the code does. Update or remove comments when behavior changes.

## Design and maintainability

- Keep functions focused on one operation and classes focused on one responsibility.
- Prefer explicit dependencies passed through constructors or function arguments. Avoid hidden global clients, service locators, and import-time I/O.
- Keep HTTP route handlers thin: parse/validate, call a use case, map the result. Keep SQL in persistence adapters and provider-specific HTTP behavior in provider clients.
- Preserve dependency direction: HTTP/apps → module application → domain/ports; infrastructure implements ports. Enforce this with architecture checks.
- Avoid circular imports, wildcard imports, mutable default arguments, unexplained magic values, and broad `except Exception` handlers.
- Catch the narrowest useful exception. Never expose credentials, access tokens, full authorization headers, or sensitive provider payloads in logs or API errors.
- Use parameterized SQL. Every outbound network request must have explicit timeouts. Retry only bounded, retryable operations; writes require idempotency or a clear reason retries are safe.
- Prefer simple control flow and early returns. Keep nesting shallow; extract a named helper when a block has a meaningful independent purpose.

## Configuration, HTTP clients, and database resources

- Parse environment configuration once into typed settings; do not scatter `os.environ` reads through business modules.
- Create process-scoped HTTP transports with explicit connection limits, keep-alive limits, and timeout settings. Inject them into separate provider clients and close them with the app/worker lifecycle.
- Create one database connection pool per process, configure API, worker, and CLI pool sizes explicitly, and close each pool during shutdown.
- Repositories borrow pooled connections. Use a clear unit-of-work boundary for multi-record transactions.
- Do not create clients, pools, database connections, or perform migrations as import-time side effects.

## CI quality gates

Every pull request should pass these checks before merge/deploy:

1. `ruff check` for lint rules.
2. `ruff format --check` for formatting.
3. The repository file/function-size checker.
4. `mypy --strict` for the configured backend packages.
5. Architecture-boundary checks for imports and dependency direction.
6. The relevant contract and integration test suites.

Run the same commands locally before requesting review. CI is authoritative; local editor settings should use the checked-in configuration.

## Official references

- [PEP 8 — Style Guide for Python Code](https://peps.python.org/pep-0008/)
- [PEP 257 — Docstring Conventions](https://peps.python.org/pep-0257/)
- [Ruff rules](https://docs.astral.sh/ruff/rules/)
- [Ruff formatter](https://docs.astral.sh/ruff/formatter/)
- [mypy strict mode guidance](https://mypy.readthedocs.io/en/stable/existing_code.html)
