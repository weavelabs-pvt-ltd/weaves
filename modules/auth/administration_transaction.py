"""Preserve service signatures while sharing the organization mutation boundary."""

from collections.abc import Callable
from functools import wraps
from typing import Concatenate, Protocol

from modules.auth.organization_guard import OrganizationGuard


class GuardedAdministration(Protocol):
    """Service port for the shared organization transaction boundary."""

    administration_guard: OrganizationGuard


def serialized_administration[S: GuardedAdministration, **P, T](
    method: Callable[Concatenate[S, P], T],
) -> Callable[Concatenate[S, P], T]:
    """Acquire before authorization and retain through every nested write/audit."""

    @wraps(method)
    def serialized(self: S, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with self.administration_guard.mutation():
            return method(self, *args, **kwargs)

    return serialized
