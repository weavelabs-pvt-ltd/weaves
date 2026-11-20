"""DNS and actual transport boundaries reject unsafe dispatch, including proxies."""

import ipaddress
import socket

import httpcore
import pytest

from clients.model_providers.egress_policy import ModelEgressPolicy
from infrastructure.http.model_egress import ModelNetworkBackend, open_model_http_client
from shared.errors import ModelGatewayError
from tests.support.runtime import LocalPlatformRuntime


def answers(*addresses):
    return [
        (
            socket.AF_INET6 if ":" in ip else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, 443),
        )
        for ip in addresses
    ]


@pytest.mark.parametrize(
    "ips",
    [
        [],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["1.1.1.1", "10.0.0.1"],
        ["2606:4700:4700::1111", "::1"],
        ["::ffff:169.254.169.254"],
    ],
)
def test_bad_or_mixed_dns_answers_create_no_connection(monkeypatch, ips):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers(*ips))
    monkeypatch.setattr(
        httpcore.SyncBackend,
        "connect_tcp",
        lambda *a, **k: pytest.fail("Unsafe DNS caused a socket connection"),
    )
    with pytest.raises(ModelGatewayError):
        ModelNetworkBackend(ModelEgressPolicy()).connect_tcp(
            "api.anthropic.com", 443, 1
        )


def test_dns_answers_are_pinned_at_dial_and_rechecked_for_new_connections(monkeypatch):
    resolutions = []

    def resolve(*args, **kwargs):
        resolutions.append(args[0])
        return answers("1.1.1.1" if len(resolutions) == 1 else "169.254.169.254")

    dialed = []

    class Stream:
        def get_extra_info(self, name):
            assert name == "server_addr"
            return ("1.1.1.1", 443)

    def dial(self, host, port, timeout, **kwargs):
        dialed.append(host)
        return Stream()

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", dial)
    backend = ModelNetworkBackend(ModelEgressPolicy())
    backend.connect_tcp("api.anthropic.com", 443, 1)
    with pytest.raises(ModelGatewayError):
        backend.connect_tcp("api.anthropic.com", 443, 1)
    assert dialed == ["1.1.1.1"]
    assert resolutions == ["api.anthropic.com"] * 2


def test_unexpected_peer_is_closed_before_tls_or_headers(monkeypatch):
    closed = []

    class Stream:
        def get_extra_info(self, name):
            return ("127.0.0.1", 443)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers("1.1.1.1"))
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *a, **k: Stream())
    with pytest.raises(ModelGatewayError):
        ModelNetworkBackend(ModelEgressPolicy()).connect_tcp(
            "api.anthropic.com", 443, 1
        )
    assert closed == [True]


def test_process_model_client_does_not_use_environment_proxies_or_netrc(
    monkeypatch, tmp_path
):
    for name in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        monkeypatch.setenv(name, "http://127.0.0.1:8888")
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.anthropic.com login unwanted password secret")
    monkeypatch.setenv("NETRC", str(netrc))
    calls = []

    def connect(self, host, port, *args, **kwargs):
        calls.append((host, port))
        raise ModelGatewayError("model.egress_denied", "blocked fixture")

    monkeypatch.setattr(ModelNetworkBackend, "connect_tcp", connect)
    with open_model_http_client() as client:
        assert client._auth is None
        assert not client._trust_env
        assert not client.follow_redirects
        assert not any(client._mounts.values())
        with pytest.raises(ModelGatewayError):
            client.get("https://api.anthropic.com/v1/models")
    assert calls == [("api.anthropic.com", 443)]


def test_runtime_owns_separate_model_client_and_closes_it():
    runtime = LocalPlatformRuntime()
    model_client = runtime.model_adapters[
        next(kind for kind in runtime.model_adapters if kind.value == "anthropic")
    ]._client
    assert model_client is not runtime.http_client
    runtime.close()
    assert model_client.is_closed


def test_operator_allowed_private_host_still_checks_every_dns_answer(monkeypatch):
    policy = ModelEgressPolicy(
        private_networks={"api.anthropic.com": (ipaddress.ip_network("172.18.0.0/24"),)}
    )
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: answers("172.18.0.8", "169.254.169.254")
    )
    with pytest.raises(ModelGatewayError):
        ModelNetworkBackend(policy).connect_tcp("api.anthropic.com", 443, 1)


def test_composition_failure_closes_both_process_clients(monkeypatch):
    import httpx

    shared = httpx.Client()
    model = open_model_http_client()
    monkeypatch.setattr("apps.runtime_wiring.open_model_http_client", lambda: model)

    def fail(_runtime):
        raise RuntimeError("fixture composition failure")

    monkeypatch.setattr("apps.runtime_wiring.configure_capability_services", fail)
    with pytest.raises(RuntimeError, match="fixture composition failure"):
        LocalPlatformRuntime(http_client=shared)
    assert shared.is_closed
    assert model.is_closed
