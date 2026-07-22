from __future__ import annotations

import json

import pytest

from src.veriquant.integration.finalize import atomic_write_json
from src.veriquant.integration import (
    IntegrationAcceptanceError,
    build_acceptance_result,
    redacted_for_workbench,
    safe_artifact_relative_path,
    write_acceptance_result,
)


def _write_ready_result(tmp_path, package_id: str, *, hooks: list[str] | None = None, security_notes: list[str] | None = None) -> None:
    package = tmp_path / ".autobuild" / "veriquant" / "parallel" / package_id
    package.mkdir(parents=True)
    owned_path = (
        "agent/src/api/veriquant_workbench_routes.py"
        if package_id == "workbench"
        else f"agent/src/veriquant/{package_id}/api.py"
    )
    owned = tmp_path / owned_path
    owned.parent.mkdir(parents=True)
    owned.write_text("# owned fixture\n", encoding="utf-8")
    (package / "result.json").write_text(
        json.dumps(
            {
                "agent_id": f"parallel_{package_id}",
                "status": "READY_FOR_INTEGRATION",
                "owned_files_changed": [owned_path],
                "tests_run": [
                    {
                        "command": f"python -m pytest tests/veriquant/parallel_{package_id}",
                        "result": "3 passed in 0.01s",
                    }
                ],
                "test_result": "PASSED",
                "exported_symbols": ["PublicSurface"],
                "integration_hooks": hooks or ["integration-owned public hook"],
                "security_notes": security_notes or ["sealed rankings are forbidden on research surfaces"],
            }
        ),
        encoding="utf-8",
    )


def test_absent_parallel_results_disable_all_capabilities_and_defer_prompt15(tmp_path) -> None:
    (tmp_path / ".autobuild" / "veriquant" / "parallel" / "statistical_promotion").mkdir(parents=True)

    result = build_acceptance_result(tmp_path)

    assert result["status"] == "FAILED"
    assert result["prompt_statuses"]["prompt_11"]["implemented"] == "UNAVAILABLE"
    assert result["prompt_statuses"]["prompt_12"]["implemented"] == "UNAVAILABLE"
    assert result["prompt_statuses"]["prompt_15"]["implemented"] == "DEFERRED"
    assert result["connections"]["Evidence Service -> Statistical Promotion"]["status"] == "DISABLED"
    assert "no capability package is eligible" in result["reason"]


def test_ready_llm_and_research_memory_packages_map_to_prompt13_and_prompt14(tmp_path) -> None:
    _write_ready_result(tmp_path, "llm_desks")
    _write_ready_result(tmp_path, "research_memory")

    result = build_acceptance_result(tmp_path)

    assert result["status"] == "IMPLEMENTED_NOT_RUN"
    assert result["prompt_statuses"]["prompt_13"]["package_id"] == "llm_desks"
    assert result["prompt_statuses"]["prompt_13"]["implemented"] == "IMPLEMENTED_NOT_RUN"
    assert result["prompt_statuses"]["prompt_14"]["package_id"] == "research_memory"
    assert result["prompt_statuses"]["prompt_14"]["implemented"] == "IMPLEMENTED_NOT_RUN"
    by_id = {item["package_id"]: item for item in result["capability_packages"]}
    assert by_id["llm_desks"]["agent_id"] == "parallel_llm_desks"
    assert by_id["llm_desks"]["eligible"] is True
    assert by_id["research_memory"]["eligible"] is True
    assert result["connections"]["Research DAG / FactorDAG -> Bounded LLM Desks"]["status"] == "READY_NOT_WIRED"


def test_all_real_package_ids_map_to_the_six_prompt_slots(tmp_path) -> None:
    for package_id in (
        "real_research",
        "statistical_promotion",
        "llm_desks",
        "research_memory",
        "shadow_portfolio",
        "workbench",
    ):
        _write_ready_result(tmp_path, package_id)

    result = build_acceptance_result(tmp_path)

    assert {result["prompt_statuses"][prompt]["package_id"] for prompt in (
        "prompt_11", "prompt_12", "prompt_13", "prompt_14", "prompt_17", "prompt_18"
    )} == {
        "real_research", "statistical_promotion", "llm_desks", "research_memory", "shadow_portfolio", "workbench"
    }
    assert all(item["eligible"] for item in result["capability_packages"])


def test_structured_test_result_hooks_and_declared_blockers_are_preserved(tmp_path) -> None:
    _write_ready_result(tmp_path, "workbench")
    path = tmp_path / ".autobuild" / "veriquant" / "parallel" / "workbench" / "result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["test_result"] = {"status": "PASSED_FOR_EXECUTABLE_PACKAGE_TESTS", "frontend": "NOT_RUN"}
    payload["integration_hooks"] = [{"name": "backend_router", "status": "AVAILABLE"}]
    payload["blockers"] = [{"scope": "frontend", "status": "NOT_RUN_ENV_BLOCKED"}]
    path.write_text(json.dumps(payload), encoding="utf-8")

    package = build_acceptance_result(tmp_path)["capability_packages"][0]

    assert package["eligible"] is True
    assert "backend_router" in package["integration_hooks"][0]
    assert "NOT_RUN_ENV_BLOCKED" in package["declared_blockers"][0]


