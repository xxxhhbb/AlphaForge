from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.veriquant.contracts import (
    COMPONENT_CAPABILITY_MATRIX,
    INPUT_FIELD_OWNERSHIP,
    ROLE_CAPABILITY_MATRIX,
    Capability,
    Component,
    ContractValidationError,
    EvidenceClass,
    EvidencePack,
    FailureCode,
    Role,
    require_role_capability,
)


NOW = datetime(2025, 1, 15, 16, 0, tzinfo=timezone.utc)
HASH = "sha256:" + "b" * 64


def test_component_and_role_matrices_cover_deterministic_dag_compilation() -> None:
    assert Capability.COMPILE_FACTOR_DAG in COMPONENT_CAPABILITY_MATRIX[Component.DATA_SERVICE]
    assert Capability.COMPILE_FACTOR_DAG in ROLE_CAPABILITY_MATRIX[Role.DATA_STEWARD]


def test_researcher_role_cannot_calculate_official_metrics() -> None:
    with pytest.raises(ContractValidationError) as caught:
        require_role_capability(Role.RESEARCHER, Capability.CALCULATE_OFFICIAL_METRICS)

    assert caught.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_input_ownership_assigns_each_authority_field_to_one_component() -> None:
    assert INPUT_FIELD_OWNERSHIP == {
        "official_metrics": Component.EVIDENCE_SERVICE,
        "expected_returns": Component.ALPHA_SERVICE,
        "promotion": Component.PROMOTION_SERVICE,
        "promotion_decision": Component.PROMOTION_SERVICE,
        "risk_approval": Component.RISK_GATE,
        "target_weights": Component.PORTFOLIO_SERVICE,
        "orders": Component.ORDER_SERVICE,
        "fills": Component.EXECUTION_SERVICE,
        "ledger_entries": Component.LEDGER_SERVICE,
    }


def test_deterministic_evidence_service_can_build_official_pack() -> None:
    payload = {
        "contract_type": "EvidencePack",
        "schema_version": "1.0.0",
        "evidence_id": "evidence-001",
        "experiment_identity_hash": HASH,
        "official_metrics": {"rank_ic": 0.03, "turnover": 0.4},
        "artifact_hashes": [HASH],
        "input_hashes": [HASH],
        "decision_time": NOW.isoformat(),
        "label_start": (NOW + timedelta(days=1)).isoformat(),
        "label_end": (NOW + timedelta(days=2)).isoformat(),
        "event_time": (NOW + timedelta(days=3)).isoformat(),
        "published_at": (NOW + timedelta(days=3, seconds=1)).isoformat(),
        "available_at": (NOW + timedelta(days=3, seconds=2)).isoformat(),
        "produced_by": "EVIDENCE_SERVICE",
        "state": "EVALUATED",
        "evidence_class": "OFFICIAL_EVIDENCE",
    }

    evidence = EvidencePack.from_payload(payload)

    assert evidence.evidence_class is EvidenceClass.OFFICIAL_EVIDENCE
    assert evidence.artifact_hash.startswith("sha256:")

