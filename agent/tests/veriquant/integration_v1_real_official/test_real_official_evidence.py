from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from veriquant.contracts import ContractValidationError, EvidenceClass, FailureCode
from veriquant.evidence import TrialLedger, TrialOutcome, TrialStatus
from veriquant.integration.v1_real_official import (
    APPROVED_QUALIFICATION_HASH,
    FactorAttemptStatus,
    PinnedRealOfficialInputPaths,
    RealOfficialEvidenceAdapter,
)
from veriquant.integration.v1_real_official import cli
from veriquant.integration.v1_real_official.service import FactorCandidate


def test_pinned_real_proxy_runs_visible_official_evidence_and_consumes_budget(tmp_path: Path) -> None:
    result = RealOfficialEvidenceAdapter().run(output_root=tmp_path / "run")

    assert result.status == "COMPLETED_OFFICIAL_VISIBLE"
    assert result.source_class == "TUSHARE_COMPATIBLE_PROXY"
    assert result.provider_class == "APPROVED_PROXY_DATA_VENDOR"
    assert result.official_tushare_source is False
    assert result.qualification_hash == APPROVED_QUALIFICATION_HASH
    assert result.budget_consumed == 2
    assert result.family_budget == 2
    assert result.official_evidence_run is True
    assert result.sealed_evidence_run is False
    assert result.promotion_run is False
    assert result.challenger_created is False
    assert all(item.status is FactorAttemptStatus.OFFICIAL_VISIBLE_EVIDENCE for item in result.factors)
    assert all(item.train_evidence.evidence_class is EvidenceClass.OFFICIAL_EVIDENCE for item in result.factors)
    assert all(item.validation_evidence.evidence_class is EvidenceClass.OFFICIAL_EVIDENCE for item in result.factors)
    assert all(item.validation_evidence.metrics.observation_count >= 20 for item in result.factors)

    replay = TrialLedger(tmp_path / "run/trial_ledger.sqlite3").replay()
    family = next(iter(replay.families.values()))
    assert family.consumed == 2
    assert family.terminal_trials == 2
    assert all(item.status is TrialStatus.TERMINAL for item in replay.trials.values())
    assert all(item.outcome is TrialOutcome.SUCCESS for item in replay.trials.values())


def test_same_frozen_inputs_seed_and_config_reproduce_official_run_hash(tmp_path: Path) -> None:
    first = RealOfficialEvidenceAdapter().run(output_root=tmp_path / "first")
    second = RealOfficialEvidenceAdapter().run(output_root=tmp_path / "second")

    assert first.artifact_hash == second.artifact_hash
    assert first.trial_ledger_tip_hash == second.trial_ledger_tip_hash
    assert [item.train_evidence.artifact_hash for item in first.factors] == [
        item.train_evidence.artifact_hash for item in second.factors
    ]
    assert [item.validation_evidence.artifact_hash for item in first.factors] == [
        item.validation_evidence.artifact_hash for item in second.factors
    ]


def test_parse_failure_is_terminal_and_consumes_family_budget(tmp_path: Path) -> None:
    bad = FactorCandidate(
        factor_id="forbidden-python",
        payload={
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "forbidden-python",
            "nodes": [
                {"id": "close", "op": "field", "field": "close"},
                {"id": "bad", "op": "python_callable", "inputs": ["close"]},
            ],
            "output_node": "bad",
        },
    )

    result = RealOfficialEvidenceAdapter().run(
        output_root=tmp_path / "failed",
        factors=(bad,),
        family_budget=1,
    )

    assert result.status == "FAILED_CONSUMED"
    assert result.budget_consumed == 1
    assert result.official_evidence_run is False
    factor = result.factors[0]
    assert factor.status is FactorAttemptStatus.FAILED_CONSUMED
    assert factor.failure_code in {FailureCode.INVALID_FACTOR_DAG, FailureCode.INVALID_VALUE}
    replay = TrialLedger(tmp_path / "failed/trial_ledger.sqlite3").replay()
    trial = next(iter(replay.trials.values()))
    assert trial.status is TrialStatus.TERMINAL
    assert trial.outcome is TrialOutcome.PARSE_REJECTION


def test_changed_qualification_fails_before_evidence_authority(tmp_path: Path) -> None:
    paths = PinnedRealOfficialInputPaths.default()
    payload = json.loads(paths.qualification.read_text(encoding="utf-8"))
    payload["official_tushare_source"] = True
    tampered = tmp_path / "qualification.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    adapter = RealOfficialEvidenceAdapter(paths=replace(paths, qualification=tampered))

    with pytest.raises(ContractValidationError) as caught:
        adapter.run(output_root=tmp_path / "never-started")

    assert caught.value.code in {FailureCode.AUXILIARY_MASQUERADE, FailureCode.INVALID_HASH}
    assert not (tmp_path / "never-started/trial_ledger.sqlite3").exists()


def test_cli_writes_machine_readable_result_without_promotion(tmp_path: Path, monkeypatch) -> None:
    allowed = tmp_path / ".autobuild/veriquant/parallel/ui_agent_c"
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)

    assert cli.main(["--output-root", str(allowed / "run"), "--seed", "7"]) == 0
    payload = json.loads((allowed / "run/official_evidence_run.json").read_text(encoding="utf-8"))
    assert payload["official_evidence_run"] is True
    assert payload["sealed_evidence_run"] is False
    assert payload["promotion_run"] is False
    assert payload["challenger_created"] is False
    assert payload["official_tushare_source"] is False


def test_cli_rejects_output_path_outside_owned_artifact_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    assert cli.main(["--output-root", str(tmp_path / "outside")]) == 2


def test_owned_source_has_no_arbitrary_execution_or_forbidden_smoke_shortcuts() -> None:
    source_root = Path(__file__).resolve().parents[3] / "src/veriquant/integration/v1_real_official"
    text = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py"))
    assert "real_factor_smoke" not in text
    assert "sealed_passed=True" not in text
    assert "p_value=0.01" not in text
    assert "factor.py" not in text
    assert "eval(" not in text
    assert "exec(" not in text
    assert "subprocess" not in text
    assert "promotion_service" not in text.lower()

