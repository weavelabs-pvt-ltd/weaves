"""Operator-controlled model destinations; tenant records cannot widen egress."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from contracts.v1 import ModelProviderType
from shared.errors import ModelGatewayError

HOSTED_BASE_URLS = {
    ModelProviderType.OPENAI: "https://api.openai.com/v1",
    ModelProviderType.ANTHROPIC: "https://api.anthropic.com",
    ModelProviderType.GEMINI: "https://generativelanguage.googleapis.com/v1beta",
}
type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_PRIVATE_RANGES = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
# Platform services must stay blocked even with an operator's ULA exception.
_PLATFORM_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("fd00:ec2::/32", "fd20:ce::254/128", "168.63.129.16/32")
)


def blocked() -> ModelGatewayError:
    """Never include submitted URLs, DNS answers or credentials in errors."""
    return ModelGatewayError(
        "model.egress_denied", "Model provider destination is not permitted"
    )


def normalize_base_url(value: str) -> str:
    """Require unambiguous HTTPS origins and plain, bounded base paths."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        invalid = (
            value != value.strip()
            or not value.isascii()
            or re.search(r"[\s\\%?#@]", value) is not None
            or parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or not host
            or host.endswith(".")
            or parsed.username is not None
            or not re.fullmatch(r"/[A-Za-z0-9._~:/-]*|", parsed.path)
            or "//" in parsed.path
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        )
        if invalid:
            raise blocked()
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            ipaddress.ip_address(host)
        elif not re.fullmatch(
            r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
            r"[a-zA-Z][a-zA-Z0-9-]{0,62}",
            host,
        ):
            raise blocked()
        authority = f"[{host.lower()}]" if ":" in host else host.lower()
        return "https://" + authority + parsed.path.rstrip("/")
    except (ValueError, TypeError) as exc:
        raise blocked() from exc


@dataclass(frozen=True)
class ModelEgressPolicy:
    """Custom endpoints and optional private networks are process configuration."""

    custom_base_urls: tuple[str, ...] = ()
    private_networks: dict[str, tuple[Network, ...]] = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> ModelEgressPolicy:
        """Fail startup on malformed or overly broad operator exceptions."""
        try:
            custom = json.loads(os.environ.get("WEAVES_MODEL_CUSTOM_BASE_URLS", "[]"))
            private = json.loads(os.environ.get("WEAVES_MODEL_PRIVATE_NETWORKS", "{}"))
            if not isinstance(custom, list) or not all(
                isinstance(x, str) for x in custom
            ):
                raise ValueError
            urls = tuple(normalize_base_url(value) for value in custom)
            allowed_hosts = {
                urlsplit(url).hostname for url in (*HOSTED_BASE_URLS.values(), *urls)
            }
            networks = _private_networks(private, allowed_hosts)
            return cls(urls, networks)
        except (ValueError, TypeError, ModelGatewayError) as exc:
            raise ValueError("Invalid operator model egress configuration") from exc

    def base_url(self, provider_type: ModelProviderType, value: str | None) -> str:
        """Hosted identities cannot be redirected through tenant-controlled URLs."""
        canonical = HOSTED_BASE_URLS.get(provider_type)
        result = normalize_base_url(value if value is not None else canonical or "")
        if canonical and result == canonical:
            return result
        if (
            provider_type is ModelProviderType.OPENAI_COMPATIBLE
            and result in self.custom_base_urls
        ):
            return result
        raise blocked()

    def permits_address(self, host: str, address: Address) -> bool:
        """Always reject local, metadata, mapped and transition addresses."""
        if (
            any(
                address.version == network.version and address in network
                for network in _PLATFORM_NETWORKS
            )
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or (
                isinstance(address, ipaddress.IPv6Address)
                and (
                    address.is_site_local
                    or address.ipv4_mapped is not None
                    or address.sixtofour is not None
                    or address.teredo is not None
                    or address in ipaddress.ip_network("64:ff9b::/96")
                    or address in ipaddress.ip_network("64:ff9b:1::/48")
                )
            )
        ):
            return False
        return address.is_global or any(
            address.version == network.version and address in network
            for network in self.private_networks.get(host, ())
        )

    def permits_host(self, host: str) -> bool:
        """The connection layer accepts only exact operator-approved hosts."""
        return host in {
            urlsplit(url).hostname
            for url in (*HOSTED_BASE_URLS.values(), *self.custom_base_urls)
        }

    def validate_request(self, url: str) -> None:
        """Constrain the dedicated transport even when called without an adapter."""
        normalized = normalize_base_url(url)
        if not any(
            normalized.startswith(base + "/")
            for base in (*HOSTED_BASE_URLS.values(), *self.custom_base_urls)
        ):
            raise blocked()


def _private_networks(
    value: object, hosts: set[str | None]
) -> dict[str, tuple[Network, ...]]:
    """Only exact approved hosts may opt into explicit RFC1918/ULA subnets."""
    if not isinstance(value, dict):
        raise ValueError
    result = {}
    for host, cidrs in value.items():
        if (
            host not in hosts
            or not isinstance(cidrs, list)
            or not all(isinstance(x, str) for x in cidrs)
        ):
            raise ValueError
        networks = tuple(ipaddress.ip_network(cidr, strict=True) for cidr in cidrs)
        if any(
            not any(
                n.version == parent.version
                and n.prefixlen >= parent.prefixlen
                and n.network_address in parent
                for parent in _PRIVATE_RANGES
            )
            for n in networks
        ):
            raise ValueError
        result[host] = networks
    return result
