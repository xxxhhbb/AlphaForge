from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from veriquant.agent_research_sandbox import (
    DatasetAssessmentRow,
    DatasetAssessmentTable,
    DiagnosticRow,
    DiagnosticTable,
    EventRow,
    EventTable,
    ExperimentCodeIdentity,
    ExperimentDataIdentity,
    ExperimentReviewGate,
    ExperimentReviewPolicy,
    ExperimentReviewRequest,
    MechanicalReviewStatus,
    RegressionRow,
    RegressionTable,
    RegimeComparisonRow,
    RegimeComparisonTable,
    ReviewFailureCode,
    ReviewerSuggestion,
    SandboxAmendmentIdentity,
    ScoreRow,
    ScoreTable,
)
from veriquant.contracts import ContractValidationError


NOW = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)


def h(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_hash(source: str) -> str:
    return h(source)


def amendment() -> SandboxAmendmentIdentity:
    return SandboxAmendmentIdentity(
        amendment_id="research-sandbox-python",
        amendment_version="1.0.0",
        constitution_hash=h("constitution"),
        policy_hash=h("amendment-policy"),
        authorized_at=NOW,
    )


def policy() -> ExperimentReviewPolicy:
    return ExperimentReviewPolicy()


def code_identity(source: str, *, amendment_value=None, policy_value=None) -> ExperimentCodeIdentity:
    amendment_value = amendment_value or amendment()
    policy_value = policy_value or policy()
    return ExperimentCodeIdentity(
        source_hash=source_hash(source),
        python_runtime_hash=h("python-3.11"),
        dependency_manifest_hash=h("numpy-pandas"),
        review_policy_hash=policy_value.artifact_hash,
        amendment_hash=amendment_value.artifact_hash,
    )


def data_identity() -> ExperimentDataIdentity:
    return ExperimentDataIdentity(
        dataset_hash=h("dataset"),
        snapshot_hash=h("snapshot"),
        universe_hash=h("universe"),
        schema_hash=h("schema"),
        availability_policy_hash=h("availability"),
        decision_start=NOW,
        decision_end=NOW,
    )


def request(source: str, **overrides) -> ExperimentReviewRequest:
    amendment_value = amendment()
    policy_value = policy()
    base = dict(
        experiment_id="experiment.momentum",
        proposer_id="researcher.market_structure",
        reviewer_id="reviewer.security",
        source_text=source,
        code_identity=code_identity(source, amendment_value=amendment_value, policy_value=policy_value),
        data_identity=data_identity(),
        amendment=amendment_value,
        policy=policy_value,
    )
    base.update(overrides)
    return ExperimentReviewRequest(**base)


def codes(source: str) -> set[ReviewFailureCode]:
    return {item.code for item in ExperimentReviewGate().review(request(source)).findings}


def ordered(*rows):
    return tuple(sorted(rows, key=lambda row: row.artifact_hash))


def test_identity_and_score_table_are_stable_and_label_free() -> None:
    source = "import numpy as np\ndef research_experiment(features):\n    return np.log1p(features['volume'])\n"
    code = code_identity(source)
    data = data_identity()
    row = ScoreRow(symbol="000001.SZ", decision_time=NOW, source_available_at=NOW, score=1.25, label_horizon_sessions=20)
    table = ScoreTable(
        experiment_id="experiment.momentum",
        code_identity_hash=code.artifact_hash,
        data_identity_hash=data.artifact_hash,
        rows=(row,),
    )
    replay = ScoreTable.from_payload(table.to_dict())
    assert replay.artifact_hash == table.artifact_hash
    assert "label" not in ScoreRow.__dataclass_fields__
    assert "label" not in table.to_dict()["rows"][0]


def test_metric_label_text_is_not_mistaken_for_data_access() -> None:
    source = (
        "def run_research(train_rows, prediction_rows, seed):\n"
        "    return {'rows': [{'regime_id': 'all', 'metric_name': 'mean_future_return', "
        "'estimate': 0.0, 'sample_count': len(train_rows)}]}\n"
    )
    decision = ExperimentReviewGate().review(request(source))
    assert decision.status is MechanicalReviewStatus.ACCEPTED


def test_dict_get_cannot_bypass_label_key_scan() -> None:
    source = (
        "def run_research(train_rows, prediction_rows, seed):\n"
        "    return {'rows': [{'score': prediction_rows[0].get('future_return_20d')}]}\n"
    )
    decision = ExperimentReviewGate().review(request(source))
    assert any(item.code is ReviewFailureCode.LABEL_ACCESS_FORBIDDEN for item in decision.findings)


def test_score_table_rejects_future_availability_and_duplicate_keys() -> None:
    future = NOW.replace(hour=10)
    with pytest.raises(ContractValidationError):
        ScoreRow(symbol="000001.SZ", decision_time=NOW, source_available_at=future, score=1.0, label_horizon_sessions=5)
    row_a = ScoreRow(symbol="000001.SZ", decision_time=NOW, source_available_at=NOW, score=1.0, label_horizon_sessions=5)
    row_b = replace(row_a, score=2.0)
    with pytest.raises(ContractValidationError):
        ScoreTable(experiment_id="experiment.x", code_identity_hash=h("code"), data_identity_hash=h("data"), rows=ordered(row_a, row_b))


def test_all_standard_output_tables_have_canonical_hashes() -> None:
    common = dict(experiment_id="experiment.all", code_identity_hash=h("code"), data_identity_hash=h("data"))
    event = EventRow(event_id="event.1", symbol="000001.SZ", event_type="earnings", event_time=NOW, published_at=NOW, available_at=NOW, decision_time=NOW, signal=1.0)
    regression = RegressionRow(term="roe", estimate=0.1, std_error=0.02, t_statistic=5.0, p_value=0.01, observation_count=100)
    regime = RegimeComparisonRow(regime_id="high_vol", metric_name="visible_ic", estimate=0.03, sample_count=30)
    diagnostic = DiagnosticRow(diagnostic_id="cost", metric_name="turnover", value=0.2, unit="ratio", interpretation="visible diagnostic only")
    assessment = DatasetAssessmentRow(field_name="roe", source_id="approved_proxy", coverage=0.8, pit_eligible=True, observed_count=80, limitation="visible coverage estimate")
    tables = (
        EventTable(**common, rows=(event,)),
        RegressionTable(**common, specification_id="ols.1", rows=(regression,)),
        RegimeComparisonTable(**common, rows=(regime,)),
        DiagnosticTable(**common, rows=(diagnostic,)),
        DatasetAssessmentTable(**common, rows=(assessment,)),
    )
    assert len({table.artifact_hash for table in tables}) == len(tables)
    assert all(table.output_class.value in {"DRAFT_RESEARCH_RESULT", "AUXILIARY_RESEARCH_EVIDENCE"} for table in tables)


def test_safe_numeric_research_is_accepted_but_not_authorized_to_execute() -> None:
    source = "import numpy as np\nimport pandas as pd\ndef run_research(features, seed):\n    score = features['close'].pct_change(20)\n    return pd.DataFrame({'score': np.tanh(score)})\n"
    decision = ExperimentReviewGate().review(request(source))
    assert decision.status is MechanicalReviewStatus.ACCEPTED
    assert decision.findings == ()
    assert decision.grants_execution_authority is False


def test_mapping_get_is_not_misclassified_as_network_access() -> None:
    source = "def run_research(rows, seed):\n    history = {}\n    return {'rows': history.get('rows', [])}\n"
    decision = ExperimentReviewGate().review(request(source))
    assert decision.status is MechanicalReviewStatus.ACCEPTED
    assert ReviewFailureCode.NETWORK_FORBIDDEN not in {item.code for item in decision.findings}


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("import os\nos.getenv('TOKEN')", ReviewFailureCode.FORBIDDEN_IMPORT),
        ("import requests\nrequests.get('https://example.test')", ReviewFailureCode.NETWORK_FORBIDDEN),
        ("import subprocess\nsubprocess.run(['python'])", ReviewFailureCode.PROCESS_FORBIDDEN),
        ("value = eval('1+1')", ReviewFailureCode.DYNAMIC_EXECUTION_FORBIDDEN),
        ("value = __import__('math')", ReviewFailureCode.DYNAMIC_IMPORT_FORBIDDEN),
        ("value = object.__subclasses__()", ReviewFailureCode.DUNDER_ACCESS_FORBIDDEN),
        ("value = open('result.txt')", ReviewFailureCode.FILE_IO_FORBIDDEN),
        ("import pandas as pd\nvalue = pd.read_csv('data.csv')", ReviewFailureCode.FILE_IO_FORBIDDEN),
        ("value = 'C:\\\\Users\\\\secret\\\\.env'", ReviewFailureCode.PATH_ACCESS_FORBIDDEN),
        ("score = features['future_return_20d']", ReviewFailureCode.LABEL_ACCESS_FORBIDDEN),
        ("score = features['close'].shift(-1)", ReviewFailureCode.FUTURE_REFERENCE_FORBIDDEN),
    ),
)
def test_mechanical_gate_rejects_privileged_surfaces(source: str, expected: ReviewFailureCode) -> None:
    decision = ExperimentReviewGate().review(request(source))
    assert decision.status is MechanicalReviewStatus.REJECTED
    assert expected in {item.code for item in decision.findings}
    assert decision.may_submit_once_more is True


