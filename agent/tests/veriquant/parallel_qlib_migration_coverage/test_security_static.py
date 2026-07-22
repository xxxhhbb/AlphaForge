from __future__ import annotations

import ast
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "veriquant"
    / "qlib_migration_coverage"
)


def test_scanner_has_no_dynamic_execution_or_legacy_factor_imports() -> None:
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    forbidden_import_fragments = {"factors.zoo", "importlib", "runpy"}
    findings: list[str] = []

    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden_calls
            ):
                findings.append(f"{path.name}:{node.lineno}:{node.func.id}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(
                    fragment in name
                    for name in names
                    for fragment in forbidden_import_fragments
                ):
                    findings.append(f"{path.name}:{node.lineno}:legacy-import")

    assert findings == []
