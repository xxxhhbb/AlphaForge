from __future__ import annotations

from datetime import datetime, timezone

import pytest

from veriquant.adapters.qlib import QlibCostConfig, QlibDateSegment
from veriquant.contracts import ContractValidationError, FailureCode, ResearchState
from veriquant.data import FieldRole
from veriquant.factors import FACTOR_DAG_SCHEMA_VERSION
from veriquant.quanta_research_adapters import (
    QuantaAdapterContext,
    QuantaFactorTaskAdapter,
    QuantaLineageAdapter,
    QuantaQlibManifestAdapter,
    QuantaReportDraftAdapter,
    QuantaStaticLintAdapter,
)


HASH0 = "sha256:" + "0" * 64
HASH1 = "sha256:" + "1" * 64
HASH2 = "sha256:" + "2" * 64


def ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def context() -> QuantaAdapterContext:
    return QuantaAdapterContext(
        direction_hash=HASH0,
        question_hash=HASH1,
        universe=("000001.SZ", "600000.SH"),
        horizon="20d",
        decision_time=ts(20),
        availability_assumptions=("available_at must not exceed decision_time",),
        field_roles={"close": FieldRole.FEATURE, "volume": FieldRole.FEATURE, "future_return_5d": FieldRole.LABEL},
    )


def hypothesis_payload() -> dict:
    return {
        "statement": "Recent price strength predicts short horizon returns.",
        "economic_mechanism": "Slow information diffusion can create persistent demand.",
        "expected_sign": "POSITIVE",
        "falsification_tests": ["lag availability by one day", "raise transaction costs"],
    }


def factor_task_payload() -> dict:
    return {
        "name": "safe_momentum_5d",
        "description": "Five day close momentum ranked cross-sectionally.",
        "expression": "rank_cross_section(pct_change(close, 5))",
        "required_fields": ["close"],
        "lookback_bars": 5,
        "neutralization": [],
    }


def date_segment(start: int, end: int) -> dict:
    return QlibDateSegment(start=ts(start), end=ts(end)).to_dict()


def cost_config() -> dict:
    return QlibCostConfig(
        open_cost_bps=1.0,
        close_cost_bps=1.0,
        linear_impact_bps=2.0,
        minimum_cost=5.0,
    ).to_dict()


def qlib_payload(tmp_path) -> dict:
    return {
        "provider_uri": str(tmp_path.resolve()),
        "region": "cn",
        "universe": ["csi300"],
        "train": date_segment(1, 3),
        "validation": date_segment(5, 7),
        "test": date_segment(9, 10),
        "label": "Ref($close,-5)/$close-1",
        "model": "LightGBM",
        "strategy": "TopkDropoutStrategy",
        "cost": cost_config(),
        "seed": 7,
    }


def test_factor_task_payload_is_draft_factor_dag_only() -> None:
    proposal = QuantaFactorTaskAdapter().adapt(
        factor_task_payload(),
        hypothesis_payload=hypothesis_payload(),
        context=context(),
    )

    assert proposal.state is ResearchState.DRAFT
    assert proposal.hypothesis.state is ResearchState.DRAFT
    assert proposal.factor_draft.state is ResearchState.DRAFT
    assert proposal.factor_dag.required_fields == ("close",)
    assert "python" not in proposal.factor_draft.proposed_expression.lower()


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "def compute(): return 1"},
        {"factor_task": "factor.py"},
        {"expression": "eval(close)"},
        {"implementation": {"callable": "lambda x: x"}},
    ],
)
def test_python_or_factor_py_payload_rejected(payload: dict) -> None:
    with pytest.raises(ContractValidationError) as exc:
        QuantaFactorTaskAdapter().adapt(
            {**factor_task_payload(), **payload},
            hypothesis_payload=hypothesis_payload(),
            context=context(),
        )

    assert exc.value.code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD


def test_static_lint_success_is_not_pit_validated() -> None:
    dag_payload = {
        "schema_version": FACTOR_DAG_SCHEMA_VERSION,
        "dag_id": "lint_safe_dag",
        "nodes": [
            {"id": "n0", "op": "field", "field": "close"},
            {"id": "n1", "op": "pct_change", "inputs": ["n0"], "periods": 5},
        ],
        "output_node": "n1",
    }

    report = QuantaStaticLintAdapter().lint(dag_payload, field_roles=context().field_roles)

    assert report.parser_accepted is True
    assert report.report_kind == "lint"
    assert report.state is ResearchState.DRAFT
    assert "PIT_VALIDATED" not in report.canonical_json()


def test_report_draft_must_carry_citations() -> None:
    with pytest.raises(ContractValidationError) as exc:
        QuantaReportDraftAdapter().adapt(
            {
                "title": "Earnings revision drift",
                "claims": ["Revision surprise is associated with demand imbalance."],
                "source_document_hash": HASH0,
            }
        )

    assert exc.value.code is FailureCode.MISSING_REQUIRED_FIELD

    payload = {
        "title": "Earnings revision drift",
        "claims": ["Revision surprise is associated with demand imbalance."],
        "citation_hashes": [HASH1],
        "source_document_hash": HASH0,
    }
    draft = QuantaReportDraftAdapter().adapt(payload)
    same = QuantaReportDraftAdapter().adapt(payload)
    assert draft.state is ResearchState.DRAFT
    assert draft.artifact_hash == same.artifact_hash


def test_qlib_manifest_visible_view_excludes_sealed_test_and_is_auxiliary(tmp_path) -> None:
    draft = QuantaQlibManifestAdapter().adapt(qlib_payload(tmp_path))
    visible_payload = draft.visible_manifest.to_dict()

    assert draft.auxiliary_only is True
    assert draft.complete_trading_chain is False
    assert "test" not in visible_payload
    assert "sealed" not in visible_payload


def test_qlib_topk_cannot_claim_complete_trading_chain(tmp_path) -> None:
    payload = qlib_payload(tmp_path)
    payload["complete_trading_chain"] = True

    with pytest.raises(ContractValidationError) as exc:
        QuantaQlibManifestAdapter().adapt(payload)

    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_rankic_only_success_rejected() -> None:
    with pytest.raises(ContractValidationError) as exc:
        QuantaLineageAdapter().adapt(
            {
                "parent_ids": ["parent_a"],
                "operator": "MUTATION",
                "generation": 1,
                "reason": "rankic > 0",
                "prompt_hash": HASH1,
                "artifact_hashes": [HASH2],
                "metrics": {"RankIC": 0.01},
                "success": True,
            }
        )

    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_lineage_cannot_write_promotion() -> None:
    with pytest.raises(ContractValidationError) as exc:
        QuantaLineageAdapter().adapt(
            {
                "parent_ids": [],
                "operator": "ORIGINAL",
                "generation": 0,
                "reason": "new starting point",
                "prompt_hash": HASH1,
                "artifact_hashes": [HASH2],
                "promotion": "CHALLENGER",
            }
        )

    assert exc.value.code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD


def test_stable_hash_and_reference_repo_not_required(tmp_path) -> None:
    left = QuantaQlibManifestAdapter().adapt(qlib_payload(tmp_path))
    right = QuantaQlibManifestAdapter().adapt(qlib_payload(tmp_path))

    assert left.artifact_hash == right.artifact_hash
