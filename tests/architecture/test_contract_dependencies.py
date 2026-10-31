import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2]
CONTRACT_ROOT = PACKAGE_ROOT / "contracts"
SHARED_ROOT = PACKAGE_ROOT / "shared"

OUTER_LAYERS = {
    "api",
    "application",
    "bootstrap",
    "infrastructure",
    "modules",
    "product",
    "workers",
    "worker",
    "persistence",
    "runtime",
}
CONTRACT_FORBIDDEN = OUTER_LAYERS | {
    "fastapi",
    "starlette",
    "httpx",
    "psycopg",
    "sqlite3",
}
SHARED_FORBIDDEN = CONTRACT_FORBIDDEN | {"contracts"}
INFRASTRUCTURE_FORBIDDEN = {
    "api",
    "bootstrap",
    "modules",
    "product",
    "workers",
    "fastapi",
    "starlette",
}
APPLICATION_FORBIDDEN = {
    "api",
    "bootstrap",
    "workers",
    "fastapi",
    "starlette",
}


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def _violations(root: Path, forbidden: set[str]) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in root.rglob("*.py"):
        for module in _imported_modules(path):
            if any(
                module == item or module.startswith(item + ".") for item in forbidden
            ):
                found.append((path, module))
    return found


def test_product_contracts_do_not_depend_on_runtime_or_infrastructure():
    violations = _violations(CONTRACT_ROOT, CONTRACT_FORBIDDEN)
    assert not violations, f"Product contracts cross a forbidden boundary: {violations}"


def test_shared_primitives_stay_free_of_outer_layers():
    violations = _violations(SHARED_ROOT, SHARED_FORBIDDEN)
    assert not violations, f"Shared primitives cross a forbidden boundary: {violations}"


def test_infrastructure_does_not_depend_on_application_or_http():
    violations = _violations(PACKAGE_ROOT / "infrastructure", INFRASTRUCTURE_FORBIDDEN)
    assert not violations, (
        f"Infrastructure imports a product or HTTP layer: {violations}"
    )
    outside_ports = [
        (path, module)
        for path in (PACKAGE_ROOT / "infrastructure").rglob("*.py")
        for module in _imported_modules(path)
        if module == "application"
        or (
            module.startswith("application.")
            and not module.startswith("application.ports.")
        )
    ]
    assert not outside_ports, (
        f"Infrastructure imports outside application ports: {outside_ports}"
    )


def test_http_and_use_cases_do_not_import_infrastructure_directly():
    roots = (
        PACKAGE_ROOT / "modules",
        PACKAGE_ROOT / "application",
    )
    violations: list[tuple[Path, str]] = []
    for root in roots:
        for path in root.rglob("*.py"):
            for module in _imported_modules(path):
                if module == "infrastructure" or module.startswith("infrastructure."):
                    violations.append((path, module))
    assert not violations, (
        f"HTTP or use-case modules import infrastructure: {violations}"
    )


def test_application_modules_do_not_import_http_or_process_entrypoints():
    violations: list[tuple[Path, str]] = []
    for root in (PACKAGE_ROOT / "application", PACKAGE_ROOT / "modules"):
        for path in root.rglob("*.py"):
            if "http" in path.parts:
                continue
            for module in _imported_modules(path):
                if any(
                    module == item or module.startswith(item + ".")
                    for item in APPLICATION_FORBIDDEN
                ):
                    violations.append((path, module))
    assert not violations, (
        f"Application code imports HTTP or an entry point: {violations}"
    )
