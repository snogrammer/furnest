"""furnest.domain stays pure: it must not import anything else from furnest."""

import ast
from pathlib import Path

import furnest.domain

DOMAIN_DIR = Path(furnest.domain.__file__).parent


def _imported_modules(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def test_domain_imports_nothing_else_from_furnest() -> None:
    files = sorted(DOMAIN_DIR.rglob("*.py"))
    assert files
    offenders = [
        f"{path.name}: {module}"
        for path in files
        for module in _imported_modules(path)
        if module.split(".")[0] == "furnest" and not module.startswith("furnest.domain")
    ]
    assert offenders == []
