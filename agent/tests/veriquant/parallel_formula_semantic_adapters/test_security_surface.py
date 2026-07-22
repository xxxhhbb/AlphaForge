from __future__ import annotations

import ast
import json
from pathlib import Path

from src.veriquant.formula_semantic_adapters import ADAPTER_POLICY_HASH, BUILTIN_ADAPTER_POLICIES


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "src" / "veriquant" / "formula_semantic_adapters"


def test_package_has_no_generated_code_dynamic_loading_network_or_process_surface() -> None:
    prohibited_calls = {"eval", "exec", "compile", "__import__"}
    prohibited_roots = {"qlib", "importlib", "requests", "httpx", "socket", "subprocess"}
    for path in SOURCE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in prohibited_calls
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in prohibited_roots for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in prohibited_roots


def test_package_exposes_no_registration_resolver_or_callback_contract() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))
    for forbidden in ("register_operator", "custom_operator", "expression_resolver", "dynamic_import", "python_callback"):
        assert forbidden not in source.lower()


def test_policy_manifest_matches_runtime_catalog() -> None:
    manifest_path = (
        ROOT.parent
        / ".autobuild"
        / "veriquant"
        / "parallel"
        / "formula_semantic_adapters"
        / "policy_manifest.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["adapter_policy_hash"] == ADAPTER_POLICY_HASH
    assert set(payload["adapters"]) == set(BUILTIN_ADAPTER_POLICIES)
    for name, policy in BUILTIN_ADAPTER_POLICIES.items():
        assert payload["adapters"][name]["policy_hash"] == policy.policy_hash
        assert payload["adapters"][name]["adapter_hash"] == policy.adapter_hash
