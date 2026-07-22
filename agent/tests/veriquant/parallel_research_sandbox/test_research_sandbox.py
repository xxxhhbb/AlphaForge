from __future__ import annotations

from dataclasses import replace

import pytest

from veriquant.contracts import ContractValidationError
from veriquant.research_os import AnswerLevel
from veriquant.research_sandbox import (
    FakeAuxiliaryResearchSandboxRunner,
    ResearchSandboxProtocol,
    ResearchSandboxRunRequest,
    ResearchSandboxRunStatus,
    SandboxResourceBudget,
)


H = "sha256:" + "a" * 64
P = "sha256:" + "b" * 64


def protocol(**overrides):
    base = {
        "protocol_id": "sandbox_protocol_1",
        "campaign_hash": H,
        "group_id": "market_structure",
        "purpose": "auxiliary microstructure idea exploration",
        "input_hashes": (P,),
        "budget": SandboxResourceBudget(),
    }
    base.update(overrides)
    return ResearchSandboxProtocol(**base)


def request(**overrides):
    base = {
        "run_id": "sandbox_run_1",
        "protocol": protocol(),
        "prompt_hash": P,
        "research_payload": {
            "hypothesis": "large momentum may proxy delayed investor attention",
            "fields": ("close", "volume"),
        },
        "requested_outputs": ("auxiliary_summary",),
    }
    base.update(overrides)
    return ResearchSandboxRunRequest(**base)


def test_fake_runner_outputs_auxiliary_only_and_stable_artifacts(tmp_path) -> None:
    runner = FakeAuxiliaryResearchSandboxRunner(artifact_root=tmp_path)
    first = runner.run(request())
    second = runner.run(request())

    assert first.status is ResearchSandboxRunStatus.COMPLETED_AUXILIARY
    assert first.output_hash == second.output_hash
    assert first.answer_section is not None
    assert first.answer_section.level is AnswerLevel.AUXILIARY_RESEARCH
    assert first.auxiliary_evidence is not None
    assert first.auxiliary_evidence.evidence_class.value == "AUXILIARY_RESEARCH_EVIDENCE"
    assert first.limitations
    assert (tmp_path / "sandbox_run_1" / "sandbox_result.json").exists()
    assert not list((tmp_path / "sandbox_run_1").glob("*.py"))


def test_protocol_rejects_network_secret_broker_and_trusted_path_flags() -> None:
    for field in ("allow_network", "allow_secret_access", "allow_broker_access", "trusted_path_enabled"):
        with pytest.raises(ContractValidationError):
            protocol(**{field: True})


def test_payload_rejects_downstream_authority_and_sealed_fields() -> None:
    for payload in (
        {"official_metrics": {"rank_ic": 0.1}},
        {"promotion": "passed"},
        {"target_weights": {"000001.SZ": 1.0}},
        {"sealed": {"rank": 1}},
    ):
        with pytest.raises(ContractValidationError):
            request(research_payload=payload)


def test_path_traversal_and_absolute_paths_fail_closed() -> None:
    with pytest.raises(ContractValidationError):
        request(run_id="runs/../secret")
    with pytest.raises(ContractValidationError):
        request(research_payload={"note": "read C:\\secret\\token.txt"})


def test_executable_payloads_fail_closed() -> None:
    bad_payloads = (
        {"code": "eval" + "(\"1+1\")"},
        {"code": "exec" + "(\"print(1)\")"},
        {"code": "sub" + "process.run(['cmd" + ".exe'])"},
        {"code": "write factor" + ".py and run it"},
        {"code": "import os"},
    )
    for payload in bad_payloads:
        with pytest.raises(ContractValidationError):
            request(research_payload=payload)


def test_harmless_generated_code_is_text_only_not_trusted(tmp_path) -> None:
    result = FakeAuxiliaryResearchSandboxRunner(artifact_root=tmp_path).run(
        request(generated_code_text="def alpha(close):\n    return close\n")
    )
    assert result.status is ResearchSandboxRunStatus.COMPLETED_AUXILIARY
    summary = (tmp_path / "sandbox_run_1" / "sandbox_summary.json").read_text(encoding="utf-8")
    assert "TEXT_ONLY_NOT_EXECUTED" in summary


def test_tiny_resource_budget_blocks_without_auxiliary_claim(tmp_path) -> None:
    small = replace(protocol().budget, max_output_bytes=10)
    blocked_request = request(protocol=protocol(budget=small))
    result = FakeAuxiliaryResearchSandboxRunner(artifact_root=tmp_path).run(blocked_request)
    assert result.status is ResearchSandboxRunStatus.RESOURCE_BUDGET_EXCEEDED
    assert result.auxiliary_evidence is None
    assert result.failure_code is not None


def test_timeout_budget_blocks_without_running_provider(tmp_path) -> None:
    tiny = replace(protocol().budget, max_runtime_ms=1)
    result = FakeAuxiliaryResearchSandboxRunner(artifact_root=tmp_path).run(request(protocol=protocol(budget=tiny)))
    assert result.status is ResearchSandboxRunStatus.TIMEOUT
    assert result.auxiliary_evidence is None


def test_secret_like_payload_is_not_echoed() -> None:
    with pytest.raises(ContractValidationError) as caught:
        request(research_payload={"api_key": "should-not-appear"})
    rendered = str(caught.value) + str(caught.value.details)
    assert "should-not-appear" not in rendered
