"""Refuse to run the disaster-recovery drill anywhere but a local throwaway stack.

The drill destroys and recreates a database. It must never be pointed at the
production VM, its project, its hostnames or a remote Docker daemon, so every
check here is a refusal: the function returns the reasons and the caller stops.
Only variable *names* are reported, never values, because values may be secrets.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

STACK_NAME = re.compile(r"dr-drill-[0-9a-f]{8}")
PRODUCTION_MARKERS = (
    "weaves-510819",
    "weaves-personal",
    "weaves-single",
    "weave-studio.in",
    "8.231.88.4",
    "/opt/weaves",
)
# Variables that can steer a tool at a database, cloud project or remote daemon.
TARGET_VARIABLES = (
    "DATABASE_URL",
    "WEAVES_DATABASE_URL",
    "WEAVES_TEST_DATABASE_URL",
    "PGHOST",
    "PGDATABASE",
    "CLOUDSDK_CORE_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "COMPOSE_PROJECT_NAME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
)
PRODUCTION_REGISTRY = "weaves-510819/weaves/"
PRODUCTION_ENV_FILE = "/opt/weaves/singlevm/runtime.env"
LOCAL_ENDPOINTS = ("unix://", "npipe://")


def refusals(
    environ: Mapping[str, str],
    hostname: str,
    docker_endpoint: str,
    path_exists: Callable[[str], bool],
    stack_name: str | None = None,
    image: str | None = None,
) -> list[str]:
    """Return every reason this machine or target is not a safe drill site."""
    problems: list[str] = []
    for name in TARGET_VARIABLES:
        value = environ.get(name, "")
        if any(marker in value for marker in PRODUCTION_MARKERS):
            problems.append(f"environment variable {name} names a production target")
    if environ.get("DATABASE_URL") or environ.get("WEAVES_DATABASE_URL"):
        problems.append(
            "a database URL is set in the environment; unset it (the drill creates "
            "its own throwaway database and never reads one)"
        )
    host = environ.get("DOCKER_HOST", "")
    if host and not host.startswith(LOCAL_ENDPOINTS):
        problems.append("DOCKER_HOST points at a remote Docker daemon")
    if not docker_endpoint.startswith(LOCAL_ENDPOINTS):
        problems.append("the active Docker context is not a local daemon")
    if any(marker in hostname.lower() for marker in PRODUCTION_MARKERS):
        problems.append("this machine looks like the production VM")
    if path_exists(PRODUCTION_ENV_FILE):
        problems.append(f"{PRODUCTION_ENV_FILE} exists: this is the production host")
    if stack_name is not None and not STACK_NAME.fullmatch(stack_name):
        problems.append("the stack name must match dr-drill-<8 hex>")
    if image is not None and PRODUCTION_REGISTRY in image:
        problems.append("the image is from the production registry repository")
    return problems
