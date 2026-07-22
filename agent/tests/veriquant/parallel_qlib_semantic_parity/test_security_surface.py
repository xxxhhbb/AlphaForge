from __future__ import annotations

import ast
import json
from pathlib import Path

from src.veriquant.qlib_semantic_parity.api import EXPECTED_DISPOSITIONS, SEMANTIC_POLICY_HASH


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "src" / "veriquant" / "qlib_semantic_parity"


def test_parity_package_has_no_dynamic_execution_network_or_qlib_import() -> None:
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    forbidden_roots = {"qlib", "requests", "httpx", "socket", "subprocess"}
    for path in SOURCE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden_roots for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_roots


def test_parity_package_does_not_offer_custom_operator_registration() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))
    assert "custom_ops" not in text
    assert "register_operator" not in text
    assert "get_expression_instance" not in text


def test_measured_smoke_artifact_is_complete_and_matches_frozen_policy() -> None:
    artifact_path = ROOT.parent / ".autobuild" / "veriquant" / "parallel" / "qlib_semantic_parity" / "real_smoke_result.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["status"] == "PASSED"
    assert payload["pyqlib_version"] == "0.9.7"
    assert payload["semantic_policy_hash"] == SEMANTIC_POLICY_HASH
    assert payload["case_count"] == 22 and payload["replay_stable"] is True
    assert {
        name: evidence["status"] for name, evidence in payload["operator_evidence"].items()
    } == {name: status.value for name, status in EXPECTED_DISPOSITIONS.items()}
    assert payload["native_string_resolver_used"] is False
    assert payload["custom_operator_registered"] is False
    assert payload["network_used"] is False