def test_statistical_interaction_term_is_not_misclassified_as_ntfs_stream() -> None:
    source = "def run_research(rows):\n    return {'term': 'signal:shibor_high', 'rows': []}\n"
    decision = ExperimentReviewGate().review(request(source))
    assert ReviewFailureCode.PATH_ACCESS_FORBIDDEN not in {item.code for item in decision.findings}


def test_ntfs_alternate_stream_path_remains_forbidden() -> None:
    source = "value = 'C:\\\\Users\\\\secret.txt:hidden'"
    decision = ExperimentReviewGate().review(request(source))
    assert ReviewFailureCode.PATH_ACCESS_FORBIDDEN in {item.code for item in decision.findings}


def test_reviewer_suggestion_cannot_override_mechanical_rejection() -> None:
    decision = ExperimentReviewGate().review(
        request("score = eval('1+1')"),
        reviewer_suggestions=(ReviewerSuggestion(suggestion_id="reviewer.accept", message="Reviewer thinks this is useful."),),
    )
    assert decision.status is MechanicalReviewStatus.REJECTED
    assert decision.reviewer_suggestions
    assert decision.grants_execution_authority is False


def test_source_hash_mismatch_is_rejected() -> None:
    original = request("score = 1")
    tampered = replace(original, source_text="score = 2")
    decision = ExperimentReviewGate().review(tampered)
    assert decision.status is MechanicalReviewStatus.REJECTED
    assert ReviewFailureCode.SOURCE_HASH_MISMATCH in {item.code for item in decision.findings}


