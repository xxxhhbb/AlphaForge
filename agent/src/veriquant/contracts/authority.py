"""Authority boundaries for contract creation and protected input fields."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .enums import Capability, Component, ContractValidationError, FailureCode, Role


def _caps(*values: Capability) -> frozenset[Capability]:
    return frozenset(values)


COMPONENT_CAPABILITY_MATRIX: Mapping[Component, frozenset[Capability]] = MappingProxyType(
    {
        Component.LLM_RESEARCHER: _caps(
            Capability.PROPOSE_RESEARCH, Capability.PRODUCE_AUXILIARY_EVIDENCE
        ),
        Component.RESEARCH_DESK: _caps(
            Capability.PROPOSE_RESEARCH, Capability.PRODUCE_AUXILIARY_EVIDENCE
        ),
        Component.QUANTAALPHA: _caps(
            Capability.PROPOSE_RESEARCH, Capability.PRODUCE_AUXILIARY_EVIDENCE
        ),
        Component.QLIB: _caps(Capability.PRODUCE_AUXILIARY_EVIDENCE),
        Component.DATA_SERVICE: _caps(
            Capability.CREATE_DATASET_CARD,
            Capability.CREATE_PIT_RECORD,
            Capability.COMPILE_FACTOR_DAG,
        ),
        Component.EVIDENCE_SERVICE: _caps(
            Capability.CALCULATE_OFFICIAL_METRICS,
            Capability.CREATE_EXPERIMENT_IDENTITY,
        ),
        Component.CRITIC_SERVICE: _caps(Capability.CRITIQUE),
        Component.PROMOTION_SERVICE: _caps(Capability.PROMOTE_CANDIDATE),
        Component.ALPHA_SERVICE: _caps(Capability.CALIBRATE_ALPHA),
        Component.PORTFOLIO_SERVICE: _caps(Capability.SET_TARGET_WEIGHTS),
        Component.RISK_GATE: _caps(
            Capability.APPROVE_RISK, Capability.TRIGGER_HALT, Capability.CLEAR_HALT
        ),
        Component.ORDER_SERVICE: _caps(Capability.CREATE_ORDER_PLAN),
        Component.EXECUTION_SERVICE: _caps(Capability.RECORD_FILL),
        Component.TRIAL_LEDGER: _caps(Capability.APPEND_TRIAL),
        Component.LEDGER_SERVICE: _caps(Capability.APPEND_LEDGER),
        Component.ATTRIBUTION_SERVICE: _caps(Capability.ATTRIBUTE_RESULTS),
        Component.OPERATOR: _caps(Capability.TRIGGER_HALT),
    }
)


ROLE_CAPABILITY_MATRIX: Mapping[Role, frozenset[Capability]] = MappingProxyType(
    {
        Role.RESEARCHER: _caps(
            Capability.PROPOSE_RESEARCH, Capability.PRODUCE_AUXILIARY_EVIDENCE
        ),
        Role.DATA_STEWARD: _caps(
            Capability.CREATE_DATASET_CARD,
            Capability.CREATE_PIT_RECORD,
            Capability.COMPILE_FACTOR_DAG,
        ),
        Role.EVIDENCE_EVALUATOR: _caps(
            Capability.CALCULATE_OFFICIAL_METRICS,
            Capability.CREATE_EXPERIMENT_IDENTITY,
        ),
        Role.CRITIC: _caps(Capability.CRITIQUE),
        Role.PROMOTION_APPROVER: _caps(Capability.PROMOTE_CANDIDATE),
        Role.PORTFOLIO_MANAGER: _caps(
            Capability.CALIBRATE_ALPHA, Capability.SET_TARGET_WEIGHTS
        ),
        Role.RISK_OFFICER: _caps(
            Capability.APPROVE_RISK, Capability.TRIGGER_HALT, Capability.CLEAR_HALT
        ),
        Role.EXECUTION_OPERATOR: _caps(
            Capability.CREATE_ORDER_PLAN, Capability.RECORD_FILL
        ),
        Role.LEDGER_CUSTODIAN: _caps(
            Capability.APPEND_TRIAL, Capability.APPEND_LEDGER
        ),
        Role.AUDITOR: _caps(Capability.ATTRIBUTE_RESULTS),
    }
)


PROTECTED_FIELD_CAPABILITIES: Mapping[str, Capability] = MappingProxyType(
    {
        "official_metrics": Capability.CALCULATE_OFFICIAL_METRICS,
        "expected_returns": Capability.CALIBRATE_ALPHA,
        "promotion": Capability.PROMOTE_CANDIDATE,
        "promotion_decision": Capability.PROMOTE_CANDIDATE,
        "risk_approval": Capability.APPROVE_RISK,
        "target_weights": Capability.SET_TARGET_WEIGHTS,
        "orders": Capability.CREATE_ORDER_PLAN,
        "fills": Capability.RECORD_FILL,
        "ledger_entries": Capability.APPEND_LEDGER,
    }
)


INPUT_FIELD_OWNERSHIP: Mapping[str, Component] = MappingProxyType(
    {
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
)


def require_component_capability(component: Component, capability: Capability) -> None:
    if capability not in COMPONENT_CAPABILITY_MATRIX.get(component, frozenset()):
        raise ContractValidationError(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            f"{component.value} lacks capability {capability.value}",
            details={"component": component.value, "capability": capability.value},
        )


def require_role_capability(role: Role, capability: Capability) -> None:
    if capability not in ROLE_CAPABILITY_MATRIX.get(role, frozenset()):
        raise ContractValidationError(
            FailureCode.UNAUTHORIZED_CAPABILITY,
            f"{role.value} lacks capability {capability.value}",
            details={"role": role.value, "capability": capability.value},
        )


def _walk_fields(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[str, tuple[str, ...]]]:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = (*path, key)
            yield key, child_path
            yield from _walk_fields(child, child_path)
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            yield from _walk_fields(child, (*path, str(index)))


def validate_component_payload(component: Component, payload: Mapping[str, Any]) -> None:
    """Reject authority-bearing fields not owned by the submitting component."""

    for field_name, path in _walk_fields(payload):
        capability = PROTECTED_FIELD_CAPABILITIES.get(field_name)
        if capability is None:
            continue
        require_component_capability(component, capability)
        owner = INPUT_FIELD_OWNERSHIP[field_name]
        if component is not owner:
            raise ContractValidationError(
                FailureCode.INVALID_FIELD_OWNER,
                f"{field_name} is owned by {owner.value}, not {component.value}",
                details={
                    "field": field_name,
                    "path": ".".join(path),
                    "owner": owner.value,
                    "component": component.value,
                },
            )

