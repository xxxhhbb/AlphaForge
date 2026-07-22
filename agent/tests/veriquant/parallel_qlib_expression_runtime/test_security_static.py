from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[3] / "src" / "veriquant" / "qlib_expression_runtime"


def test_trusted_runtime_has_no_dynamic_execution_or_network_imports() -> None:
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    forbidden_imports = {"socket", "urllib", "requests", "httpx"}
    findings: list[str] = []

    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(name.split(".")[0] in forbidden_imports for name in names):
                    findings.append(f"{path.name}:{node.lineno}:network-import")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                findings.append(f"{path.name}:{node.lineno}:{node.func.id}")

    assert findings == []


def test_optional_probe_is_metadata_only_and_never_initializes_qlib() -> None:
    from veriquant.qlib_expression_runtime.optional_pyqlib import _PROBE_SCRIPT

    lowered = _PROBE_SCRIPT.lower()
    assert "qlib.init" not in lowered
    assert "provider_uri" not in lowered
    assert "features(" not in lowered
    assert "download" not in lowered