def test_owned_path_scope_violation_fails_closed(tmp_path) -> None:
    _write_ready_result(tmp_path, "llm_desks")
    path = tmp_path / ".autobuild" / "veriquant" / "parallel" / "llm_desks" / "result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    forbidden = tmp_path / "agent" / "src" / "veriquant" / "contracts" / "changed.py"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("# forbidden fixture\n", encoding="utf-8")
    payload["owned_files_changed"].append("agent/src/veriquant/contracts/changed.py")
    path.write_text(json.dumps(payload), encoding="utf-8")

    package = build_acceptance_result(tmp_path)["capability_packages"][0]

    assert package["eligible"] is False
    assert any("OUTSIDE_OWNED_PATHS" in item for item in package["blockers"])


def test_ready_result_with_path_traversal_or_sealed_payload_is_not_eligible(tmp_path) -> None:
    package = tmp_path / ".autobuild" / "veriquant" / "parallel" / "statistical_promotion"
    package.mkdir(parents=True)
    (package / "result.json").write_text(
        json.dumps(
            {
                "agent_id": "statistical_promotion",
                "status": "READY_FOR_INTEGRATION",
                "owned_files_changed": ["agent/src/veriquant/statistical_promotion/contracts.py", "../escape.py"],
                "tests_run": ["python -m pytest agent/tests/veriquant/test_statistical_promotion.py"],
                "test_result": "12 passed",
                "exported_symbols": ["PromotionGate"],
                "integration_hooks": ["sealed_metric_path"],
                "security_notes": ["sealed metrics never exposed"],
            }
        ),
        encoding="utf-8",
    )

    result = build_acceptance_result(tmp_path)
    package_result = result["capability_packages"][0]

    assert package_result["eligible"] is False
    assert any("owned_files_changed" in item for item in package_result["blockers"])
    assert any("SEALED_REFERENCE" in item for item in package_result["blockers"])
    assert result["prompt_statuses"]["prompt_12"]["implemented"] == "UNAVAILABLE"


def test_sealed_safety_prose_does_not_block_ready_package(tmp_path) -> None:
    _write_ready_result(
        tmp_path,
        "statistical_promotion",
        hooks=["evidence reader bridge keeps sealed handle opaque"],
        security_notes=["raw sealed rows and sealed rankings are forbidden on research surfaces"],
    )

    result = build_acceptance_result(tmp_path)
    package_result = result["capability_packages"][0]

    assert package_result["eligible"] is True
    assert package_result["blockers"] == []
    assert result["prompt_statuses"]["prompt_12"]["implemented"] == "IMPLEMENTED_NOT_RUN"


def test_safe_artifact_path_rejects_traversal_absolute_escape_and_nul(tmp_path) -> None:
    assert safe_artifact_relative_path(tmp_path, "docs/veriquant/report.json") == "docs/veriquant/report.json"

    with pytest.raises(IntegrationAcceptanceError):
        safe_artifact_relative_path(tmp_path, "../outside.json")
    with pytest.raises(IntegrationAcceptanceError):
        safe_artifact_relative_path(tmp_path, str(tmp_path.parent / "outside.json"))
    with pytest.raises(IntegrationAcceptanceError):
        safe_artifact_relative_path(tmp_path, "docs/veriquant/bad\x00name.json")


def test_workbench_redaction_removes_sealed_and_secret_surfaces() -> None:
    redacted = redacted_for_workbench(
        {
            "visible": "ok",
            "sealed_metrics": {"rank": 1},
            "notes": ["Authorization: Bearer abcdefghijklmnop", "sealed_score_path=/tmp/x"],
        }
    )

    rendered = json.dumps(redacted, sort_keys=True)
    assert redacted["visible"] == "ok"
    assert redacted["sealed_metrics"] == "[REDACTED_SEALED]"
    assert "abcdefghijklmnop" not in rendered
    assert "sealed_score_path" not in rendered


def test_write_acceptance_result_is_parseable_and_content_hashed(tmp_path) -> None:
    target = tmp_path / ".autobuild" / "veriquant" / "integration" / "acceptance_result.json"
    result = write_acceptance_result(tmp_path, target)

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == result
    assert parsed["artifact_hash"].startswith("sha256:")
    assert not list(target.parent.glob("*.tmp"))

def test_final_state_writer_replaces_atomically_and_leaves_no_temporary_file(tmp_path) -> None:
    target = tmp_path / ".autobuild" / "veriquant" / "state.json"
    atomic_write_json(target, {"schema_version": "fixture/v1", "status": "FAILED"})
    first = json.loads(target.read_text(encoding="utf-8"))

    atomic_write_json(target, {"schema_version": "fixture/v1", "status": "IMPLEMENTED_AND_TESTED"})
    second = json.loads(target.read_text(encoding="utf-8"))

    assert first["status"] == "FAILED"
    assert second["status"] == "IMPLEMENTED_AND_TESTED"
    assert not list(target.parent.glob(".*.tmp"))
