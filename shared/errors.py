"""Application-facing errors shared by use cases and adapters."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a conflicting or failed request."""


class IdempotencyKeyAlreadyExists(ValueError):
    """A persistence uniqueness constraint rejected a duplicate run key."""


class ExecutionCancelled(RuntimeError):
    """A worker observed a cancellation request at a safe boundary."""


class ModelGatewayError(RuntimeError):
    """Safe model failure details suitable for a run error summary."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


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


class RateLimitExceeded(RuntimeError):
    """Raised when a public operation exceeds its source-address budget."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("this operation is temporarily limited")
        self.retry_after_seconds = retry_after_seconds
