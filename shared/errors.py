"""Application-facing errors shared by use cases and adapters."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a conflicting or failed request."""


class IdempotencyKeyAlreadyExists(ValueError):
    """A persistence uniqueness constraint rejected a duplicate run key."""


class ExecutionCancelled(RuntimeError):
    """A worker observed a cancellation request at a safe boundary."""


class OwnershipConflict(ValueError):
    """A write tried to change the organization or workspace that owns a record."""


class TenantContextRequired(RuntimeError):
    """Tenant-scoped work ran without a validated organization and workspace."""

    code = "tenant.context_required"
    retryable = False


class TenantInactive(PermissionError):
    """New work was refused because its organization or workspace is not active."""

    code = "tenant.inactive"
    retryable = False


class ModelGatewayError(RuntimeError):
    """Safe model failure details suitable for a run error summary."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        usage: tuple[int, int] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        # (input, output) tokens the provider reported even though the
        # response was unusable; ``None`` when no count is known.
        self.usage = usage
        # Upstream HTTP status when the provider itself refused the request.
        self.http_status = http_status


class PluginGatewayError(RuntimeError):
    """Safe dispatch errors that never expose connector payloads or credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SecretStoreUnavailable(RuntimeError):
    """Raised when durable secret encryption is not configured."""


class LoginThrottled(RuntimeError):
    """Raised when an account or source address exceeds the login budget."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("login attempts are temporarily limited")
        self.retry_after_seconds = retry_after_seconds


class StaleRevision(Exception):
    """An edit named a revision that no longer matches the stored record."""
