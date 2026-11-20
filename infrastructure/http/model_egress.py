"""Dedicated model transport with DNS validation at every new TCP connection."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

from clients.model_providers.egress_policy import ModelEgressPolicy, blocked
from infrastructure.config.settings import load_pool_settings


class ModelNetworkBackend(httpcore.SyncBackend):
    """Resolve once, reject mixed answers, then dial a validated numeric address."""

    def __init__(self, policy: ModelEgressPolicy) -> None:
        """Retain immutable process policy separately from tenant configuration."""
        self.policy = policy

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        """Keep original hostname in HTTPCore for TLS SNI and certificate checks."""
        if (
            not self.policy.permits_host(host)
            or port != 443
            or local_address is not None
        ):
            raise blocked()
        started = time.monotonic()
        try:
            answers = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        except OSError as exc:
            raise httpcore.ConnectError("Model destination resolution failed") from exc
        addresses = tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))
        if not 1 <= len(addresses) <= 64 or any(
            not self.policy.permits_address(host, ipaddress.ip_address(ip))
            for ip in addresses
        ):
            raise blocked()
        last_error: Exception | None = None
        for ip in addresses:
            remaining = (
                None if timeout is None else timeout - (time.monotonic() - started)
            )
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("Model connection timed out")
            try:
                stream = super().connect_tcp(
                    ip, port, remaining, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
                continue
            peer = stream.get_extra_info("server_addr")
            if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(ip):
                stream.close()
                raise blocked()
            return stream
        if isinstance(last_error, httpcore.ConnectTimeout):
            raise last_error
        raise httpcore.ConnectError(
            "Model destination connection failed"
        ) from last_error

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        """Unix sockets cannot be selected as model destinations."""
        raise blocked()


class ModelHTTPTransport(httpx.BaseTransport):
    """Use a guarded direct pool with no proxy, alternate mount or HTTP retry."""

    def __init__(self, policy: ModelEgressPolicy) -> None:
        """TLS verification stays enabled; operator CA configuration is honored."""
        self.policy = policy
        settings = load_pool_settings(process="api")
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive_connections,
            network_backend=ModelNetworkBackend(policy),
            retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Reject policy violations before socket creation or credential dispatch."""
        self.policy.validate_request(str(request.url))
        try:
            response = self.pool.handle_request(
                httpcore.Request(
                    request.method,
                    str(request.url),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions=request.extensions,
                )
            )
            try:
                return httpx.Response(
                    response.status,
                    headers=response.headers,
                    content=response.read(),
                    extensions=response.extensions,
                )
            finally:
                response.close()
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException(
                "Model transport timed out", request=request
            ) from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise httpx.TransportError(
                "Model transport failed", request=request
            ) from exc

    def close(self) -> None:
        """Close every model connection with its owning process runtime."""
        self.pool.close()


def open_model_http_client() -> httpx.Client:
    """Ignore proxy/netrc configuration and never follow credentialed redirects."""
    policy = ModelEgressPolicy.from_environment()
    return httpx.Client(
        transport=ModelHTTPTransport(policy), trust_env=False, follow_redirects=False
    )
