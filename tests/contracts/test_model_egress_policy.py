"""Model endpoint validation is enforced for new and historical records."""

import ipaddress
import json

import httpx
import pytest

from clients.model_providers.egress_policy import (
    HOSTED_BASE_URLS,
    ModelEgressPolicy,
    normalize_base_url,
)
from clients.model_providers.gateway import (
    AnthropicAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
)
from contracts.v1 import ModelProviderType
from shared.errors import ModelGatewayError
from tests.contracts.test_model_gateway import binding, request


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://api.openai.com:8443/v1",
        "https://api.openai.com.evil.test/v1",
        "https://api.openai.com./v1",
        "https://api.openai.com/v1?redirect=1",
        "https://api.openai.com/v1#x",
        "https://api.openai.com/v1/../other",
        "https://api.openai.com//v1",
        "https://user:secret@api.openai.com/v1",
        "https://api.openai.com%2e/v1",
        "https://127.1/v1",
        "https://2130706433/v1",
        "https://0x7f000001/v1",
        "https://api.openai.com\\@evil.test/v1",
        "https://api.openai.com/\nv1",
    ],
)
def test_hosted_urls_cannot_select_alternate_destinations(url):
    with pytest.raises(ModelGatewayError) as error:
        ModelEgressPolicy().base_url(ModelProviderType.OPENAI, url)
    assert error.value.code == "model.egress_denied"
    assert url not in str(error.value)


@pytest.mark.parametrize("kind,url", HOSTED_BASE_URLS.items())
def test_hosted_defaults_and_canonical_urls_succeed(kind, url):
    policy = ModelEgressPolicy()
    assert policy.base_url(kind, None) == url
    assert policy.base_url(kind, url + "/") == url


def test_custom_urls_require_exact_operator_allowlist(monkeypatch):
    url = "https://models.example/v1"
    with pytest.raises(ModelGatewayError):
        ModelEgressPolicy().base_url(ModelProviderType.OPENAI_COMPATIBLE, url)
    monkeypatch.setenv("WEAVES_MODEL_CUSTOM_BASE_URLS", json.dumps([url]))
    policy = ModelEgressPolicy.from_environment()
    assert policy.base_url(ModelProviderType.OPENAI_COMPATIBLE, url) == url
    with pytest.raises(ModelGatewayError):
        policy.base_url(ModelProviderType.OPENAI_COMPATIBLE, url + "/elsewhere")
    with pytest.raises(ModelGatewayError):
        policy.base_url(ModelProviderType.ANTHROPIC, url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "168.63.129.16",
        "0.0.0.0",
        "100.100.100.200",
        "224.0.0.1",
        "192.0.2.1",
        "::",
        "::1",
        "fc00::1",
        "fec0::1",
        "fd20:ce::254",
        "fd00:ec2::254",
        "fd00:ec2::123",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    ],
)
def test_nonpublic_and_embedded_addresses_are_denied(address):
    assert not ModelEgressPolicy().permits_address(
        "api.anthropic.com", ipaddress.ip_address(address)
    )


def test_operator_private_networks_are_host_scoped_and_cannot_include_metadata(
    monkeypatch,
):
    monkeypatch.setenv(
        "WEAVES_MODEL_PRIVATE_NETWORKS",
        json.dumps({"api.anthropic.com": ["172.18.0.0/24"]}),
    )
    policy = ModelEgressPolicy.from_environment()
    ip = ipaddress.ip_address("172.18.0.8")
    assert policy.permits_address("api.anthropic.com", ip)
    assert not policy.permits_address("api.openai.com", ip)
    assert not policy.permits_address(
        "api.anthropic.com", ipaddress.ip_address("172.19.0.8")
    )
    for invalid in [
        {"api.anthropic.com": ["0.0.0.0/0"]},
        {"api.anthropic.com": ["169.254.0.0/16"]},
        {"evil.test": ["10.0.0.0/8"]},
    ]:
        monkeypatch.setenv("WEAVES_MODEL_PRIVATE_NETWORKS", json.dumps(invalid))
        with pytest.raises(ValueError, match="operator model egress"):
            ModelEgressPolicy.from_environment()


