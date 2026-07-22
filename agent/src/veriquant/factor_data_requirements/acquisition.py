"""Compile compliance-bound acquisition plans without performing I/O."""

from __future__ import annotations

from .analyzer import build_current_missing_field_report
from .models import (
    AcquisitionGate,
    AcquisitionPlanReport,
    AcquisitionPlanStatus,
    ApprovedAcquisitionStep,
    FactorAcquisitionPlan,
    FactorDataRequirementReport,
    FactorFieldRequirement,
    RequirementDisposition,
)


_COMMON_GATES = (
    AcquisitionGate.APPROVED_API_AND_FIELD_ALLOWLIST,
    AcquisitionGate.APPROVED_PROVIDER_POLICY,
    AcquisitionGate.AVAILABLE_AT,
    AcquisitionGate.CONTENT_HASH,
    AcquisitionGate.DECISION_TIME,
    AcquisitionGate.FROZEN_SNAPSHOT,
    AcquisitionGate.PUBLISHED_AT,
    AcquisitionGate.REVISION_AS_OF,
    AcquisitionGate.ROW_PROVENANCE,
)


class CompliantAcquisitionPlanner:
    """Turn every gap into a closed, deterministic capture specification.

    The result contains API schemas and gates only.  It has no client, endpoint,
    credential, network, filesystem-write, or official-qualification authority.
    """

    def build(self, requirements: FactorDataRequirementReport) -> AcquisitionPlanReport:
        plans = tuple(self._plan(item) for item in requirements.records)
        return AcquisitionPlanReport(
            schema_version="veriquant.factor-acquisition-plan/v1",
            requirements_report_hash=requirements.report_hash,
            projection_hash=requirements.projection_hash,
            plans=plans,
            execution_ready_increment_count=0,
        )

    @staticmethod
    def _plan(requirement: FactorFieldRequirement) -> FactorAcquisitionPlan:
        if requirement.disposition is RequirementDisposition.PIT_TIME_UNPROVEN:
            status = AcquisitionPlanStatus.BLOCKED_PIT_PROOF
            steps = (
                ApprovedAcquisitionStep(
                    sequence=1,
                    api_name="stock_basic",
                    requested_fields=(
                        "industry", "industry_available_date", "industry_effective_date", "ts_code",
                    ),
                    parameter_names=("list_status",),
                    purpose="capture effective- and available-dated classification revisions for the requested universe",
                ),
            )
            gates = _COMMON_GATES + (AcquisitionGate.EXACT_FIELD_SEMANTICS,)
            precondition = (
                "all historical members have an industry revision whose effective date is in force "
                "and whose available_at is no later than each decision_time"
            )
        elif requirement.disposition is RequirementDisposition.AVAILABLE_FROM_APPROVED_PROXY_VENDOR:
            status = AcquisitionPlanStatus.READY_FOR_APPROVED_CAPTURE
            if requirement.missing_fields == ("fund:asset_growth",):
                steps = (
                    ApprovedAcquisitionStep(
                        sequence=1,
                        api_name="balancesheet",
                        requested_fields=("ann_date", "end_date", "f_ann_date", "total_assets", "ts_code"),
                        parameter_names=("end_date", "start_date", "ts_code"),
                        purpose="capture current and prior comparable total-assets revisions",
                    ),
                )
                precondition = "two comparable reporting periods are visible as-of decision_time before asset-growth derivation"
            else:
                steps = (
                    ApprovedAcquisitionStep(
                        sequence=1,
                        api_name="fina_indicator",
                        requested_fields=("ann_date", "end_date", "roe", "ts_code"),
                        parameter_names=("end_date", "start_date", "ts_code"),
                        purpose="capture announced ROE revisions without recomputing or substituting another profitability field",
                    ),
                )
                precondition = "the selected ROE revision is published and available no later than decision_time"
            gates = _COMMON_GATES + (AcquisitionGate.EXACT_FIELD_SEMANTICS,)
        elif requirement.disposition is RequirementDisposition.SOURCE_UNAVAILABLE:
            status = AcquisitionPlanStatus.BLOCKED_SOURCE_APPROVAL
            steps = ()
            gates = tuple(sorted(_COMMON_GATES + (AcquisitionGate.EXACT_FIELD_SEMANTICS,), key=lambda item: item.value))
            precondition = (
                "operator approves an exact source field and bitemporal definition; substitutions remain forbidden"
            )
        else:
            status = AcquisitionPlanStatus.AUXILIARY_ONLY
            steps = ()
            gates = tuple(sorted(_COMMON_GATES + (AcquisitionGate.EXACT_FIELD_SEMANTICS,), key=lambda item: item.value))
            precondition = "operator supplies a closed approved field policy before any trusted acquisition"
        return FactorAcquisitionPlan(
            factor_id=requirement.factor_id,
            requirement_hash=requirement.requirement_hash,
            projection_hash=requirement.projection_hash,
            status=status,
            steps=steps,
            compliance_gates=tuple(sorted(gates, key=lambda item: item.value)),
            terminal_precondition=precondition,
            execution_ready_increment=False,
            authority_note=(
                "plan-only: successful capture still requires frozen PIT validation, FactorIR/semantic gates, "
                "Trial reservation, and independent Evidence; this plan grants no official qualification"
            ),
        )


def build_current_acquisition_plan() -> AcquisitionPlanReport:
    return CompliantAcquisitionPlanner().build(build_current_missing_field_report())