def test_exactly_one_revision_is_permitted_and_bound_to_prior_review() -> None:
    first = ExperimentReviewGate().review(request("score = open('x')"))
    assert first.may_submit_once_more is True
    fixed_source = "def run_research(features, seed):\n    return features['close'].pct_change(20)\n"
    revised = request(fixed_source, revision_number=1, prior_review_hash=first.artifact_hash)
    second = ExperimentReviewGate().review(revised)
    assert second.status is MechanicalReviewStatus.ACCEPTED
    assert second.may_submit_once_more is False
    with pytest.raises(ContractValidationError):
        replace(revised, revision_number=2)
    with pytest.raises(ContractValidationError):
        request(fixed_source, revision_number=1, prior_review_hash=None)


@pytest.mark.parametrize(
    "source",
    (
        "def another_name(rows, seed):\n    return []\n",
        "async def run_research(rows, seed):\n    return []\n",
        "def run_research(rows):\n    return []\n",
        "def run_research(rows, seed, extra, fourth):\n    return []\n",
        "def run_research(rows, seed, *args):\n    return []\n",
    ),
)
def test_runner_entrypoint_contract_fails_closed(source: str) -> None:
    decision = ExperimentReviewGate().review(request(source))
    assert decision.status is MechanicalReviewStatus.REJECTED
    assert ReviewFailureCode.ENTRYPOINT_INVALID in {item.code for item in decision.findings}


def test_amendment_and_policy_hashes_are_mandatory() -> None:
    base = request("score = 1")
    with pytest.raises(ContractValidationError):
        replace(base, code_identity=replace(base.code_identity, amendment_hash=h("wrong")))
    with pytest.raises(ContractValidationError):
        replace(base, code_identity=replace(base.code_identity, review_policy_hash=h("wrong")))


def test_package_exports_no_runner() -> None:
    import veriquant.agent_research_sandbox as package

    assert not hasattr(package, "IsolatedResearchRunner")
    assert not hasattr(package, "run_approved_experiment")