@pytest.mark.parametrize(
    "adapter_type,kind",
    [
        (OpenAICompatibleAdapter, ModelProviderType.OPENAI),
        (AnthropicAdapter, ModelProviderType.ANTHROPIC),
        (GeminiAdapter, ModelProviderType.GEMINI),
    ],
)
def test_every_adapter_rechecks_legacy_url_before_resolving_secret(adapter_type, kind):
    class NoSecret:
        def resolve(self, _ref):
            pytest.fail("A denied URL must not resolve credentials")

    def no_network(_request):
        pytest.fail("A denied URL must send no HTTP request")

    record = binding()
    record.provider.provider_type = kind
    record.provider.base_url = "https://169.254.169.254/v1"
    with httpx.Client(transport=httpx.MockTransport(no_network)) as client:
        adapter = adapter_type(NoSecret(), client)
        for operation in [
            lambda: adapter.list_models(record.provider),
            lambda: adapter.complete(request(), record),
        ]:
            with pytest.raises(ModelGatewayError) as error:
                operation()
            assert error.value.code == "model.egress_denied"


@pytest.mark.parametrize(
    "adapter_type,kind",
    [
        (OpenAICompatibleAdapter, ModelProviderType.OPENAI),
        (AnthropicAdapter, ModelProviderType.ANTHROPIC),
        (GeminiAdapter, ModelProviderType.GEMINI),
    ],
)
def test_every_adapter_refuses_redirects_even_with_redirecting_client(
    adapter_type, kind
):
    calls = []

    class Secret:
        def resolve(self, _ref):
            return "synthetic-key"

    def redirect(req):
        calls.append(str(req.url))
        return httpx.Response(
            307, headers={"location": "https://169.254.169.254/latest"}
        )

    record = binding()
    record.provider.provider_type = kind
    record.provider.base_url = HOSTED_BASE_URLS[kind]
    with httpx.Client(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    ) as client:
        adapter = adapter_type(Secret(), client)
        for operation in [
            lambda: adapter.list_models(record.provider),
            lambda: adapter.complete(request(), record),
        ]:
            with pytest.raises(ModelGatewayError) as error:
                operation()
            assert error.value.code == "model.redirect_blocked"
    assert len(calls) == 2
    assert all("169.254" not in value for value in calls)


def test_unambiguous_ipv6_url_can_be_operator_selected():
    assert (
        normalize_base_url("https://[2606:4700:4700::1111]:443/v1/")
        == "https://[2606:4700:4700::1111]/v1"
    )


def test_private_model_exception_cannot_enable_ipv6_platform_services():
    policy = ModelEgressPolicy(
        private_networks={
            "api.anthropic.com": (ipaddress.ip_network("fc00::/7"),),
        }
    )
    assert policy.permits_address("api.anthropic.com", ipaddress.ip_address("fc12::1"))
    for address in ["fd20:ce::254", "fd00:ec2::254", "fd00:ec2::123"]:
        assert not policy.permits_address(
            "api.anthropic.com", ipaddress.ip_address(address)
        )


@pytest.mark.parametrize(
    "adapter_type,kind,header",
    [
        (OpenAICompatibleAdapter, ModelProviderType.OPENAI, "authorization"),
        (AnthropicAdapter, ModelProviderType.ANTHROPIC, "x-api-key"),
        (GeminiAdapter, ModelProviderType.GEMINI, "x-goog-api-key"),
    ],
)
def test_each_canonical_adapter_preserves_discovery_and_completion(
    adapter_type, kind, header
):
    class Secret:
        def resolve(self, _ref):
            return "fixture-only"

    sent = []

    def respond(req):
        sent.append(req)
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "model-a"}],
                    "models": [
                        {
                            "name": "models/model-a",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "valid"}}],
                "content": [{"type": "text", "text": "valid"}],
                "candidates": [{"content": {"parts": [{"text": "valid"}]}}],
            },
        )

    record = binding()
    record.provider.provider_type = kind
    record.provider.base_url = HOSTED_BASE_URLS[kind]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = adapter_type(Secret(), client)
        assert adapter.list_models(record.provider)[0]["id"] == "model-a"
        assert adapter.complete(request(), record).content == "valid"
    assert len(sent) == 2
    assert all("fixture-only" in req.headers[header] for req in sent)
    assert all(str(req.url).startswith(HOSTED_BASE_URLS[kind] + "/") for req in sent)
